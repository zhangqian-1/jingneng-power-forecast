"""Persistent rolling cache of real point-level measurements."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from input_adapter import InputAdapter, InputValidationError, ParsedInput
from models.utils import time_feature_frame


class HistoryNotReadyError(ValueError):
    def __init__(self, status: dict):
        self.status = status
        super().__init__(
            f"真实历史数据不足：当前连续 {status['continuousPoints']} 点，"
            f"模型需要 {status['requiredPoints']} 点"
        )


class RealHistoryCache:
    """Merge overlapping one-day requests and retain recent real history."""

    def __init__(self, cache_path: Path, required_points: int = 672, retention_points: int = 768):
        self.cache_path = Path(cache_path)
        self.required_points = required_points
        self.retention_points = max(retention_points, required_points)
        self.adapter = InputAdapter()

    def merge(self, parsed: ParsedInput) -> tuple[pd.DataFrame, dict]:
        incoming = parsed.frame[["ts"] + parsed.point_columns + self._source_valid_columns()].copy()
        incoming["ts"] = pd.to_datetime(incoming["ts"])

        if self.cache_path.exists():
            cached = pd.read_csv(self.cache_path)
            cached["ts"] = pd.to_datetime(cached["ts"])
            combined = pd.concat([cached, incoming], ignore_index=True, sort=False)
        else:
            combined = incoming

        point_columns = parsed.point_columns
        source_columns = self._source_valid_columns()
        combined = combined.sort_values("ts").reset_index(drop=True)
        # For overlapping requests, a missing field must not erase an already
        # stored real value at the same timestamp. Keep the latest non-null
        # point value, while source-valid flags use the newest request.
        grouped_points = combined.groupby("ts", as_index=False)[point_columns].last()
        grouped_source = combined.groupby("ts", as_index=False)[source_columns].last()
        combined = pd.merge(grouped_points, grouped_source, on="ts", how="left")
        combined = combined.tail(self.retention_points).reset_index(drop=True)
        load_columns, weather_columns = self.adapter.point_columns()
        fill_quality = self.adapter.fill_missing_points(
            combined,
            load_columns,
            weather_columns,
            allow_leading_backfill=not self.cache_path.exists(),
        )

        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix(".tmp")
        combined.to_csv(temporary, index=False, encoding="utf-8-sig")
        temporary.replace(self.cache_path)

        history, status = self._latest_continuous_history(combined)
        status.update(
            {
                "cacheFile": str(self.cache_path),
                "cachedPoints": int(len(combined)),
                "cacheStart": combined["ts"].iloc[0].isoformat(sep=" "),
                "cacheEnd": combined["ts"].iloc[-1].isoformat(sep=" "),
                "cacheFillQuality": fill_quality,
            }
        )
        if len(history) < self.required_points:
            raise HistoryNotReadyError(status)

        # Keep the extra retained context for station-state smoothing. The
        # predictor will take the latest required_points after state features
        # are calculated exactly as in offline preprocessing.
        model_frame = self.adapter.build_model_frame(history.reset_index(drop=True))
        time_features = time_feature_frame(model_frame["ts"])
        for column in time_features.columns:
            model_frame[column] = time_features[column].to_numpy(dtype=float)
        return model_frame, status

    def _latest_continuous_history(self, frame: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
        if frame.empty:
            return frame, {"ready": False, "continuousPoints": 0, "requiredPoints": self.required_points}
        breaks = frame["ts"].diff().ne(pd.Timedelta(minutes=15))
        segment = breaks.cumsum()
        latest_segment = segment.iloc[-1]
        continuous = frame[segment == latest_segment].copy()
        return continuous, {
            "ready": len(continuous) >= self.required_points,
            "continuousPoints": int(len(continuous)),
            "requiredPoints": self.required_points,
            "continuousStart": continuous["ts"].iloc[0].isoformat(sep=" "),
            "continuousEnd": continuous["ts"].iloc[-1].isoformat(sep=" "),
        }

    @staticmethod
    def _source_valid_columns() -> list[str]:
        from input_adapter import STATION_FEATURES

        return [f"__{prefix}_source_valid" for prefix in STATION_FEATURES.values()]
