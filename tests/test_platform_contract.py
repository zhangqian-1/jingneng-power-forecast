"""Contract and missing-data edge cases; fixture values are not accuracy data."""
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from history_cache import HistoryNotReadyError, RealHistoryCache
from input_adapter import InputAdapter, InputValidationError, STATION_LOAD_POINTS, STATION_WEATHER_POINTS
from platform_adapter import POINT_TABLE, PlatformForecastService, to_model_payload, to_platform_result
from platform_test_utils import validate_platform_prediction


class PlatformContractTests(unittest.TestCase):
    def setUp(self):
        self.payload = json.loads((ROOT / "examples/platform_input_example.json").read_text(encoding="utf-8"))
        self.records = {"data": [
            {"ts": row["timestamp"], "stations": {
                station: {
                    "load_points": {point: row.get(point) for point in STATION_LOAD_POINTS[station]},
                    "weather_points": {point: row.get(point) for point in STATION_WEATHER_POINTS[station]},
                } for station in STATION_LOAD_POINTS
            }} for row in self.payload["frames"]
        ]}
        self.adapter = InputAdapter()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache_path = Path(self.temp.name) / "platform.csv"

    def test_all_raw_points_and_features_preserved(self):
        self.assertEqual(len(POINT_TABLE), 35)
        expected = self.adapter.parse_json(self.records)
        actual = self.adapter.parse_json(to_model_payload(self.payload))
        pd.testing.assert_frame_equal(actual.frame, expected.frame)
        self.assertEqual(actual.input_quality, expected.input_quality)

    def test_table_order_does_not_change_point_mapping(self):
        self.payload["point_table"].reverse()
        pd.testing.assert_frame_equal(
            self.adapter.parse_json(to_model_payload(self.payload)).frame,
            self.adapter.parse_json(self.records).frame,
        )

    def test_utc_labels_preserved_and_nonzero_offset_rejected(self):
        original = copy.deepcopy(self.payload)
        for row in self.payload["frames"]:
            row["timestamp"] = row["timestamp"].replace(" ", "T") + "Z"
        actual = to_model_payload(self.payload)
        self.assertEqual(actual, to_model_payload(original))
        self.payload["frames"][0]["timestamp"] = "2025-10-19T00:00:00+08:00"
        with self.assertRaises(InputValidationError):
            to_model_payload(self.payload)

    def test_missing_duplicate_and_unknown_table_points_rejected(self):
        for table in (POINT_TABLE[:-1], POINT_TABLE + [POINT_TABLE[0]], POINT_TABLE + ["unknown"]):
            with self.subTest(table=table), self.assertRaises(InputValidationError):
                to_model_payload(dict(self.payload, point_table=table))

    def test_malformed_frames_rejected(self):
        cases = [dict(self.payload, frames=self.payload["frames"][:-1]),
                 dict(self.payload, frames=[None] * 96), [], {}]
        for key, value in (("timestamp", "bad"), ("timestamp", None),
                           ("timestamp", "2025-10-19 00:00:00.001"), ("unknown", 1.0)):
            payload = copy.deepcopy(self.payload)
            payload["frames"][0][key] = value
            cases.append(payload)
        for payload in cases:
            with self.subTest(payload_type=type(payload)), self.assertRaises(InputValidationError):
                to_model_payload(payload)

    def test_duplicate_and_discontinuous_timestamps_rejected(self):
        for timestamp in (self.payload["frames"][1]["timestamp"], "2025-10-18 23:30:00"):
            payload = copy.deepcopy(self.payload)
            payload["frames"][0]["timestamp"] = timestamp
            with self.assertRaises(InputValidationError):
                self.adapter.parse_json(to_model_payload(payload))

    def test_null_and_omitted_measurements_are_equivalent(self):
        for point in ("GARD_11MBY0100000BJ01XQ01", "GARD_11MBL3100000BT01XQ01"):
            omitted = copy.deepcopy(self.payload)
            explicit = copy.deepcopy(self.payload)
            del omitted["frames"][20][point]
            explicit["frames"][20][point] = None
            pd.testing.assert_frame_equal(
                self.adapter.parse_json(to_model_payload(omitted)).frame,
                self.adapter.parse_json(to_model_payload(explicit)).frame,
            )

    def test_zero_power_and_causal_weather_fill(self):
        self._initialize_unit_weather()
        load = "GARD_11MBY0100000BJ01XQ01"
        weather = "GARD_11MBL3100000BT01XQ01"
        self.payload["frames"][20][load] = None
        self.payload["frames"][20][weather] = None
        cache = RealHistoryCache(self.cache_path, required_points=96)
        actual, _ = cache.merge(self.adapter.parse_json(to_model_payload(self.payload)))
        self.assertEqual(actual.iloc[20]["__load::高安屯热电::" + load], 0.0)
        self.assertEqual(actual.iloc[20]["__weather::高安屯热电::" + weather],
                         actual.iloc[19]["__weather::高安屯热电::" + weather])

    def test_weather_is_not_backfilled_and_missing_point_identified(self):
        self._initialize_unit_weather()
        point = "GARD_11MBL3100000BT01XQ01"
        self.payload["frames"][0][point] = None
        with self.assertRaises(HistoryNotReadyError) as error:
            RealHistoryCache(self.cache_path, required_points=96).merge(
                self.adapter.parse_json(to_model_payload(self.payload)))
        self.assertEqual(error.exception.status["continuousPoints"], 95)
        self.assertIn(point, error.exception.status["missingWeatherPoints"])

    def _initialize_unit_weather(self):
        from input_adapter import STATION_WEATHER_POINTS
        # Controlled unit fixtures; real missing weather is tested in HTTP replay.
        for row in self.payload["frames"]:
            for points in STATION_WEATHER_POINTS.values():
                row.update({point: 20.0 for point in points})

    def test_not_ready_service_preserves_reason_and_counts(self):
        for weather in (False, True):
            status = {"continuousPoints": 0 if weather else 96, "requiredPoints": 672,
                      "waitingForWeatherHistory": weather, "missingWeatherPoints": ["point"] if weather else []}
            predictor = SimpleNamespace(input_size=672, model_name="unit-test-only",
                                        backend=SimpleNamespace(station=SimpleNamespace(state_centers={})),
                                        predict_records=Mock(side_effect=HistoryNotReadyError(status)))
            service = PlatformForecastService(predictor)
            with self.assertLogs("platform_adapter", level="WARNING"):
                result = service.compute(self.payload)
            self.assertEqual(result["result_point"], [])
            self.assertEqual(result["reason"], "weather_history_not_ready" if weather else "history_not_ready")
            self.assertEqual(result["continuous_points"], status["continuousPoints"])

    def test_response_mapping_preserves_real_prediction_values(self):
        example = json.loads((ROOT / "examples/platform_output_example.json").read_text(encoding="utf-8"))
        result_rows = {"predictions": [{"timestamp": row["timestamp"], "value": row["value"]}
                                       for row in example["result_point"]]}
        result = to_platform_result(result_rows)
        validate_platform_prediction(result, self.payload)
        np.testing.assert_array_equal([row["value"] for row in result["result_point"]],
                                      [row["value"] for row in result_rows["predictions"]])
        result_rows["predictions"][0]["value"] = float("nan")
        with self.assertRaises(ValueError):
            to_platform_result(result_rows)

    def test_platform_examples_match_generated_contract(self):
        for name in ("example", "not_ready", "weather_not_ready"):
            payload = json.loads((ROOT / f"examples/platform_input_{name}.json").read_text(encoding="utf-8"))
            self.adapter.parse_json(to_model_payload(payload))
            response = json.loads((ROOT / f"examples/platform_output_{name}.json").read_text(encoding="utf-8"))
            if name == "example":
                validate_platform_prediction(response, payload)
            else:
                from platform_test_utils import validate_not_ready
                validate_not_ready(response)

    def test_invalid_output_count_or_duplicate_target_rejected(self):
        example = json.loads((ROOT / "examples/platform_output_example.json").read_text(encoding="utf-8"))
        result_rows = {"predictions": [{"timestamp": row["timestamp"], "value": row["value"]}
                                       for row in example["result_point"]]}
        with self.assertRaises(ValueError):
            to_platform_result({"predictions": result_rows["predictions"][:-1]})
        result_rows["predictions"][0]["timestamp"] = result_rows["predictions"][1]["timestamp"]
        with self.assertRaises(ValueError):
            to_platform_result(result_rows)


if __name__ == "__main__":
    unittest.main()
