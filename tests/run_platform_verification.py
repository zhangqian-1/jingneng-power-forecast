"""Local real-model verification, isolated caches, restart and full rolling replay."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import socket
import shutil
import subprocess
import sys

import pandas as pd

from platform_test_utils import PLATFORM_PATH, validate_not_ready, validate_platform_prediction
from run_api_test import request_json, wait_until_ready


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
from time_policy import MODEL_TIMEZONE, TIME_POLICY_ID, TIMEZONE_BASIS


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


@contextmanager
def server(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    with (directory / "service.log").open("a", encoding="utf-8") as log:
        process = subprocess.Popen([
            sys.executable, "-u", "app/api.py", "--host", "127.0.0.1", "--port", str(port),
            "--device", "cpu", "--history-cache", str(directory / "platform.csv"),
            "--latest-json", str(directory / "platform_latest.json"),
        ], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        try:
            base_url = f"http://127.0.0.1:{port}"
            wait_until_ready(base_url)
            yield base_url
        finally:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=20)


def run(*args: str) -> None:
    subprocess.run([sys.executable, *map(str, args)], cwd=ROOT, check=True)


def with_timestamp_format(payload: dict, separator: str, suffix: str) -> dict:
    return dict(payload, frames=[dict(row, timestamp=row["timestamp"].replace(" ", separator) + suffix)
                                 for row in payload["frames"]])


def score_test_set(output: Path, workers: int) -> None:
    """Independent HTTP services keep each chronological replay and cache isolated."""
    if workers == 1:
        with server(output / "rolling") as url:
            run("tests/run_rolling_accuracy_test.py", "--base-url", url, "--output-dir", output / "rolling_accuracy")
        return
    from run_rolling_accuracy_test import write_outputs
    workers = min(workers, 73)
    sizes = [73 // workers + (index < 73 % workers) for index in range(workers)]
    tasks = []
    offset = 0
    for index, days in enumerate(sizes):
        tasks.append((index, pd.Timestamp("2025-10-20") + pd.Timedelta(days=offset), days))
        offset += days

    def part(task):
        index, start, days = task
        directory = output / "rolling_shards" / f"part_{index:02d}"
        directory.mkdir(parents=True)
        with server(directory / "service") as url, (directory / "replay.log").open("w", encoding="utf-8") as log:
            # All shards seed from the same real history. Some weather points have
            # gaps longer than 14 days, so a later independent cold start is invalid.
            warmup_days = (start - pd.Timestamp("2025-10-06")).days
            subprocess.run([sys.executable, "-u", "tests/run_rolling_accuracy_test.py",
                "--base-url", url, "--target-start", str(start), "--target-windows", str(days),
                "--warmup-days", str(warmup_days),
                "--output-dir", str(directory / "results")], cwd=ROOT, check=True, stdout=log, stderr=subprocess.STDOUT)
        print(f"HTTP replay part {index + 1}/{workers} completed: {days * 96} targets", flush=True)
        return directory / "results"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        directories = list(pool.map(part, tasks))
    result = pd.concat([pd.read_csv(path / "rolling_predictions.csv", parse_dates=["ts"])
                        for path in directories], ignore_index=True).sort_values("ts")
    expected = pd.date_range("2025-10-20", periods=7008, freq="15min")
    if not pd.DatetimeIndex(result.ts).equals(expected):
        raise AssertionError("Sharded HTTP replay has missing or duplicate target times")
    statuses = {}
    for directory in directories:
        summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
        for key, count in summary["http_status_counts"].items():
            statuses[key] = statuses.get(key, 0) + count
    aggregate = output / "rolling_accuracy"
    warmup_requests = sum((start - pd.Timestamp("2025-10-06")).days for _, start, _ in tasks)
    write_outputs(result, aggregate, {"warmup_days": warmup_requests, "target_windows": 73,
        "target_start": str(expected[0]), "target_end": str(expected[-1]),
        "raw_dir": ROOT / "tests/real_data_raw", "api_url": "isolated_local_http_services",
        "http_status_counts": statuses})
    shutil.copytree(directories[0] / "samples", aggregate / "samples")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--rolling-workers", type=int, default=1, help="Independent local HTTP replay services")
    args = parser.parse_args()
    if args.rolling_workers < 1:
        parser.error("rolling-workers must be positive")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    for name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
        os.environ[name] = "1"
    os.environ["PYTHONUTF8"] = "1"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["MPLBACKEND"] = "Agg"
    fixtures = output / "fixtures"
    run("tests/build_real_test_payloads.py", "--raw-dir", "tests/real_data_raw",
        "--output-dir", fixtures, "--end-time", "2025-10-12 23:45:00")
    smoke = output / "smoke"
    with server(smoke) as url:
        run("tests/run_api_test.py", "--base-url", url, "--fixture-dir", fixtures,
            "--save-response", output / "platform_smoke.json")
        for old_route in ("/api/power/forecast", "/api/power/forecast/latest"):
            assert request_json(url + old_route)[0] == 404
            assert request_json(url + old_route, "POST", {})[0] == 404
        before = (smoke / "platform.csv").read_bytes()
        payload = json.loads((fixtures / "day_07.json").read_text(encoding="utf-8"))
        mixed = with_timestamp_format(payload, "T", "Z")
        mixed["frames"][0]["timestamp"] = payload["frames"][0]["timestamp"]
        for value in ([], {}, dict(payload, frames=payload["frames"][:-1]), mixed):
            status, response = request_json(url + PLATFORM_PATH, "POST", value)
            assert status == 400 and response["reason"] == "invalid_request", response
        assert (smoke / "platform.csv").read_bytes() == before
        assert (smoke / "platform.csv").is_file()
        assert request_json(url + PLATFORM_PATH, "POST", json.loads((fixtures / "day_01.json").read_text(encoding="utf-8")))[0] == 400
        assert (smoke / "platform.csv").read_bytes() == before
        last_result = request_json(url + PLATFORM_PATH + "/latest")[1]
        cached_frame = pd.read_csv(smoke / "platform.csv")
        formats = [(" ", ""), ("T", ""), ("T", "Z"), ("T", "+00:00"),
                   (" ", "+00:00"), ("T", ".000Z"), ("T", ".000000000+00:00"), ("T", "+0000")]
        for separator, suffix in formats:
            formatted = with_timestamp_format(payload, separator, suffix)
            status, response = request_json(url + PLATFORM_PATH, "POST", formatted)
            assert status == 200, response
            validate_platform_prediction(response, formatted)
            assert [row["value"] for row in response["result_point"]] == [row["value"] for row in last_result["result_point"]]
            pd.testing.assert_frame_equal(pd.read_csv(smoke / "platform.csv"), cached_frame,
                                          check_exact=False, rtol=1e-12, atol=1e-12)
            assert request_json(url + PLATFORM_PATH + "/latest")[1] == response
            last_result = response
        write_json(output / "samples/input_iso8601.json", formatted)
        write_json(output / "samples/output_iso8601.json", response)
        unavailable = dict(payload, frames=[dict(row, timestamp=(datetime.fromisoformat(row["timestamp"]) + timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S"))
                                            for row in payload["frames"]])
        status, response = request_json(url + PLATFORM_PATH, "POST", unavailable)
        assert status == 200
        validate_not_ready(response)
        assert request_json(url + PLATFORM_PATH + "/latest")[1] == last_result
    # Restart checks use the saved weather-seeded cache before introducing a gap.
    # Repeat on a fresh directory so the preceding negative check stays isolated.
    restart = output / "restart"
    iso_fixtures = output / "fixtures_iso8601"
    for path in sorted(fixtures.glob("day_*.json")):
        write_json(iso_fixtures / path.name, with_timestamp_format(json.loads(path.read_text(encoding="utf-8")), "T", "Z"))
    with server(restart) as url:
        run("tests/run_api_test.py", "--base-url", url, "--fixture-dir", iso_fixtures,
            "--save-response", output / "platform_restart.json")
    with server(restart) as url:
        run("tests/run_api_test.py", "--base-url", url, "--fixture-dir", iso_fixtures,
            "--verify-restored", output / "platform_restart.json")
        latest_path = restart / "platform_latest.json"
        saved = json.loads(latest_path.read_text(encoding="utf-8"))
        assert saved["time_policy"] == TIME_POLICY_ID and saved["training_timezone"] == MODEL_TIMEZONE
        for stale in (dict(saved, training_timezone="unconfirmed", time_policy="platform_clock_unconfirmed_v1"),
                      {key: value for key, value in saved.items() if key != "time_policy"}):
            write_json(latest_path, stale)
            status, response = request_json(url + PLATFORM_PATH + "/latest")
            assert status == 404 and response["reason"] == "no_forecast"
        write_json(latest_path, saved)
        assert request_json(url + PLATFORM_PATH + "/latest")[1] == saved["response"]
    with server(output / "weather") as url:
        # Deliberately remove a real weather point to exercise the missing-data contract.
        weather_payload = json.loads((fixtures / "day_01.json").read_text(encoding="utf-8"))
        point = "GARD_11MBL3100000BT01XQ01"
        for row in weather_payload["frames"]:
            row[point] = None
        status, response = request_json(url + PLATFORM_PATH, "POST", weather_payload)
        assert status == 200 and response["reason"] == "weather_history_not_ready"
        assert point in response["missing_weather_points"]
        write_json(output / "samples/input_weather_not_ready.json", weather_payload)
        write_json(output / "samples/output_weather_not_ready.json", response)
    score_test_set(output, args.rolling_workers)
    summary = json.loads((output / "rolling_accuracy/summary.json").read_text(encoding="utf-8"))
    active = json.loads((ROOT / "models/active_model.json").read_text(encoding="utf-8"))
    manifest = json.loads((ROOT / "models" / active["model_dir"] / active["manifest"]).read_text(encoding="utf-8"))
    assert summary["metrics"]["points"] == 7008
    assert abs(summary["metrics"]["mape_percent"] - manifest["test_metrics"]["mape"]) <= 0.01
    result = {"local_http_tests": "passed", "restart_tests": "passed", "invalid_request_cache_checks": "passed",
              "weather_not_ready": "passed", "removed_routes": "passed", "stale_latest_policy_rejected": "passed",
              "timestamp_format_preservation": "passed", "format_cases": len(formats),
              "format_prediction_and_cache_parity": "passed", "iso_latest_restart": "passed",
              "metrics": summary["metrics"], "training_timezone": MODEL_TIMEZONE,
              "time_policy": TIME_POLICY_ID, "timezone_basis": TIMEZONE_BASIS,
              "utc_production_acceptance": "pending", "docker_validation": "not_run"}
    write_json(output / "verification.json", result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
