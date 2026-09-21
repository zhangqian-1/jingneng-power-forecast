"""Local real-model verification, isolated caches, restart and full rolling replay."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

from platform_test_utils import PLATFORM_PATH, validate_not_ready
from run_api_test import request_json, wait_until_ready


ROOT = Path(__file__).resolve().parents[1]


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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
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
        for value in ([], {}, dict(payload, frames=payload["frames"][:-1])):
            status, response = request_json(url + PLATFORM_PATH, "POST", value)
            assert status == 400 and response["reason"] == "invalid_request", response
        assert (smoke / "platform.csv").read_bytes() == before
        assert (smoke / "platform.csv").is_file()
        assert request_json(url + PLATFORM_PATH, "POST", json.loads((fixtures / "day_01.json").read_text(encoding="utf-8")))[0] == 400
        assert (smoke / "platform.csv").read_bytes() == before
        last_result = request_json(url + PLATFORM_PATH + "/latest")[1]
        unavailable = dict(payload, frames=[dict(row, timestamp=row["timestamp"].replace("2025-10-12", "2025-10-14"))
                                            for row in payload["frames"]])
        status, response = request_json(url + PLATFORM_PATH, "POST", unavailable)
        assert status == 200
        validate_not_ready(response)
        assert request_json(url + PLATFORM_PATH + "/latest")[1] == last_result
    # Restart checks use the saved seven-day cache before introducing a gap.
    # Repeat on a fresh directory so the preceding negative check stays isolated.
    restart = output / "restart"
    with server(restart) as url:
        run("tests/run_api_test.py", "--base-url", url, "--fixture-dir", fixtures,
            "--save-response", output / "platform_restart.json")
    with server(restart) as url:
        run("tests/run_api_test.py", "--base-url", url, "--fixture-dir", fixtures,
            "--verify-restored", output / "platform_restart.json")
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
    with server(output / "rolling") as url:
        run("tests/run_rolling_accuracy_test.py", "--base-url", url, "--output-dir", output / "rolling_accuracy")
    summary = json.loads((output / "rolling_accuracy/summary.json").read_text(encoding="utf-8"))
    active = json.loads((ROOT / "models/active_model.json").read_text(encoding="utf-8"))
    manifest = json.loads((ROOT / "models" / active["model_dir"] / active["manifest"]).read_text(encoding="utf-8"))
    assert summary["metrics"]["points"] == 7008
    assert abs(summary["metrics"]["mape_percent"] - manifest["test_metrics"]["mape"]) <= 0.01
    result = {"local_http_tests": "passed", "restart_tests": "passed", "invalid_request_cache_checks": "passed",
              "weather_not_ready": "passed", "removed_routes": "passed",
              "metrics": summary["metrics"], "training_timezone": "unconfirmed",
              "utc_production_acceptance": "pending", "docker_validation": "not_run"}
    write_json(output / "verification.json", result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
