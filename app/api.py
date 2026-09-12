"""HTTP API for real-data power forecasting."""
from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from input_adapter import InputValidationError
from history_cache import HistoryNotReadyError
from predict import DEFAULT_HISTORY_CACHE, DEFAULT_MODEL_PATH, PowerPredictor


PACKAGE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LATEST_JSON = PACKAGE_ROOT / "runtime" / "latest_forecast.json"
MAX_REQUEST_BYTES = 50 * 1024 * 1024


class ForecastHandler(BaseHTTPRequestHandler):
    predictor: PowerPredictor
    latest_json: Path = DEFAULT_LATEST_JSON
    inference_lock = threading.Lock()

    def send_json(self, status_code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path != "/api/power/forecast":
            self.send_json(404, {"code": 404, "msg": "not_found", "data": []})
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0:
                raise InputValidationError("请求体不能为空")
            if content_length > MAX_REQUEST_BYTES:
                self.send_json(413, {"code": 413, "msg": "request_too_large", "data": []})
                return

            try:
                payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise InputValidationError("请求体不是有效的 UTF-8 JSON") from exc

            with self.inference_lock:
                result = self.predictor.predict_json(payload)
                self._save_latest(result)
            self.send_json(200, result)
        except InputValidationError as exc:
            self.send_json(400, {"code": 400, "msg": str(exc), "data": []})
        except HistoryNotReadyError as exc:
            self.send_json(
                409,
                {
                    "code": 409,
                    "msg": str(exc),
                    "historyCache": exc.status,
                    "data": [],
                },
            )
        except ValueError as exc:
            self.send_json(422, {"code": 422, "msg": str(exc), "data": []})
        except Exception as exc:
            self.send_json(500, {"code": 500, "msg": f"prediction_failed: {exc}", "data": []})

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path != "/api/power/forecast/latest":
            self.send_json(404, {"code": 404, "msg": "not_found", "data": []})
            return
        if not self.latest_json.exists():
            self.send_json(404, {"code": 404, "msg": "尚未收到真实数据，暂无预测结果", "data": []})
            return

        try:
            payload = json.loads(self.latest_json.read_text(encoding="utf-8"))
            if payload.get("model") != self.predictor.model_name:
                self.send_json(404, {"code": 404, "msg": "当前模型尚未产生预测结果", "data": []})
                return
            self.send_json(200, payload)
        except Exception as exc:
            self.send_json(500, {"code": 500, "msg": f"latest_result_unavailable: {exc}", "data": []})

    def _save_latest(self, payload: dict) -> None:
        self.latest_json.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.latest_json.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.latest_json)

    def log_message(self, format: str, *args) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="Jingneng power forecast API")
    parser.add_argument("--model-path", type=Path, default=DEFAULT_MODEL_PATH)
    parser.add_argument("--latest-json", type=Path, default=DEFAULT_LATEST_JSON)
    parser.add_argument("--history-cache", type=Path, default=DEFAULT_HISTORY_CACHE)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--device", choices=["cpu", "cuda"], default=None)
    args = parser.parse_args()

    ForecastHandler.predictor = PowerPredictor(
        args.model_path,
        device=args.device,
        history_cache_path=args.history_cache,
    )
    ForecastHandler.latest_json = args.latest_json
    server = ThreadingHTTPServer((args.host, args.port), ForecastHandler)
    print(f"Power forecast API: http://{args.host}:{args.port}")
    print("POST /api/power/forecast")
    print("GET  /api/power/forecast/latest")
    server.serve_forever()


if __name__ == "__main__":
    main()
