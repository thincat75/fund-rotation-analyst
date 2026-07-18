#!/usr/bin/env python3
"""Cross-check shadow Tushare datasets and update per-dataset promotion eligibility."""

from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path
from typing import Any

from data_access import load_json, parse_number, write_json


STYLE_NAMES = {
    "000300": "沪深300", "000016": "上证50", "000905": "中证500", "000852": "中证1000",
    "399006": "创业板指", "000688": "科创50", "399370": "国证成长", "399371": "国证价值", "000922": "中证红利",
}


def _find_key(row: dict[str, Any], tokens: list[str]) -> str | None:
    return next((str(key) for token in tokens for key in row if token.lower() in str(key).lower()), None)


def series_map(rows: list[dict[str, Any]], value_tokens: list[str], cutoff: str | None = None) -> dict[str, float]:
    if not rows:
        return {}
    date_key = _find_key(rows[0], ["净值日期", "日期", "trade_date", "date"])
    value_key = _find_key(rows[0], value_tokens)
    if not date_key or not value_key:
        return {}
    output = {}
    for row in rows:
        day = str(row.get(date_key) or "")[:10].replace("-", "")
        value = parse_number(row.get(value_key))
        cutoff_key = str(cutoff or "").replace("-", "")
        if day and value is not None and (not cutoff_key or day <= cutoff_key):
            output[day] = value
    return output


def latest_relative_diff(left: dict[str, float], right: dict[str, float]) -> float | None:
    common = sorted(set(left) & set(right))
    if not common:
        return None
    day = common[-1]
    if right[day] == 0:
        return None
    return abs(left[day] / right[day] - 1)


def top_stock_codes(rows: list[dict[str, Any]]) -> set[str]:
    output = set()
    for row in rows[:10]:
        key = _find_key(row, ["股票代码", "symbol", "stk_code"])
        value = str(row.get(key) or "").split(".")[0] if key else ""
        if value:
            output.add(value.zfill(6))
    return output


def valid_shadow(payload: dict[str, Any]) -> bool:
    return (payload.get("provider_shadow") or {}).get("provider") in {
        "Tushare Pro 官方",
        "第三方 Tushare 兼容代理",
        "第三方 Tushare 代理",
    }


def collect_checks(shadows: list[dict[str, Any]]) -> dict[str, Any]:
    checks: dict[str, list[dict[str, Any]]] = {name: [] for name in ["fund_nav", "style_indexes", "etf_history", "fund_portfolio", "margin_summary", "market_daily_info"]}
    distinct_days = set()
    validation_modes = set()
    for payload in shadows:
        if not valid_shadow(payload):
            continue
        week = payload.get("week") or {}
        if payload.get("shadow_validation_mode") == "historical_backfill":
            validation_modes.add("historical_backfill")
            distinct_days.add(week.get("end_date"))
        else:
            validation_modes.add("live_daily")
            distinct_days.add(week.get("collection_trade_date") or payload.get("as_of", "")[:10])
        cutoff = week.get("end_date")
        proxy_data = (payload.get("provider_shadow") or {}).get("datasets") or {}
        for key, rows in proxy_data.items():
            if key.startswith("fund_nav:"):
                code = key.split(":", 1)[1]
                old = (((payload.get("funds") or {}).get(code) or {}).get("nav") or [])
                diff = latest_relative_diff(
                    series_map(rows, ["累计净值"], cutoff),
                    series_map(old, ["累计净值", "复权净值", "单位净值"], cutoff),
                )
                checks["fund_nav"].append({"code": code, "difference": diff, "pass": diff is not None and diff <= 0.001})
            elif key.startswith("style_index:"):
                symbol = key.split(":", 1)[1]
                old = ((payload.get("market") or {}).get("style_indexes") or {}).get(STYLE_NAMES.get(symbol), [])
                diff = latest_relative_diff(series_map(rows, ["收盘", "close"], cutoff), series_map(old, ["收盘", "close"], cutoff))
                checks["style_indexes"].append({"symbol": symbol, "difference": diff, "pass": diff is not None and diff <= 0.002})
            elif key.startswith("etf_return:"):
                code = key.split(":", 1)[1]
                candidate_etfs = payload.get("candidate_etfs") or {}
                old = ((((candidate_etfs.get("history") or {}).get(code) or {}).get("none") or [])
                       or ((candidate_etfs.get("history_sina") or {}).get(code) or []))
                proxy_none = (rows or {}).get("none") or []
                diff = latest_relative_diff(series_map(proxy_none, ["原始收盘", "收盘", "close"], cutoff), series_map(old, ["收盘", "close"], cutoff))
                checks["etf_history"].append({"code": code, "difference": diff, "pass": diff is not None and diff <= 0.002})
            elif key.startswith("fund_portfolio:"):
                code = key.split(":", 1)[1]
                old = (((payload.get("full_details") or {}).get(code) or {}).get("stock_holdings") or [])
                proxy_codes, old_codes = top_stock_codes(rows), top_stock_codes(old)
                denominator = min(len(proxy_codes), len(old_codes))
                overlap = len(proxy_codes & old_codes) / denominator if denominator else None
                checks["fund_portfolio"].append({"code": code, "top10_overlap": overlap, "pass": overlap is not None and overlap >= 0.8})
            elif key.startswith("margin_summary:"):
                exchange = key.split(":", 1)[1]
                old = ((((payload.get("market") or {}).get("margin_raw") or {}).get("exchanges") or {}).get(exchange) or [])
                diff = latest_relative_diff(
                    series_map(rows, ["margin_balance", "rzrqye", "融资融券余额"], cutoff),
                    series_map(old, ["margin_balance", "rzrqye", "融资融券余额"], cutoff),
                )
                checks["margin_summary"].append({"exchange": exchange, "difference": diff, "pass": diff is not None and diff <= 0.001})
            elif key.startswith("market_daily_info:"):
                exchange = key.split(":", 1)[1]
                old = ((((payload.get("market") or {}).get("margin_raw") or {}).get("market_daily") or {}).get(exchange) or [])
                market_cap_diff = latest_relative_diff(
                    series_map(rows, ["float_market_cap", "float_mv", "流通市值"], cutoff),
                    series_map(old, ["float_market_cap", "float_mv", "流通市值"], cutoff),
                )
                turnover_diff = latest_relative_diff(
                    series_map(rows, ["market_turnover", "amount", "成交金额"], cutoff),
                    series_map(old, ["market_turnover", "amount", "成交金额"], cutoff),
                )
                available = [value for value in (market_cap_diff, turnover_diff) if value is not None]
                maximum = max(available) if len(available) == 2 else None
                checks["market_daily_info"].append({
                    "exchange": exchange,
                    "market_cap_difference": market_cap_diff,
                    "turnover_difference": turnover_diff,
                    "difference": maximum,
                    "pass": maximum is not None and maximum <= 0.002,
                })
    checks["distinct_shadow_dates"] = sorted(day for day in distinct_days if day)
    checks["validation_modes"] = sorted(validation_modes)
    return checks


def promote(health: dict[str, Any], checks: dict[str, Any]) -> dict[str, Any]:
    datasets = health.get("datasets") or {}
    three_days = len(checks.get("distinct_shadow_dates") or []) >= 3
    historical_only = set(checks.get("validation_modes") or []) == {"historical_backfill"}
    rules = {
        "fund_nav": (checks.get("fund_nav") or [], three_days, "同日净值差不超过0.1%，且覆盖3个交易日"),
        "style_indexes": (checks.get("style_indexes") or [], three_days, "同日指数差不超过0.2%，且覆盖3个交易日"),
        "fund_portfolio": (checks.get("fund_portfolio") or [], True, "前十大股票代码重合率至少80%"),
        "margin_summary": (checks.get("margin_summary") or [], three_days, "沪深交易所同日两融余额差不超过0.1%，且覆盖3个交易日"),
        "market_daily_info": (checks.get("market_daily_info") or [], three_days, "沪深同日流通市值和成交额差不超过0.2%，且覆盖3个交易日"),
    }
    for dataset, (rows, date_gate, basis) in rules.items():
        target = datasets.get(dataset)
        if not target:
            continue
        passed = bool(rows and all(row.get("pass") for row in rows) and date_gate and target.get("operational_eligible"))
        target["crosscheck_status"] = "passed_historical_backfill_pending_live_days" if passed and historical_only else "passed" if passed else "failed_or_incomplete"
        target["crosscheck_basis"] = basis
        target["crosscheck_samples"] = rows
        target["provisional_promotion_eligible"] = passed
        target["promotion_eligible"] = bool(passed and not historical_only)
        key_field = "code" if dataset in {"fund_nav", "fund_portfolio"} else "exchange" if dataset in {"margin_summary", "market_daily_info"} else "symbol"
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            if row.get(key_field):
                grouped.setdefault(str(row[key_field]), []).append(row)
        for identifier, samples in grouped.items():
            item_passed = bool(samples and all(row.get("pass") for row in samples) and date_gate and target.get("operational_eligible"))
            item_key = f"{dataset}:{identifier}"
            datasets[item_key] = {
                **{key: value for key, value in target.items() if key not in {"crosscheck_samples", "promotion_eligible", "provisional_promotion_eligible"}},
                "dataset": item_key,
                "crosscheck_status": "passed_historical_backfill_pending_live_days" if item_passed and historical_only else "passed" if item_passed else "failed_or_incomplete",
                "crosscheck_basis": basis,
                "crosscheck_samples": samples,
                "provisional_promotion_eligible": item_passed,
                "promotion_eligible": bool(item_passed and not historical_only),
            }
    etf_rows = checks.get("etf_history") or []
    etf_passed = bool(etf_rows and all(row.get("pass") for row in etf_rows) and three_days)
    for dataset in ["fund_daily", "fund_adj"]:
        target = datasets.get(dataset)
        if target:
            passed = bool(etf_passed and target.get("operational_eligible"))
            target["crosscheck_status"] = "passed_historical_backfill_pending_live_days" if passed and historical_only else "passed" if passed else "failed_or_incomplete"
            target["crosscheck_basis"] = "ETF同日收盘价差不超过0.2%，且覆盖3个交易日"
            target["crosscheck_samples"] = etf_rows
            target["provisional_promotion_eligible"] = passed
            target["promotion_eligible"] = bool(passed and not historical_only)
    grouped_etfs: dict[str, list[dict[str, Any]]] = {}
    for row in etf_rows:
        if row.get("code"):
            grouped_etfs.setdefault(str(row["code"]), []).append(row)
    operational_etf = bool((datasets.get("fund_daily") or {}).get("operational_eligible") and (datasets.get("fund_adj") or {}).get("operational_eligible"))
    for code, samples in grouped_etfs.items():
        passed = bool(samples and all(row.get("pass") for row in samples) and three_days and operational_etf)
        datasets[f"etf_return:{code}"] = {
            "dataset": f"etf_return:{code}", "provider": health.get("provider"), "transport": health.get("transport"),
            "operational_eligible": operational_etf,
            "crosscheck_status": "passed_historical_backfill_pending_live_days" if passed and historical_only else "passed" if passed else "failed_or_incomplete",
            "crosscheck_basis": "ETF同日收盘价差不超过0.2%，且覆盖3个交易日",
            "crosscheck_samples": samples,
            "provisional_promotion_eligible": passed,
            "promotion_eligible": bool(passed and not historical_only),
        }
    health["shadow_validation"] = {
        "validated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "distinct_shadow_dates": checks.get("distinct_shadow_dates") or [],
        "checks": checks,
    }
    foundation = [row for row in datasets.values() if row.get("required_for_foundation")]
    health["foundation_ready"] = bool(foundation and all(row.get("promotion_eligible") for row in foundation))
    return health


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--health", required=True, type=Path)
    parser.add_argument("--shadow", required=True, action="append", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    health = load_json(args.health)
    shadows = [load_json(path) for path in args.shadow]
    checks = collect_checks(shadows)
    write_json(args.output, promote(health, checks))
    print(f"SHADOW VALIDATION: {len(checks.get('distinct_shadow_dates') or [])} distinct dates -> {args.output}")


if __name__ == "__main__":
    main()
