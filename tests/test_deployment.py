"""Negative checks for the container acceptance assertions; not accuracy data."""
import copy
import json
from pathlib import Path
import unittest

from run_api_test import expected_model_name, validate_prediction


class PredictionContractTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1]
        self.payload = json.loads((root / "examples/input_example.json").read_text(encoding="utf-8"))
        self.response = json.loads((root / "examples/output_example.json").read_text(encoding="utf-8"))
        # These tests exercise the seven-day smoke-test contract, not the example's eight-day cache.
        self.response["historyCache"]["continuousPoints"] = 672
        self.model = expected_model_name()

    def test_valid_contract(self):
        validate_prediction(self.response, self.payload, self.model)

    def test_wrong_model(self):
        self.response["model"] = "old-model"
        with self.assertRaises(AssertionError):
            validate_prediction(self.response, self.payload, self.model)

    def test_wrong_time_or_sequence(self):
        for key, value in (("predictedTime", "202501010000"), ("timeSeries", 2)):
            response = copy.deepcopy(self.response)
            response["data"][0][key] = value
            with self.assertRaises(AssertionError):
                validate_prediction(response, self.payload, self.model)

    def test_missing_or_extra_predictions(self):
        for rows in (self.response["data"][:-1], self.response["data"] + self.response["data"][:1]):
            response = dict(self.response, data=rows)
            with self.assertRaises(AssertionError):
                validate_prediction(response, self.payload, self.model)

    def test_invalid_power(self):
        for value in (None, True, "123.4", float("nan"), float("inf")):
            self.response["data"][0]["predictedPower"] = value
            with self.subTest(value=value), self.assertRaises(AssertionError):
                validate_prediction(self.response, self.payload, self.model)

    def test_invalid_accuracy_type(self):
        self.response["data"][0]["accuracy"] = 95.0
        with self.assertRaises(AssertionError):
            validate_prediction(self.response, self.payload, self.model)

    def test_history_not_ready(self):
        self.response["historyCache"]["ready"] = False
        with self.assertRaises(AssertionError):
            validate_prediction(self.response, self.payload, self.model)


if __name__ == "__main__":
    unittest.main()
