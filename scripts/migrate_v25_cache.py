#!/usr/bin/env python3
"""Import validated v2.5 weekly artifacts into the v2.6 SQLite cache."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from cache_store import CacheStore
from data_access import load_json


def sector_symbol(row: dict[str, Any], kind: str) -> str | None:
    code = row.get("ts_code")
    name = row.get("industry") if kind == "industry" else row.get("name")
    name = name or row.get("name") or row.get("industry")
    if code:
        return str(code)
    return f"tushare:{kind}:{name}" if name else None


def import_weekly_payload(store: CacheStore, payload: dict[str, Any]) -> dict[str, int]:
    counts = {"fund_nav": 0, "style_index": 0, "ranking_snapshot": 0}
    for code, content in (payload.get("funds") or {}).items():
        counts["fund_nav"] += store.upsert_series("AkShare", "fund_nav", code, content.get("nav") or [])
    for name, rows in ((payload.get("market") or {}).get("style_indexes") or {}).items():
        counts["style_index"] += store.upsert_series("AkShare及备用源", "style_index", name, rows)
    for group, rows in (payload.get("rankings") or {}).items():
        by_date: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            date = str(row.get("日期") or "")[:10]
            if date:
                by_date.setdefault(date, []).append(row)
        for date, dated_rows in by_date.items():
            store.put_snapshot("AkShare", "fund_ranking", group, date, dated_rows)
            counts["ranking_snapshot"] += len(dated_rows)
    return counts


def import_tushare_flow_files(store: CacheStore, roots: list[Path]) -> dict[str, int]:
    counts = {"industry_flow_daily": 0, "concept_flow_daily": 0}
    seen: set[Path] = set()
    for root in roots:
        for path in root.rglob("tushare_moneyflow_*_ths_*.json") if root.exists() else []:
            if path in seen:
                continue
            seen.add(path)
            rows = load_json(path)
            if not isinstance(rows, list):
                continue
            kind = "industry" if "moneyflow_ind_ths" in path.name else "concept"
            dataset = f"{kind}_flow_daily"
            grouped: dict[str, list[dict[str, Any]]] = {}
            for row in rows:
                symbol = sector_symbol(row, kind)
                if symbol and row.get("trade_date"):
                    grouped.setdefault(symbol, []).append(row)
            for symbol, values in grouped.items():
                counts[dataset] += store.upsert_series("第三方 Tushare 代理", dataset, symbol, values)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weekly-data", action="append", type=Path, default=[])
    parser.add_argument("--legacy-cache", action="append", type=Path, default=[])
    parser.add_argument("--cache-root", type=Path, default=Path("work/cache/fund-rotation"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    summary: dict[str, Any] = {"weekly_files": [], "flow_roots": [str(path) for path in args.legacy_cache]}
    with CacheStore(args.cache_root) as store:
        totals: dict[str, int] = {}
        for path in args.weekly_data:
            counts = import_weekly_payload(store, load_json(path))
            summary["weekly_files"].append({"path": str(path), "counts": counts})
            for key, value in counts.items():
                totals[key] = totals.get(key, 0) + value
        flow_counts = import_tushare_flow_files(store, args.legacy_cache)
        for key, value in flow_counts.items():
            totals[key] = totals.get(key, 0) + value
        summary["totals"] = totals
        summary["database"] = str(store.path)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
