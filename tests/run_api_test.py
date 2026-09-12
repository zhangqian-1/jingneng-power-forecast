"""Exercise the production container with seven sequential daily requests."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
import math
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]


def expected_model_name() -> str:
    active = json.loads((ROOT / "models/active_model.json").read_text(encoding="utf-8"))
    manifest = ROOT / "models" / active["model_dir"] / active["manifest"]
    return str(json.loads(manifest.read_text(encoding="utf-8"))["model_name"])


def validate_prediction(result: dict, payload: dict, expected_model: str) -> None:
    if result.get("code") != 200 or result.get("model") != expected_model:
        raise AssertionError("response code or model does not match the active package")
    rows = result.get("data")
    if not isinstance(rows, list) or len(rows) != 96:
        raise AssertionError("response must contain 96 predictions")
    cutoff = max(datetime.fromisoformat(row["ts"]) for row in payload["data"])
    for index, row in enumerate(rows, start=1):
        target = (cutoff + timedelta(minutes=15 * index)).strftime("%Y%m%d%H%M")
        if row.get("predictedTime") != target or row.get("timeSeries") != index:
            raise AssertionError("prediction timestamps or sequence numbers are incorrect")
        power = row.get("predictedPower")
        if type(power) not in (int, float) or not math.isfinite(power):
            raise AssertionError("predictedPower must be a finite JSON number")
        if not isinstance(row.get("accuracy"), str) or not row["accuracy"].endswith("%"):
            raise AssertionError("accuracy must be a percentage string")
    cache = result.get("historyCache", {})
    if cache.get("ready") is not True or cache.get("continuousPoints") != 672:
        raise AssertionError("seven-day test cache must be ready with 672 points")


def request_json(url: str, method: str = "GET", payload: dict | None = None) -> tuple[int, dict]:
    body = None
    headers = {}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=300) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def wait_until_ready(base_url: str) -> None:
    deadline = time.time() + 180
    while time.time() < deadline:
        try:
            status, _ = request_json(f"{base_url}/api/power/forecast/latest")
            if status in {404, 200}:
                return
        except (URLError, TimeoutError, OSError):
            pass
        time.sleep(2)
    raise RuntimeError("container did not start within 180 seconds")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--fixture-dir", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--save-response", type=Path, help="Save final response for the recreation check")
    mode.add_argument("--verify-restored", type=Path, help="Check saved result and repeated inference after recreation")
    args = parser.parse_args()

    wait_until_ready(args.base_url)
    files = sorted(args.fixture_dir.glob("day_*.json"))
    if len(files) != 7:
        raise RuntimeError(f"expected 7 daily fixtures, found {len(files)}")
    model = expected_model_name()
    last_payload = json.loads(files[-1].read_text(encoding="utf-8"))

    if args.verify_restored:
        saved = json.loads(args.verify_restored.read_text(encoding="utf-8"))
        status, latest = request_json(f"{args.base_url}/api/power/forecast/latest")
        if status != 200 or latest != saved:
            raise AssertionError("latest result was not preserved across container recreation")
        validate_prediction(latest, last_payload, model)
        status, repeated = request_json(f"{args.base_url}/api/power/forecast", method="POST", payload=last_payload)
        if status != 200:
            raise AssertionError(f"history was not restored: HTTP {status}: {repeated}")
        validate_prediction(repeated, last_payload, model)
        if repeated["data"] != saved["data"]:
            raise AssertionError("repeated prediction changed after container recreation")
        print("Persistence test passed: saved result, history and repeated prediction survived recreation.")
        return

    for index, path in enumerate(files, start=1):
        payload = json.loads(path.read_text(encoding="utf-8"))
        status, result = request_json(
            f"{args.base_url}/api/power/forecast", method="POST", payload=payload
        )
        expected = 200 if index == len(files) else 409
        if status != expected:
            raise AssertionError(f"{path.name}: expected HTTP {expected}, got {status}: {result}")
        if index == len(files):
            validate_prediction(result, payload, model)
        print(f"{path.name}: HTTP {status}")

    status, latest = request_json(f"{args.base_url}/api/power/forecast/latest")
    if status != 200 or latest != result:
        raise AssertionError(f"latest endpoint does not match the last successful response: {status}")
    validate_prediction(latest, last_payload, model)
    if args.save_response:
        args.save_response.parent.mkdir(parents=True, exist_ok=True)
        args.save_response.write_text(json.dumps(latest, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Real-data API test passed: latest endpoint returned 96 predictions.")


if __name__ == "__main__":
    main()
