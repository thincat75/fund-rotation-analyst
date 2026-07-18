#!/usr/bin/env python3
"""Create clearly labelled historical shadow checkpoints from one real shadow capture."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
from pathlib import Path
from typing import Any

from data_access import load_json, write_json


def parse_day(value: Any) -> dt.date | None:
    text = str(value or "")[:10]
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def row_day(row: dict[str, Any]) -> dt.date | None:
    for key, value in row.items():
        if "date" in str(key).lower() or "日期" in str(key):
            day = parse_day(value)
            if day:
                return day
    return None


def truncate_rows(rows: list[dict[str, Any]], end: dt.date) -> list[dict[str, Any]]:
    return [row for row in rows if row_day(row) is None or row_day(row) <= end]


def checkpoint(source: dict[str, Any], end: dt.date) -> dict[str, Any]:
    payload = copy.deepcopy(source)
    week = payload.get("week") or {}
    monday = end - dt.timedelta(days=end.weekday())
    week.update({
        "period_mode": "explicit_historical_backfill",
        "completeness": "complete" if end.weekday() >= 4 else "partial",
        "requested_end_date": end.isoformat(),
        "start_date": monday.isoformat(),
        "end_date": end.isoformat(),
    })
    payload["week"] = week
    payload["shadow_validation_mode"] = "historical_backfill"
    payload["shadow_validation_note"] = "同一次真实采集的历史序列回放，仅验证跨源数值，不等同于连续自然日在线稳定性。"
    for fund in (payload.get("funds") or {}).values():
        if isinstance(fund.get("nav"), list):
            fund["nav"] = truncate_rows(fund["nav"], end)
    styles = ((payload.get("market") or {}).get("style_indexes") or {})
    for name, rows in styles.items():
        if isinstance(rows, list):
            styles[name] = truncate_rows(rows, end)
    histories = (((payload.get("candidate_etfs") or {}).get("history") or {}))
    for variants in histories.values():
        for adjust, rows in variants.items():
            if isinstance(rows, list):
                variants[adjust] = truncate_rows(rows, end)
    history_sina = (((payload.get("candidate_etfs") or {}).get("history_sina") or {}))
    for code, rows in history_sina.items():
        if isinstance(rows, list):
            history_sina[code] = truncate_rows(rows, end)
    shadow = ((payload.get("provider_shadow") or {}).get("datasets") or {})
    shadow["fund_basic"] = []
    shadow.pop("industry_flow", None)
    shadow.pop("concept_flow", None)
    for key, value in shadow.items():
        if isinstance(value, list):
            shadow[key] = truncate_rows(value, end)
        elif key.startswith("etf_return:") and isinstance(value, dict):
            for adjust, rows in value.items():
                if isinstance(rows, list):
                    value[adjust] = truncate_rows(rows, end)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--end-date", required=True, action="append")
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    source = load_json(args.source)
    for value in args.end_date:
        end = dt.date.fromisoformat(value)
        path = args.output_dir / f"weekly_shadow_backfill_{end.strftime('%Y%m%d')}.json"
        write_json(path, checkpoint(source, end))
        print(path)


if __name__ == "__main__":
    main()
