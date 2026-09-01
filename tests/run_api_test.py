"""Exercise the production container with seven sequential daily requests."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


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
    args = parser.parse_args()

    wait_until_ready(args.base_url)
    files = sorted(args.fixture_dir.glob("day_*.json"))
    if len(files) != 7:
        raise RuntimeError(f"expected 7 daily fixtures, found {len(files)}")

    for index, path in enumerate(files, start=1):
        payload = json.loads(path.read_text(encoding="utf-8"))
        status, result = request_json(
            f"{args.base_url}/api/power/forecast", method="POST", payload=payload
        )
        expected = 200 if index == len(files) else 409
        if status != expected:
            raise AssertionError(f"{path.name}: expected HTTP {expected}, got {status}: {result}")
        if index == len(files):
            predictions = result.get("data")
            if not isinstance(predictions, list) or len(predictions) != 96:
                raise AssertionError(f"final response must contain 96 predictions: {result}")
            cache = result.get("historyCache", {})
            if cache.get("continuousPoints") != 672:
                raise AssertionError(f"final cache is not 672 points: {cache}")
        print(f"{path.name}: HTTP {status}")

    status, latest = request_json(f"{args.base_url}/api/power/forecast/latest")
    if status != 200 or len(latest.get("data", [])) != 96:
        raise AssertionError(f"latest endpoint did not return 96 predictions: {status}, {latest}")
    print("Real-data container test passed: latest endpoint returned 96 predictions.")


if __name__ == "__main__":
    main()
