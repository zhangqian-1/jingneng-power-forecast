"""Load fitted NHITS and PatchTST components for single-step inference."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


class NeuralForecastComponent:
    """Load one saved NeuralForecast model and predict from total power history."""

    def __init__(self, model_dir: Path, alias: str, device: str) -> None:
        try:
            from neuralforecast import NeuralForecast
        except ImportError as exc:
            raise RuntimeError(
                "Single-step inference requires neuralforecast; install production requirements first"
            ) from exc

        self.alias = alias
        self.model = NeuralForecast.load(str(model_dir), map_location=device)
        accelerator = "gpu" if device == "cuda" else "cpu"
        for fitted_model in self.model.models:
            fitted_model.trainer_kwargs["accelerator"] = accelerator
            fitted_model.trainer_kwargs["devices"] = 1

    def predict(self, frame: pd.DataFrame, input_points: int) -> np.ndarray:
        history = frame[["ts", "total_power"]].tail(input_points).copy()
        if len(history) != input_points:
            raise ValueError(
                f"{self.alias} requires {input_points} history points, got {len(history)}"
            )
        history = history.rename(columns={"ts": "ds", "total_power": "y"})
        history.insert(0, "unique_id", "total_power")
        prediction = self.model.predict(df=history)
        value_columns = [
            column for column in prediction.columns if column not in {"unique_id", "ds"}
        ]
        if len(value_columns) != 1:
            raise ValueError(
                f"{self.alias} returned unexpected prediction columns: {value_columns}"
            )
        return prediction[value_columns[0]].to_numpy(dtype=float).reshape(1, -1)
