#!/usr/bin/env python3
"""Probe every public interface used by the fund analysis skill."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io
import statistics
import time
from pathlib import Path
from typing import Any, Callable

from collect_weekly_data import eastmoney_sector_flow_compat, eastmoney_sector_flow_history_compat, parse_day
from data_access import df_to_records, wall_clock_timeout, write_json


def specs(end_date: str) -> list[dict[str, Any]]:
    start_date = (parse_day(end_date) - dt.timedelta(days=20)).strftime("%Y%m%d")
    compact_end = end_date.replace("-", "")
    return [
        {"group": "calendar", "name": "tool_trade_date_hist_sina", "kwargs": {}},
        {"group": "fund", "name": "fund_name_em", "kwargs": {}},
        {"group": "fund", "name": "fund_open_fund_info_em", "kwargs": {"symbol": "001170", "indicator": "累计净值走势"}, "fresh_after": end_date},
        {"group": "fund", "name": "fund_etf_fund_info_em", "kwargs": {"fund": "560780"}, "fresh_after": end_date},
        {"group": "profile", "name": "fund_individual_basic_info_xq", "kwargs": {"symbol": "001170"}},
        {"group": "profile", "name": "fund_info_ths", "kwargs": {"symbol": "001170"}},
        {"group": "profile", "name": "fund_individual_detail_hold_xq", "kwargs": {"symbol": "001170"}},
        {"group": "profile", "name": "fund_portfolio_hold_em", "variant": "current_year", "kwargs": {"symbol": "001170", "date": str(parse_day(end_date).year)}},
        {"group": "profile", "name": "fund_portfolio_hold_em", "variant": "previous_year", "kwargs": {"symbol": "001170", "date": str(parse_day(end_date).year - 1)}},
        {"group": "profile", "name": "fund_portfolio_hold_em", "variant": "default", "kwargs": {"symbol": "001170"}},
        {"group": "profile", "name": "fund_portfolio_hold_em", "variant": "other_fund", "kwargs": {"symbol": "005844", "date": str(parse_day(end_date).year)}},
        {"group": "profile", "name": "fund_portfolio_industry_allocation_em", "kwargs": {"symbol": "001170", "date": str(parse_day(end_date).year)}},
        {"group": "ranking", "name": "fund_open_fund_rank_em", "kwargs": {"symbol": "全部"}},
        {"group": "index", "name": "index_zh_a_hist", "kwargs": {"symbol": "000300", "period": "daily", "start_date": start_date, "end_date": compact_end}, "fresh_after": end_date},
        {"group": "index", "name": "stock_zh_index_hist_csindex", "kwargs": {"symbol": "000922", "start_date": start_date, "end_date": compact_end}, "fresh_after": end_date},
        {"group": "index", "name": "stock_zh_index_daily_tx", "kwargs": {"symbol": "sh000300", "start_date": start_date, "end_date": compact_end}, "fresh_after": end_date},
        {"group": "index", "name": "stock_zh_index_daily", "kwargs": {"symbol": "sh000300"}, "fresh_after": end_date},
        {"group": "sector", "name": "stock_board_industry_name_em", "kwargs": {}},
        {"group": "sector", "name": "stock_board_concept_name_em", "kwargs": {}},
        {"group": "sector", "name": "stock_board_industry_summary_ths", "kwargs": {}},
        *[
            {"group": "sector_flow", "name": "stock_sector_fund_flow_rank", "variant": f"{sector_type}:{indicator}", "kwargs": {"indicator": indicator, "sector_type": sector_type}}
            for indicator in ["今日", "5日", "10日"]
            for sector_type in ["行业资金流", "概念资金流"]
        ],
        {"group": "sector_flow", "name": "stock_sector_fund_flow_hist", "kwargs": {"symbol": "油田服务"}, "fresh_after": end_date},
        {"group": "stock_flow", "name": "stock_individual_fund_flow", "kwargs": {"stock": "300308", "market": "sz"}},
        {"group": "etf", "name": "fund_etf_spot_em", "kwargs": {}},
        {"group": "etf", "name": "fund_etf_category_sina", "kwargs": {"symbol": "ETF基金"}},
        {"group": "etf", "name": "fund_etf_spot_ths", "kwargs": {}},
        {"group": "etf", "name": "fund_etf_hist_em", "kwargs": {"symbol": "560780", "period": "daily", "start_date": start_date, "end_date": compact_end, "adjust": "hfq"}, "fresh_after": end_date},
        {"group": "etf", "name": "fund_etf_hist_sina", "kwargs": {"symbol": "sh560780"}, "fresh_after": end_date},
        *[
            {
                "group": "compat", "name": "eastmoney_sector_flow_compat", "variant": f"{sector_type}:{indicator}",
                "custom": lambda period=indicator, kind=sector_type: eastmoney_sector_flow_compat(period, kind),
            }
            for indicator in ["今日", "5日", "10日"]
            for sector_type in ["行业资金流", "概念资金流"]
        ],
        {"group": "compat", "name": "eastmoney_sector_flow_history_compat", "variant": "油田服务", "custom": lambda: eastmoney_sector_flow_history_compat("油田服务"), "fresh_after": end_date},
    ]


def latest_date(records: list[dict[str, Any]]) -> str | None:
    dates = []
    for row in records:
        for key, value in row.items():
            if any(token in str(key).lower() for token in ["日期", "date", "时间"]):
                day = parse_day(value)
                if day:
                    dates.append(day)
    return max(dates).isoformat() if dates else None


def call_once(ak: Any, spec: dict[str, Any], timeout: int) -> dict[str, Any]:
    started = time.monotonic()
    try:
        function: Callable[..., Any] | None = spec.get("custom") or getattr(ak, spec["name"], None)
        if function is None:
            return {"status": "missing_function", "elapsed_seconds": 0.0, "rows": 0, "error": "AkShare function not found"}
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with wall_clock_timeout(timeout) as enforced:
                value = function() if spec.get("custom") else function(**spec.get("kwargs", {}))
        records = value if isinstance(value, list) else df_to_records(value)
        newest = latest_date(records)
        status = "ok" if records else "empty"
        fresh_after = parse_day(spec.get("fresh_after"))
        if status == "ok" and fresh_after and newest and parse_day(newest) < fresh_after:
            status = "stale"
        return {
            "status": status,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "rows": len(records),
            "latest_date": newest,
            "timeout_enforced": enforced,
            "error": None,
        }
    except Exception as exc:
        return {
            "status": "timeout" if type(exc).__name__ == "CallTimeout" else "error",
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "rows": 0,
            "latest_date": None,
            "error": f"{type(exc).__name__}: {exc}",
        }


def classify(attempts: list[dict[str, Any]]) -> str:
    statuses = [row["status"] for row in attempts]
    if all(status == "ok" for status in statuses):
        elapsed = [row["elapsed_seconds"] for row in attempts]
        return "slow" if max(elapsed) >= 8 else "stable"
    if all(status == "stale" for status in statuses):
        return "stale"
    if len(set(statuses)) > 1:
        return "unstable"
    if all(status == "empty" for status in statuses):
        return "empty"
    return "failed"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=12)
    parser.add_argument("--end-date", default=dt.date.today().isoformat())
    parser.add_argument("--group", action="append", help="Only run one or more named interface groups")
    parser.add_argument("--interface", action="append", help="Only run one or more named interfaces")
    args = parser.parse_args()

    import akshare as ak

    results = []
    selected_specs = [
        spec for spec in specs(args.end_date)
        if (not args.group or spec["group"] in set(args.group))
        and (not args.interface or spec["name"] in set(args.interface))
    ]
    for spec in selected_specs:
        attempts = [call_once(ak, spec, args.timeout) for _ in range(args.rounds)]
        elapsed = [row["elapsed_seconds"] for row in attempts]
        results.append({
            "group": spec["group"],
            "interface": spec["name"],
            "variant": spec.get("variant"),
            "kwargs": spec.get("kwargs") or {},
            "classification": classify(attempts),
            "success_rate": round(sum(row["status"] == "ok" for row in attempts) / len(attempts), 3),
            "median_seconds": round(statistics.median(elapsed), 3),
            "max_seconds": round(max(elapsed), 3),
            "attempts": attempts,
        })
    summary = {name: sum(row["classification"] == name for row in results) for name in ["stable", "slow", "unstable", "stale", "empty", "failed"]}
    write_json(args.output, {
        "tested_at": dt.datetime.now().isoformat(timespec="seconds"),
        "akshare_version": getattr(ak, "__version__", None),
        "rounds": args.rounds,
        "timeout_seconds": args.timeout,
        "end_date": args.end_date,
        "summary": summary,
        "results": results,
    })
    print(summary)


if __name__ == "__main__":
    main()
