"""Production inference using a versioned offline-trained model chain."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import torch

from history_cache import RealHistoryCache
from time_policy import TIME_POLICY_ID
from input_adapter import InputAdapter, STATION_FEATURES, STATION_LOAD_POINTS
from models.normalizer import Normalizer
from models.station_attention import StationAttentionHF
from models.neural_forecast import NeuralForecastComponent
from models.utils import time_feature_frame
from models.causal_state import add_causal_states


PACKAGE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL_PATH = PACKAGE_ROOT / "models" / "active_model.json"
DEFAULT_HISTORY_CACHE = PACKAGE_ROOT / "runtime" / "history_single_step_7station_2025_v1_utc_to_asia_shanghai_v1.csv"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _future_feature_frame(last_timestamp: pd.Timestamp, horizon: int) -> pd.DataFrame:
    timestamps = pd.date_range(
        start=last_timestamp + pd.Timedelta(minutes=15),
        periods=horizon,
        freq="15min",
    )
    frame = time_feature_frame(timestamps)
    step = np.arange(horizon, dtype=float)
    frame["horizon_sin"] = np.sin(2 * np.pi * step / horizon)
    frame["horizon_cos"] = np.cos(2 * np.pi * step / horizon)
    frame.insert(0, "ds", timestamps)
    return frame


class StationAttentionComponent:
    """The exact offline StationAttention network plus its preprocessing state."""

    def __init__(self, checkpoint_path: Path, device: torch.device) -> None:
        checkpoint = torch.load(checkpoint_path, map_location="cpu", weights_only=True)
        if checkpoint.get("format_version") != "2025_single_step_causal_v1":
            raise ValueError("Expected the retrained single-step seven-station checkpoint")
        if checkpoint.get("baseline_policy") != "last_observed_total_power":
            raise ValueError("Single-step checkpoint must use the last observed power baseline")
        if checkpoint.get("power_missing_policy") != "zero" or checkpoint.get("state_policy") != "causal_hold_4":
            raise ValueError("Checkpoint preprocessing policy does not match production")

        self.device = device
        self.model_params = dict(checkpoint["model_params"])
        self.input_size = int(self.model_params["input_size"])
        self.horizon = int(self.model_params["horizon"])
        self.feature_columns = list(checkpoint["feature_columns"])
        self.future_feature_columns = list(_future_feature_frame(pd.Timestamp("2025-01-01"), self.horizon).columns[1:])
        self.future_feature_columns += list(checkpoint["future_state_profile_columns"])
        if len(self.future_feature_columns) != self.model_params["n_future_features"]:
            raise ValueError("Checkpoint future-feature count does not match production")

        normalizer = checkpoint["normalizer"]
        self.normalizer = Normalizer(
            mean=np.asarray(normalizer["mean"], dtype=np.float32),
            std=np.asarray(normalizer["std"], dtype=np.float32),
            target_mean=float(normalizer["target_mean"]),
            target_std=float(normalizer["target_std"]),
        )
        self.state_centers = {
            station: np.asarray(centers, dtype=float)
            for station, centers in checkpoint["state_centers"].items()
        }
        self.future_state_profile_columns = list(
            checkpoint["future_state_profile_columns"]
        )
        self.normalized_future_state_week_profile = np.asarray(
            checkpoint["normalized_future_state_week_profile"], dtype=np.float32
        )

        self.model = StationAttentionHF(**self.model_params)
        self.model.load_state_dict(checkpoint["model_state_dict"], strict=True)
        self.model.to(device)
        self.model.eval()

    def prepare_history(self, frame: pd.DataFrame) -> pd.DataFrame:
        frame = frame.copy()
        state_columns = [f"{station}_state" for station in self.state_centers]
        if not all(column in frame for column in state_columns):
            frame = add_causal_states(frame, self.state_centers)
        return frame.tail(self.input_size).reset_index(drop=True)

    def predict_raw(
        self,
        frame: pd.DataFrame,
        last_timestamp: pd.Timestamp,
    ) -> tuple[np.ndarray, np.ndarray]:
        missing = [column for column in self.feature_columns if column not in frame.columns]
        if missing:
            raise ValueError(f"Cannot build StationAttention input features: {missing}")
        if len(frame) != self.input_size:
            raise ValueError(
                f"StationAttention requires {self.input_size} history points, got {len(frame)}"
            )

        x_values = frame[self.feature_columns].to_numpy(dtype=np.float32)
        if not np.isfinite(x_values).all():
            raise ValueError("StationAttention input contains non-finite values")
        x_normalized = self.normalizer.transform_x(x_values)

        future_frame = _future_feature_frame(last_timestamp, self.horizon)
        future_frame = self._add_future_state_profiles(future_frame)
        missing_future = [
            column
            for column in self.future_feature_columns
            if column not in future_frame.columns
        ]
        if missing_future:
            raise ValueError(f"Cannot build future StationAttention features: {missing_future}")

        future_values = future_frame[self.future_feature_columns].to_numpy(
            dtype=np.float32
        )
        x_tensor = torch.from_numpy(x_normalized).unsqueeze(0).to(self.device)
        future_tensor = torch.from_numpy(future_values).unsqueeze(0).to(self.device)
        with torch.inference_mode():
            raw_normalized = self.model(x_tensor, future_tensor).cpu().numpy()
        raw_prediction = self.normalizer.inverse_y(raw_normalized)
        baseline = (
            frame["total_power"]
            .tail(self.horizon)
            .to_numpy(dtype=float)
            .reshape(1, -1)
        )
        return raw_prediction, baseline

    def _add_future_state_profiles(self, future_frame: pd.DataFrame) -> pd.DataFrame:
        future_frame = future_frame.copy()
        timestamps = pd.DatetimeIndex(future_frame["ds"])
        minute_index = ((timestamps.hour * 60 + timestamps.minute) // 15).to_numpy(
            dtype=int
        )
        week_slot = timestamps.dayofweek.to_numpy(dtype=int) * 96 + minute_index
        profiles = self.normalized_future_state_week_profile[week_slot]
        for index, column in enumerate(self.future_state_profile_columns):
            future_frame[column] = profiles[:, index]
        return future_frame

class SingleStepBackend:
    """Each retrained component consumes 288 observations and predicts one value."""

    def __init__(self, model_dir: Path, manifest: dict[str, Any], device: str) -> None:
        self.model_dir = model_dir
        self.manifest = manifest
        self.model_name = str(manifest["model_name"])
        self.input_size = int(manifest["history_points"])
        self.horizon = int(manifest["forecast_points"])
        self.test_metrics = dict(manifest["test_metrics"])
        if self.input_size != 288 or self.horizon != 1:
            raise ValueError("Single-step models must use 288 history points and output one point")

        self._verify_artifacts()
        low = manifest["low_frequency"]
        self.low_input_points = int(low["input_points"])
        self.nhits = NeuralForecastComponent(
            model_dir / str(low["nhits_dir"]), "NHITS", device
        )
        self.patchtst = NeuralForecastComponent(
            model_dir / str(low["patchtst_dir"]), "PatchTST", device
        )
        if self.low_input_points != self.input_size:
            raise ValueError("All components must use exactly three days of history")
        for component in (self.nhits, self.patchtst):
            if len(component.model.models) != 1:
                raise ValueError("Expected one fitted model per neural component")
            fitted = component.model.models[0]
            if fitted.h != 1 or fitted.input_size != 288:
                raise ValueError("Neural component is not trained for 288 -> 1")
        torch_device = torch.device(device)
        station = manifest["station_attention"]
        self.station = StationAttentionComponent(
            model_dir / str(station["checkpoint"]), torch_device
        )
        if self.station.input_size != 288 or self.station.horizon != 1:
            raise ValueError("StationAttention is not trained for 288 -> 1")
        fusion = manifest["fusion"]
        if fusion.get("method") != "single_step_convex_blend":
            raise ValueError("Single-step model requires scalar fusion")
        if any(not 0 <= float(fusion[key]) <= 1 for key in ("nhits_weight", "station_weight")):
            raise ValueError("Invalid single-step fusion weights")
        if set(self.station.state_centers) != {f"{name}_total_power" for name in STATION_FEATURES}:
            raise ValueError("StationAttention checkpoint station list does not match the input adapter")
        for station in manifest["input_data"]["stations"]:
            if set(station["power_points"]) != STATION_LOAD_POINTS[station["station"]]:
                raise ValueError("Model power-point list does not match the input adapter")

    def predict(self, frame: pd.DataFrame, last_timestamp: pd.Timestamp) -> np.ndarray:
        prepared = self.station.prepare_history(frame)
        nhits_prediction = self.nhits.predict(prepared, self.low_input_points)
        patchtst_prediction = self.patchtst.predict(prepared, self.low_input_points)

        raw_station, _ = self.station.predict_raw(prepared, last_timestamp)
        for component in (nhits_prediction, patchtst_prediction, raw_station):
            if component.shape != (1, 1):
                raise ValueError("Each component must actually predict one value")
        config = self.manifest["fusion"]
        alpha = float(config["nhits_weight"])
        station_weight = float(config["station_weight"])
        low_prediction = alpha * nhits_prediction + (1.0 - alpha) * patchtst_prediction
        prediction = (1.0 - station_weight) * low_prediction + station_weight * raw_station
        if not np.isfinite(prediction).all():
            raise ValueError("Model returned non-finite predictions")
        return prediction.reshape(-1)

    def _verify_artifacts(self) -> None:
        expected = dict(self.manifest.get("artifacts", {}))
        for relative_path, expected_hash in expected.items():
            path = self.model_dir / relative_path
            if not path.is_file():
                raise FileNotFoundError(f"Single-step artifact is missing: {path}")
            actual_hash = _sha256(path)
            if actual_hash.lower() != str(expected_hash).lower():
                raise ValueError(f"Single-step artifact hash mismatch: {path}")


def _load_backend(model_path: Path, device: str) -> SingleStepBackend:
    active_path = Path(model_path)
    if not active_path.is_file():
        raise FileNotFoundError(f"Active model config does not exist: {active_path}")
    active = json.loads(active_path.read_text(encoding="utf-8"))
    if active.get("model_type") != "single_step_fusion":
        raise ValueError(f"Unsupported active model type: {active.get('model_type')}")
    model_dir = (active_path.parent / str(active["model_dir"])).resolve()
    manifest_path = model_dir / str(active.get("manifest", "manifest.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("model_type") != active["model_type"]:
        raise ValueError("Active model type does not match its manifest")
    return SingleStepBackend(model_dir, manifest, device)


class PowerPredictor:
    """Run the active model on internal station records."""

    def __init__(
        self,
        model_path: Path = DEFAULT_MODEL_PATH,
        device: str | None = None,
        history_cache_path: Path = DEFAULT_HISTORY_CACHE,
    ) -> None:
        requested_device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        if requested_device == "cuda" and not torch.cuda.is_available():
            raise ValueError("CUDA was requested but is not available")
        self.backend = _load_backend(Path(model_path), requested_device)
        self.model_name = self.backend.model_name
        self.input_size = self.backend.input_size
        self.horizon = self.backend.horizon
        self.history_cache = RealHistoryCache(
            cache_path=history_cache_path,
            required_points=self.input_size,
            state_centers=self.backend.station.state_centers,
            model_name=self.model_name + "__" + TIME_POLICY_ID,
        )
        self.input_adapter = InputAdapter()

    def predict_records(self, payload: dict[str, Any]) -> dict[str, Any]:
        parsed = self.input_adapter.parse_json(payload)
        frame, cache_status = self.history_cache.merge(parsed)
        prediction = self.backend.predict(frame, parsed.last_timestamp)

        future_timestamps = pd.date_range(
            start=parsed.last_timestamp + pd.Timedelta(minutes=15),
            periods=self.horizon,
            freq="15min",
        )
        output_rows = []
        for timestamp, power in zip(future_timestamps, prediction):
            output_rows.append(
                {
                    "timestamp": pd.Timestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S"),
                    "value": round(float(power), 4),
                }
            )

        return {
            "inputQuality": parsed.input_quality,
            "historyCache": cache_status,
            "predictions": output_rows,
        }
