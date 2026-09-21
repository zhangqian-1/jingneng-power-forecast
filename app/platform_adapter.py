"""Platform JSON contract; training-timezone verification remains pending."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
import logging
import math
from typing import TYPE_CHECKING, Any

import pandas as pd

from history_cache import HistoryNotReadyError
from input_adapter import (
    InputValidationError,
    POINTS_PER_DAY,
    STATION_FEATURES,
    STATION_LOAD_POINTS,
    STATION_WEATHER_POINTS,
)

if TYPE_CHECKING:
    from predict import PowerPredictor


PLATFORM_PATH = "/api/v1/fluxcast/compute"
EVENT_KEY = "JNH.Fluxcast.Compute"
VARNAME = "totalPowerForecast"
POINT_TABLE = [
    point
    for station in STATION_FEATURES
    for points in (STATION_LOAD_POINTS[station], STATION_WEATHER_POINTS[station])
    for point in sorted(points)
]
LOGGER = logging.getLogger(__name__)


def empty_result(reason: str, message: str, **details: Any) -> dict[str, Any]:
    return {
        "result_point": [],
        "event_key": EVENT_KEY,
        "reason": reason,
        "message": message,
        **details,
    }


def to_model_payload(payload: Any) -> dict[str, Any]:
    """Map all 35 raw point IDs without shifting the incoming clock labels."""
    if not isinstance(payload, Mapping):
        raise InputValidationError("请求体必须是 JSON 对象")
    table = payload.get("point_table")
    if not isinstance(table, list) or not all(isinstance(point, str) for point in table):
        raise InputValidationError("point_table 必须是测点编码字符串数组")
    if len(table) != len(set(table)):
        raise InputValidationError("point_table 不得包含重复测点")
    missing = sorted(set(POINT_TABLE) - set(table))
    extra = sorted(set(table) - set(POINT_TABLE))
    if missing or extra:
        raise InputValidationError(f"point_table 必须包含七站全部35个测点；缺少: {missing}；未知: {extra}")
    frames = payload.get("frames")
    if not isinstance(frames, list) or len(frames) != POINTS_PER_DAY:
        raise InputValidationError("frames 必须正好包含96条时间记录")

    records = []
    allowed = set(POINT_TABLE) | {"timestamp"}
    for index, frame in enumerate(frames):
        if not isinstance(frame, Mapping):
            raise InputValidationError(f"frames[{index}] 必须是对象")
        if set(frame) - allowed:
            raise InputValidationError(f"frames[{index}] 包含未知字段: {sorted(set(frame) - allowed)}")
        raw = frame.get("timestamp")
        if not isinstance(raw, str) or not raw.strip() or (" " not in raw and "T" not in raw):
            raise InputValidationError(f"frames[{index}].timestamp 必须是包含日期和时间的字符串")
        try:
            timestamp = pd.Timestamp(raw)
        except (ValueError, TypeError) as exc:
            raise InputValidationError(f"frames[{index}].timestamp 格式错误") from exc
        if pd.isna(timestamp) or timestamp != timestamp.floor("15min"):
            raise InputValidationError(f"frames[{index}].timestamp 必须有效且对齐15分钟整刻")
        if timestamp.tzinfo is not None:
            if timestamp.utcoffset() != timedelta(0):
                raise InputValidationError("平台 timestamp 必须使用UTC；不接受非零时区偏移")
            timestamp = timestamp.tz_localize(None)
        # Only remove an explicit zero offset. Do not guess the training timezone.
        stations = {
            station: {
                "load_points": {point: frame.get(point) for point in STATION_LOAD_POINTS[station]},
                "weather_points": {point: frame.get(point) for point in STATION_WEATHER_POINTS[station]},
            }
            for station in STATION_FEATURES
        }
        records.append({"ts": timestamp.strftime("%Y-%m-%d %H:%M:%S"), "stations": stations})
    return {"data": records}


def to_platform_result(result: dict[str, Any]) -> dict[str, Any]:
    rows = result["predictions"]
    if len(rows) != POINTS_PER_DAY:
        raise ValueError("Expected 96 model predictions")
    points = []
    for row in rows:
        power = float(row["value"])
        if not math.isfinite(power):
            raise ValueError("Model returned a non-finite prediction")
        timestamp = pd.to_datetime(row["timestamp"], format="%Y-%m-%d %H:%M:%S")
        points.append({"varname": VARNAME, "timestamp": timestamp.strftime("%Y-%m-%d %H:%M:%S"), "value": power})
    if len({point["timestamp"] for point in points}) != POINTS_PER_DAY:
        raise ValueError("Model returned duplicate prediction timestamps")
    return {"result_point": points, "event_key": EVENT_KEY}


class PlatformForecastService:
    def __init__(self, predictor: PowerPredictor) -> None:
        self.predictor = predictor

    def compute(self, payload: Any) -> dict[str, Any]:
        adapted = to_model_payload(payload)
        try:
            result = self.predictor.predict_records(adapted)
        except HistoryNotReadyError as exc:
            status = exc.status
            weather = bool(status.get("waitingForWeatherHistory"))
            reason = "weather_history_not_ready" if weather else "history_not_ready"
            message = (
                "温湿度历史尚不能构造完整模型输入，本次数据已缓存"
                if weather else "连续可用历史不足，本次数据已缓存"
            )
            LOGGER.warning("%s continuous=%s required=%s missing_weather=%s quality=%s", reason,
                           status["continuousPoints"], status["requiredPoints"],
                           status.get("missingWeatherPoints", []), status.get("cacheFillQuality", {}))
            return empty_result(
                reason, message,
                continuous_points=status["continuousPoints"],
                required_points=status["requiredPoints"],
                missing_weather_points=status.get("missingWeatherPoints", []),
            )
        quality = result["inputQuality"]
        if quality["submittedMissingLoadValues"] or quality["submittedMissingWeatherValues"]:
            LOGGER.warning("prediction_with_missing_measurements quality=%s", quality)
        return to_platform_result(result)
