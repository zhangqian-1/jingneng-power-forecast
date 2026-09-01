"""Build real-data API fixtures from the ten station CSV files.

The generated JSON files contain only the selected seven-day test window,
not the original CSV files. They are intended for the private GitHub Actions
integration test and mirror the production contract: one request per day.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from input_adapter import STATION_FEATURES, STATION_LOAD_POINTS, STATION_WEATHER_POINTS


STATION_FILES = {
    "高安屯热电": "高安屯热电.csv",
    "京西热电": "京西热电.csv",
    "京阳热电": "京阳热电.csv",
    "京桥热电": "京桥热电.csv",
    "京丰燃气": "京丰燃气.csv",
    "未来热电": "未来热电.csv",
    "上庄热电": "上庄热电.csv",
    "深圳钰湖": "深圳钰湖.csv",
    "钰海电力": "钰海电力.csv",
    "京宜热电": "京宜热电.csv",
}


def load_station_frames(raw_dir: Path) -> dict[str, pd.DataFrame]:
    frames: dict[str, pd.DataFrame] = {}
    for station, filename in STATION_FILES.items():
        path = raw_dir / filename
        if not path.exists():
            raise FileNotFoundError(f"missing station CSV: {path}")
        frame = pd.read_csv(path, encoding="utf-8-sig", low_memory=False)
        frame["ts"] = pd.to_datetime(frame["ts"])
        frame = frame.drop_duplicates("ts").set_index("ts").sort_index()
        expected = set(STATION_LOAD_POINTS[station]) | set(STATION_WEATHER_POINTS[station])
        missing_columns = sorted(expected - set(frame.columns))
        if missing_columns:
            raise ValueError(f"{station} is missing columns: {missing_columns}")
        frames[station] = frame
    return frames


def latest_common_timestamp(frames: dict[str, pd.DataFrame]) -> pd.Timestamp:
    latest: list[pd.Timestamp] = []
    for station, frame in frames.items():
        columns = list(set(STATION_LOAD_POINTS[station]) | set(STATION_WEATHER_POINTS[station]))
        valid_rows = frame[columns].notna().any(axis=1)
        if not valid_rows.any():
            raise ValueError(f"{station} has no real measurements")
        latest.append(frame.index[valid_rows].max())
    end = min(latest).floor("15min")
    if end - pd.Timedelta(days=7) < min(frame.index.min() for frame in frames.values()):
        raise ValueError("not enough overlapping history for a seven-day fixture")
    return end


def make_payloads(frames: dict[str, pd.DataFrame], output_dir: Path, end: pd.Timestamp) -> None:
    timeline = pd.date_range(end=end, periods=7 * 96, freq="15min")
    output_dir.mkdir(parents=True, exist_ok=True)

    for day in range(7):
        day_index = timeline[day * 96 : (day + 1) * 96]
        records = []
        for ts in day_index:
            stations: dict[str, dict[str, dict[str, float | None]]] = {}
            for station in STATION_FEATURES:
                source = frames[station]
                if ts in source.index:
                    row = source.loc[ts]
                else:
                    row = pd.Series(dtype="float64")
                load_points = {
                    point: clean_value(row.get(point))
                    for point in STATION_LOAD_POINTS[station]
                }
                weather_points = {
                    point: clean_value(row.get(point))
                    for point in STATION_WEATHER_POINTS[station]
                }
                stations[station] = {
                    "load_points": load_points,
                    "weather_points": weather_points,
                }
            records.append({"ts": ts.strftime("%Y-%m-%d %H:%M:%S"), "stations": stations})

        payload = {
            "batchTime": day_index[-1].strftime("%Y%m%d%H%M"),
            "intervalMinutes": 15,
            "historyDays": 1,
            "data": records,
        }
        (output_dir / f"day_{day + 1:02d}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def clean_value(value: object) -> float | None:
    if value is None or pd.isna(value):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if pd.notna(number) else None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--end-time", type=str, default=None)
    args = parser.parse_args()

    frames = load_station_frames(args.raw_dir)
    end = pd.Timestamp(args.end_time) if args.end_time else latest_common_timestamp(frames)
    make_payloads(frames, args.output_dir, end.floor("15min"))
    print(f"Generated seven real-data requests ending at {end:%Y-%m-%d %H:%M:%S}")


if __name__ == "__main__":
    main()
