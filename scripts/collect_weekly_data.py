#!/usr/bin/env python3
"""Collect auditable weekly fund, sector, style, ranking, and ETF data."""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import os
import re
from pathlib import Path
from typing import Any

from data_access import (
    AkshareClient,
    dataset_status,
    holdings_metadata,
    holdings_hash,
    load_json,
    normalize_holdings,
    parse_number,
    unresolved_warnings,
    write_json,
)
from cache_store import CacheStore, stable_hash
from margin_leverage import (
    COMPARABLE_START,
    MIN_PERCENTILE_SAMPLE,
    POLICY_EVENTS,
    normalize_daily_info_rows,
    normalize_exchange_market_snapshot,
    normalize_margin_rows,
    percentile_rank,
)
from tushare_proxy import (
    LEGACY_THIRD_PARTY_PROVIDER,
    OFFICIAL_PROVIDER,
    PROVIDER as TUSHARE_PROVIDER,
    THIRD_PARTY_PROVIDER,
    TushareProxyClient,
    adjusted_etf_history,
    aggregate_sector_flow,
    collect_fund_master,
    create_pro,
    health_source_matches,
    load_health,
    market_ts_code,
    normalize_fund_nav,
    normalize_fund_portfolio,
    promotion_eligible,
    resolve_fund_ts_code,
)


SCHEMA_VERSION = 2
DATA_REVISION = "2.8"
STYLE_INDEXES = {
    "中证全指": {"primary": "000985", "fallback": "sh000985"},
    "沪深300": {"primary": "000300", "fallback": "sh000300"},
    "上证50": {"primary": "000016", "fallback": "sh000016"},
    "中证500": {"primary": "000905", "fallback": "sh000905"},
    "中证1000": {"primary": "000852", "fallback": "sh000852"},
    "创业板指": {"primary": "399006", "fallback": "sz399006"},
    "科创50": {"primary": "000688", "fallback": "sh000688"},
    "国证成长": {"primary": "399370", "fallback": "sz399370"},
    "国证价值": {"primary": "399371", "fallback": "sz399371"},
    "中证红利": {"primary": "000922", "fallback": "sh000922"},
}
DEFAULT_CANDIDATE_ETFS = ["560780", "562590", "159516", "159558"]
ETF_FEEDERS = {"560780": "020639", "562590": "020356"}
ETF_CHANNELS = {
    "560780": {"channel": "沪股通", "market": "SSE", "verified_at": "2026-06-29"},
    "562590": {"channel": "沪股通", "market": "SSE", "verified_at": "2026-06-29"},
    "159516": {"channel": "深股通", "market": "SZSE", "verified_at": "2026-06-29"},
    "159558": {"channel": "深股通", "market": "SZSE", "verified_at": "2026-06-29"},
}


def import_akshare() -> tuple[Any | None, str | None]:
    try:
        import akshare as ak  # type: ignore

        return ak, None
    except Exception as exc:
        return None, f"akshare unavailable: {exc}"


def create_tushare_client(args: argparse.Namespace, cache_root: Path, context: dict[str, Any]) -> tuple[TushareProxyClient | None, dict[str, Any]]:
    """Create an optional official/proxy client without making missing credentials a report defect."""
    health = load_health(Path(args.tushare_health))
    if args.provider_policy == "akshare-only":
        return None, health
    try:
        pro, ts_module, metadata = create_pro()
    except Exception as exc:
        health = dict(health)
        health["runtime_unavailable"] = f"{type(exc).__name__}: {exc}"
        return None, health
    if health and not health_source_matches(health, metadata):
        health = dict(health)
        health["datasets"] = {}
        health["source_mismatch"] = "健康文件来自不同的Tushare来源；请重新运行健康检查和shadow。"
    return (
        TushareProxyClient(
            pro,
            ts_module,
            cache_root / "tushare",
            metadata,
            timeout=args.timeout,
            retries=args.retries,
            refresh=args.refresh,
            context=context,
        ),
        health,
    )


def proxy_enabled(policy: str, health: dict[str, Any], dataset: str) -> bool:
    return policy == "shadow" or (policy == "auto" and promotion_eligible(health, dataset))


def proxy_enabled_for(policy: str, health: dict[str, Any], specific: str, generic: str) -> bool:
    return policy == "shadow" or (policy == "auto" and (promotion_eligible(health, specific) or promotion_eligible(health, generic)))


def health_crosscheck(health: dict[str, Any], dataset: str) -> str:
    return str(((health.get("datasets") or {}).get(dataset) or {}).get("crosscheck_status") or "not_recorded")


def health_crosscheck_for(health: dict[str, Any], specific: str, generic: str) -> str:
    datasets = health.get("datasets") or {}
    row = datasets.get(specific) or datasets.get(generic) or {}
    return str(row.get("crosscheck_status") or "not_recorded")


def normalized_tushare_status(
    dataset: str,
    statuses: list[dict[str, Any]],
    basis: str,
    source_date: str | None = None,
    *,
    provider: str = TUSHARE_PROVIDER,
    transport: str = "http",
) -> dict[str, Any]:
    logical = dataset_status(dataset, statuses, basis=basis, source_date=source_date)
    logical.update(
        {
            "provider": provider,
            "transport": transport,
            "endpoint_fingerprint": next((row.get("endpoint_fingerprint") for row in statuses if row.get("endpoint_fingerprint")), None),
            "crosscheck_status": "pending_shadow_crosscheck",
            "promotion_eligible": logical["status"] in {"ok", "fallback_used"},
        }
    )
    return logical


def replace_dataset_status(datasets: list[dict[str, Any]], replacement: dict[str, Any]) -> None:
    datasets[:] = [row for row in datasets if row.get("dataset") != replacement.get("dataset")]
    datasets.append(replacement)


def normalize_tushare_index(
    rows: list[dict[str, Any]], cutoff: str, *, provider: str = TUSHARE_PROVIDER
) -> list[dict[str, Any]]:
    output = []
    for row in rows:
        day = str(row.get("trade_date") or "").replace("-", "")
        close = parse_number(row.get("close"))
        if not day or close is None or day > cutoff.replace("-", ""):
            continue
        output.append({"日期": f"{day[:4]}-{day[4:6]}-{day[6:8]}", "收盘": close, "provider": provider})
    return sorted(output, key=lambda row: row["日期"])


def normalized_tushare_holdings(
    rows: list[dict[str, Any]], stock_names: dict[str, str], *, provider: str = TUSHARE_PROVIDER
) -> list[dict[str, Any]]:
    output = []
    for row in rows:
        symbol = str(row.get("symbol") or row.get("stk_code") or "")
        output.append(
            {
                "股票代码": symbol,
                "股票名称": stock_names.get(symbol) or stock_names.get(symbol.split(".")[0]) or "名称待补",
                "持仓占比": parse_number(row.get("stk_mkv_ratio")),
                "持股数": parse_number(row.get("amount")),
                "持股市值": parse_number(row.get("mkv")),
                "报告期": row.get("end_date"),
                "公告日期": row.get("ann_date"),
                "provider": provider,
            }
        )
    return output


def parse_day(value: Any) -> dt.date | None:
    if value is None:
        return None
    text = str(value)[:10]
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def weekday_calendar(start: dt.date, end: dt.date) -> list[dt.date]:
    days = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += dt.timedelta(days=1)
    return days


def extract_trade_dates(rows: list[dict[str, Any]]) -> list[dt.date]:
    dates = []
    for row in rows:
        for key, value in row.items():
            if "date" in str(key).lower() or "日期" in str(key):
                day = parse_day(value)
                if day:
                    dates.append(day)
                    break
    return sorted(set(dates))


def resolve_week(trade_dates: list[dt.date], today: dt.date, explicit_end: dt.date | None) -> dict[str, Any]:
    if explicit_end:
        anchor = explicit_end
        monday = anchor - dt.timedelta(days=anchor.weekday())
        cutoff = anchor
        period_mode = "explicit"
        completeness = "complete" if anchor.weekday() >= 4 else "partial"
    else:
        if today.weekday() >= 5:
            monday = today - dt.timedelta(days=today.weekday())
        else:
            monday = today - dt.timedelta(days=today.weekday() + 7)
        cutoff = monday + dt.timedelta(days=6)
        period_mode = "completed"
        completeness = "complete"

    sunday = monday + dt.timedelta(days=6)
    eligible = [day for day in trade_dates if monday <= day <= min(sunday, cutoff)]
    if not eligible:
        eligible = weekday_calendar(monday, min(sunday, cutoff))
    if not eligible:
        raise ValueError("no trading dates are available for the requested week")
    start_date = eligible[0]
    end_date = eligible[-1]
    baseline_candidates = [day for day in trade_dates if day < start_date]
    baseline = baseline_candidates[-1] if baseline_candidates else start_date - dt.timedelta(days=3 if start_date.weekday() == 0 else 1)
    return {
        "period_mode": period_mode,
        "completeness": completeness,
        "requested_end_date": explicit_end.isoformat() if explicit_end else None,
        "baseline_date": baseline.isoformat(),
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "calendar_source": "akshare" if trade_dates else "weekday_fallback",
    }


def collect_trade_calendar(client: AkshareClient, today: dt.date, explicit_end: dt.date | None) -> tuple[list[dt.date], str]:
    anchor = explicit_end or today
    rows = client.call(
        "A-share trading calendar",
        "tool_trade_date_hist_sina",
        [{}],
        key_extra={"years": [anchor.year - 1, anchor.year]},
    )
    dates = extract_trade_dates(rows)
    if dates:
        return dates, "akshare"
    return weekday_calendar(dt.date(anchor.year - 1, 1, 1), dt.date(anchor.year, 12, 31)), "weekday_fallback"


def index_records_cover_week(records: list[dict[str, Any]], week: dict[str, Any]) -> tuple[bool, str, str | None]:
    dates = extract_trade_dates(records)
    baseline = parse_day(week.get("baseline_date"))
    end = parse_day(week.get("end_date"))
    if not dates or not baseline or not end:
        return False, "无法解析指数日期", dates[-1].isoformat() if dates else None
    baseline_candidates = [day for day in dates if day <= baseline]
    if not baseline_candidates or (baseline - baseline_candidates[-1]).days > 7:
        return False, "缺少报告基准日附近数据", dates[-1].isoformat()
    if end not in dates:
        return False, f"最新日期{dates[-1].isoformat()}未覆盖报告结束日{end.isoformat()}", dates[-1].isoformat()
    return True, "周期覆盖完整", dates[-1].isoformat()


def index_close_on(records: list[dict[str, Any]], target: str) -> float | None:
    target_day = parse_day(target)
    for row in reversed(records):
        row_day = next((parse_day(value) for key, value in row.items() if "date" in str(key).lower() or "日期" in str(key)), None)
        if row_day == target_day:
            key = next((key for key in row if any(token in str(key).lower() for token in ["close", "收盘"])), None)
            return parse_number(row.get(key)) if key else None
    return None


def collect_style_indexes(
    client: AkshareClient,
    week: dict[str, Any],
    datasets: list[dict[str, Any]],
    store: CacheStore | None = None,
    refresh_datasets: set[str] | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    output: dict[str, list[dict[str, Any]]] = {}
    metadata: dict[str, dict[str, Any]] = {}
    start = (parse_day(week["baseline_date"]) - dt.timedelta(days=95)).strftime("%Y%m%d")
    end = week["end_date"].replace("-", "")
    refresh_datasets = refresh_datasets or set()
    for name, symbols in STYLE_INDEXES.items():
        status_start = len(client.statuses)
        records: list[dict[str, Any]] = []
        resolved_source = None
        latest_date = None
        if store and "style_index" not in refresh_datasets and f"style_index:{symbols['primary']}" not in refresh_datasets:
            cached = store.get_series("style_index", name, end_date=week["end_date"])
            valid, _, cached_latest = index_records_cover_week(cached, week) if cached else (False, "", None)
            if valid:
                records, resolved_source, latest_date = cached, "sqlite_incremental_cache", cached_latest
                datasets.append({
                    "dataset": f"style_index:{symbols['primary']}", "attempted_sources": ["sqlite_incremental_cache"],
                    "resolved_by": "sqlite_incremental_cache", "status": "ok", "basis": "共享历史序列缓存",
                    "source_date": latest_date, "stale_days": 0, "record_count": len(records), "reason": None,
                    "provider": "本地增量缓存", "transport": "sqlite", "cache_hit": True,
                })
                output[name] = records
                metadata[name] = {
                    "return_basis": "指数历史收盘价", "resolved_source": resolved_source,
                    "source_latest_date": latest_date, "freshness_status": "周期完整", "crosscheck_source": None,
                }
                continue
        source_chain = [
            ("index_zh_a_hist", [{"symbol": symbols["primary"], "period": "daily", "start_date": start, "end_date": end}]),
        ]
        if name in {"中证全指", "沪深300", "中证500", "中证1000", "中证红利"}:
            source_chain.append(
                ("stock_zh_index_hist_csindex", [{"symbol": symbols["primary"], "start_date": start, "end_date": end}])
            )
        if name == "中证红利":
            source_chain.extend([
                ("stock_zh_index_daily_tx", [{"symbol": symbols["fallback"], "start_date": start, "end_date": end}]),
                ("stock_zh_index_daily", [{"symbol": symbols["fallback"]}]),
            ])
        else:
            source_chain.extend([
                ("stock_zh_index_daily_tx", [{"symbol": symbols["fallback"], "start_date": start, "end_date": end}]),
                ("stock_zh_index_daily", [{"symbol": symbols["fallback"]}]),
            ])
        for function_name, variants in source_chain:
            candidate = client.call(f"{name} index {function_name}", function_name, variants, key_extra=week)
            if not candidate:
                continue
            valid, reason, candidate_latest = index_records_cover_week(candidate, week)
            if valid:
                records, resolved_source, latest_date = candidate, function_name, candidate_latest
                break
            client.statuses[-1]["status"] = "stale_source"
            client.statuses[-1]["reason"] = reason
        conflict = False
        crosscheck_source = None
        if name == "中证红利" and records and resolved_source != "stock_zh_index_daily_tx":
            tx_rows = client.call(
                f"{name} index tx crosscheck", "stock_zh_index_daily_tx",
                [{"symbol": symbols["fallback"], "start_date": start, "end_date": end}], key_extra={**week, "crosscheck": True},
            )
            valid, _, _ = index_records_cover_week(tx_rows, week) if tx_rows else (False, "", None)
            if valid:
                selected_close = index_close_on(records, week["end_date"])
                tx_close = index_close_on(tx_rows, week["end_date"])
                crosscheck_source = "stock_zh_index_daily_tx"
                if selected_close and tx_close and abs(selected_close / tx_close - 1) > 0.002:
                    conflict = True
                client.statuses[-1]["status"] = "crosscheck_conflict" if conflict else "crosscheck_ok"
        output[name] = records
        if store and records:
            store.upsert_series("AkShare及公开备用源", "style_index", name, records)
        logical = dataset_status(f"style_index:{symbols['primary']}", client.statuses[status_start:], basis="周收益历史收盘价", source_date=week["end_date"])
        if conflict:
            logical["status"] = "partial"
            logical["reason"] = "主源与腾讯结束日收盘价偏差超过0.2%"
        datasets.append(logical)
        metadata[name] = {
            "return_basis": "指数历史收盘价",
            "resolved_source": resolved_source,
            "source_latest_date": latest_date,
            "freshness_status": "数据冲突" if conflict else "周期完整" if records else "数据不足",
            "crosscheck_source": crosscheck_source,
        }
    return output, metadata


def _cached_series_complete(rows: list[dict[str, Any]], cutoff: str, *, minimum_rows: int = 1) -> bool:
    dates = extract_trade_dates(rows)
    return bool(len(rows) >= minimum_rows and dates and dates[-1].isoformat() >= cutoff)


def collect_margin_leverage_data(
    client: AkshareClient,
    proxy_client: TushareProxyClient | None,
    proxy_health: dict[str, Any],
    policy: str,
    store: CacheStore,
    datasets: list[dict[str, Any]],
    week: dict[str, Any],
    margin_mode: str,
    refresh_datasets: list[str],
) -> dict[str, Any]:
    """Collect沪深 aggregate margin and same-day market statistics."""
    proxy_metadata = getattr(proxy_client, "metadata", {}) if proxy_client else {}
    tushare_provider = str(proxy_metadata.get("provider") or TUSHARE_PROVIDER)
    if margin_mode == "off":
        datasets.append({
            "dataset": "margin_leverage", "requirement": "optional", "impact": "display",
            "status": "not_required", "resolved_by": None, "attempted_sources": [],
            "basis": "用户关闭两融模块", "source_date": week["end_date"], "record_count": 0,
        })
        return {"mode": "off", "exchanges": {}, "market_daily": {}, "policy_events": POLICY_EVENTS}

    cutoff = week["end_date"]
    start_key = COMPARABLE_START.replace("-", "")
    end_key = cutoff.replace("-", "")
    refresh = set(refresh_datasets or [])
    exchanges: dict[str, list[dict[str, Any]]] = {}
    market_daily: dict[str, list[dict[str, Any]]] = {}
    proxy_shadow: dict[str, Any] = {}

    for exchange, ak_function in (("SSE", "macro_china_market_margin_sh"), ("SZSE", "macro_china_market_margin_sz")):
        dataset = f"margin_summary:{exchange}"
        cached = [] if dataset in refresh or "margin_summary" in refresh else store.get_series("margin_summary", exchange, end_date=cutoff)
        selected: list[dict[str, Any]] = cached if _cached_series_complete(cached, cutoff, minimum_rows=500) else []
        attempts: list[dict[str, Any]] = []
        proxy_rows: list[dict[str, Any]] = []
        if proxy_client and not selected and (policy == "shadow" or proxy_enabled_for(policy, proxy_health, dataset, "margin_summary")):
            before = len(proxy_client.statuses)
            request_start = start_key
            if len(cached) >= MIN_PERCENTILE_SAMPLE and dataset not in refresh and "margin_summary" not in refresh:
                request_start = (dt.date.fromisoformat(cached[-1]["trade_date"]) + dt.timedelta(days=1)).strftime("%Y%m%d")
            raw_proxy = proxy_client.call(dataset, "margin", {"exchange_id": exchange, "start_date": request_start, "end_date": end_key})
            proxy_rows = normalize_margin_rows(raw_proxy, exchange, tushare_provider, cutoff=cutoff)
            proxy_shadow[dataset] = proxy_rows
            attempts.extend(proxy_client.statuses[before:])
            if policy == "auto" and proxy_rows:
                merged = {row["trade_date"]: row for row in cached}
                merged.update({row["trade_date"]: row for row in proxy_rows})
                selected = [merged[day] for day in sorted(merged)]
        if not selected or policy == "shadow":
            before = len(client.statuses)
            raw_ak = client.call(dataset, ak_function, [{}], key_extra={"cutoff": cutoff, "margin_v": 1})
            ak_rows = normalize_margin_rows(raw_ak, exchange, "AkShare交易所汇总", cutoff=cutoff)
            attempts.extend(client.statuses[before:])
            if ak_rows and (not selected or policy == "shadow"):
                selected = ak_rows
        if not selected and cached:
            selected = cached
        exchanges[exchange] = selected
        if selected:
            store.upsert_series(selected[-1].get("provider") or "multi_source", "margin_summary", exchange, selected)
        logical = dataset_status(
            dataset, attempts, basis="融资融券交易所日汇总（人民币元）", source_date=selected[-1]["trade_date"] if selected else cutoff,
            requirement="optional", impact="display", empty_status="optional_unavailable",
        )
        if cached and selected is cached:
            logical.update({"status": "ok", "resolved_by": "sqlite_incremental_cache", "cache_hit": True, "record_count": len(selected)})
        datasets.append(logical)

    # BSE is deliberately display-only and never enters the comparable SSE+SZSE series.
    bse_cached = [] if "margin_summary:BSE" in refresh or "margin_summary" in refresh else store.get_series(
        "margin_summary", "BSE", end_date=cutoff
    )
    bse_rows: list[dict[str, Any]] = bse_cached if _cached_series_complete(bse_cached, cutoff) else []
    bse_attempts: list[dict[str, Any]] = []
    if not bse_rows and proxy_client and (policy == "shadow" or proxy_enabled_for(policy, proxy_health, "margin_summary:BSE", "margin_summary")):
        before = len(proxy_client.statuses)
        request_start = start_key
        if bse_cached:
            request_start = (dt.date.fromisoformat(bse_cached[-1]["trade_date"]) + dt.timedelta(days=1)).strftime("%Y%m%d")
        raw_bse = proxy_client.call("margin_summary:BSE", "margin", {"exchange_id": "BSE", "start_date": request_start, "end_date": end_key})
        new_rows = normalize_margin_rows(raw_bse, "BSE", tushare_provider, cutoff=cutoff)
        merged = {row["trade_date"]: row for row in bse_cached}
        merged.update({row["trade_date"]: row for row in new_rows})
        bse_rows = [merged[day] for day in sorted(merged)]
        bse_attempts.extend(proxy_client.statuses[before:])
    exchanges["BSE"] = bse_rows
    if bse_rows:
        store.upsert_series(bse_rows[-1].get("provider") or "multi_source", "margin_summary", "BSE", bse_rows)
    bse_status = dataset_status(
        "margin_summary:BSE", bse_attempts, basis="北交所单列展示", source_date=bse_rows[-1]["trade_date"] if bse_rows else cutoff,
        requirement="optional", impact="display", empty_status="optional_unavailable",
    )
    if bse_rows is bse_cached:
        bse_status.update({"status": "ok", "resolved_by": "sqlite_incremental_cache", "cache_hit": True, "record_count": len(bse_rows)})
    datasets.append(bse_status)

    for exchange, ts_exchange, ts_code in (("SSE", "SH", "SH_A"), ("SZSE", "SZ", "SZ_MARKET")):
        dataset = f"market_daily_info:{exchange}"
        cached = [] if dataset in refresh or "market_daily_info" in refresh else store.get_series("market_daily_info", exchange, end_date=cutoff)
        current_cached = _cached_series_complete(cached, cutoff)
        history_cached = _cached_series_complete(cached, cutoff, minimum_rows=MIN_PERCENTILE_SAMPLE)
        proxy_allowed = bool(
            proxy_client
            and (policy == "shadow" or proxy_enabled_for(policy, proxy_health, dataset, "market_daily_info"))
        )
        # A report-end exchange snapshot is sufficient for current ratios. Long
        # history is a separate requirement for percentiles and calibration.
        selected = cached if current_cached and (history_cached or not proxy_allowed) else []
        attempts: list[dict[str, Any]] = []
        proxy_rows: list[dict[str, Any]] = []
        if proxy_allowed and (not history_cached or policy == "shadow"):
            before = len(proxy_client.statuses)
            request_start = start_key
            if history_cached and cached and dataset not in refresh and "market_daily_info" not in refresh:
                request_start = (dt.date.fromisoformat(cached[-1]["trade_date"]) + dt.timedelta(days=1)).strftime("%Y%m%d")
            raw_proxy = proxy_client.call(
                dataset, "daily_info",
                {"ts_code": ts_code, "exchange": ts_exchange, "start_date": request_start, "end_date": end_key},
            )
            proxy_rows = normalize_daily_info_rows(raw_proxy, exchange, tushare_provider, cutoff=cutoff)
            proxy_shadow[dataset] = proxy_rows
            attempts.extend(proxy_client.statuses[before:])
            if policy == "auto" and proxy_rows:
                merged = {row["trade_date"]: row for row in cached}
                merged.update({row["trade_date"]: row for row in proxy_rows})
                selected = [merged[day] for day in sorted(merged)]
        # Public exchange fallbacks provide the report-end snapshot even when no historical denominator exists.
        if not selected or policy == "shadow":
            before = len(client.statuses)
            function = "stock_sse_deal_daily" if exchange == "SSE" else "stock_szse_summary"
            raw_public = client.call(dataset, function, [{"date": end_key}], key_extra={"margin_market_snapshot": cutoff})
            public_rows = normalize_exchange_market_snapshot(raw_public, exchange, cutoff, "交易所公开汇总")
            attempts.extend(client.statuses[before:])
            if public_rows and (not selected or policy == "shadow"):
                # Preserve historical cache and replace the same-day row deterministically.
                merged = {row["trade_date"]: row for row in cached}
                merged.update({row["trade_date"]: row for row in public_rows})
                selected = [merged[day] for day in sorted(merged)]
        if not selected and cached:
            selected = cached
        market_daily[exchange] = selected
        if selected:
            store.upsert_series(selected[-1].get("provider") or "multi_source", "market_daily_info", exchange, selected)
        logical = dataset_status(
            dataset, attempts, basis="A股流通市值与股票成交额（统一为元）",
            source_date=selected[-1]["trade_date"] if selected else cutoff,
            requirement="optional", impact="display", empty_status="optional_unavailable",
        )
        if cached and selected is cached:
            logical.update({
                "status": "ok", "resolved_by": "sqlite_incremental_cache", "cache_hit": True,
                "record_count": len(selected),
                "history_coverage": "full" if history_cached else "current_snapshot_only",
            })
        datasets.append(logical)

    concentration_rows = [] if "margin_concentration" in refresh else store.get_series(
        "margin_concentration", "SSE+SZSE", end_date=cutoff
    )
    concentration: dict[str, Any] = next(
        (row for row in reversed(concentration_rows) if row.get("trade_date") == cutoff), {}
    )
    if margin_mode == "full" and concentration:
        datasets.append({
            "dataset": "margin_concentration", "requirement": "optional", "impact": "display",
            "status": "ok", "attempted_sources": ["sqlite_incremental_cache"],
            "resolved_by": "sqlite_incremental_cache", "basis": concentration.get("basis"),
            "source_date": concentration.get("as_of"), "record_count": 1, "cache_hit": True,
        })
    elif margin_mode == "full" and proxy_client and (policy == "shadow" or proxy_enabled(policy, proxy_health, "margin_concentration")):
        before = len(proxy_client.statuses)
        details = proxy_client.call("margin_concentration", "margin_detail", {"trade_date": end_key})
        unique_details: dict[str, float] = {}
        for index, row in enumerate(details):
            code = str(row.get("ts_code") or row.get("code") or index)
            balance = parse_number(row.get("rzye"))
            if balance is not None and balance >= 0:
                unique_details[code] = max(balance, unique_details.get(code, 0))
        balances = list(unique_details.values())
        balances = sorted((value for value in balances if value is not None and value >= 0), reverse=True)
        combined_current = []
        for exchange in ("SSE", "SZSE"):
            if exchanges.get(exchange) and exchanges[exchange][-1].get("trade_date") == cutoff:
                value = parse_number(exchanges[exchange][-1].get("financing_balance"))
                if value is not None:
                    combined_current.append(value)
        market_total = sum(combined_current) if len(combined_current) == 2 else None
        if market_total and len(balances) >= 100:
            prior_values = [
                parse_number(row.get("top100_share"))
                for row in concentration_rows if row.get("trade_date") < cutoff
            ]
            prior_values = [value for value in prior_values if value is not None]
            top100_share = sum(balances[:100]) / market_total * 100
            concentration = {
                "trade_date": cutoff,
                "as_of": cutoff,
                "top20_share": sum(balances[:20]) / market_total * 100,
                "top100_share": top100_share,
                "top100_percentile": percentile_rank(prior_values[-1250:], top100_share),
                "history_sample_count": len(prior_values),
                "universe_count": len(balances),
                "market_financing_balance": market_total,
                "basis": "个股融资余额/沪深全市场融资余额",
            }
            store.upsert_series("derived_from_margin_detail", "margin_concentration", "SSE+SZSE", [concentration])
        concentration_status = dataset_status(
            "margin_concentration", proxy_client.statuses[before:], basis="个股融资余额集中度",
            source_date=cutoff, requirement="optional", impact="display", empty_status="optional_unavailable",
        )
        if details and len(balances) < 100:
            concentration_status.update({
                "status": "partial", "reason": f"个股明细仅{len(balances)}只，少于Top100计算下限",
                "record_count": len(balances),
            })
        datasets.append(concentration_status)
    elif concentration:
        datasets.append({
            "dataset": "margin_concentration", "requirement": "optional", "impact": "display",
            "status": "ok", "attempted_sources": ["sqlite_incremental_cache"],
            "resolved_by": "sqlite_incremental_cache", "basis": concentration.get("basis"),
            "source_date": concentration.get("as_of"), "record_count": 1, "cache_hit": True,
        })
    else:
        datasets.append({
            "dataset": "margin_concentration", "requirement": "optional", "impact": "display",
            "status": "not_required" if margin_mode != "full" else "optional_unavailable",
            "attempted_sources": [], "resolved_by": None, "basis": "仅full模式采集",
            "source_date": cutoff, "record_count": 0,
        })
    datasets.append({
        "dataset": "margin_policy_events", "requirement": "optional", "impact": "display",
        "status": "ok", "attempted_sources": ["versioned_local_reference"], "resolved_by": "versioned_local_reference",
        "basis": "政策节点只用于解释，不修改数据", "source_date": cutoff, "record_count": len(POLICY_EVENTS),
    })
    return {
        "mode": margin_mode,
        "exchanges": exchanges,
        "market_daily": market_daily,
        "concentration": concentration,
        "policy_events": POLICY_EVENTS,
        "comparable_start": COMPARABLE_START,
        "proxy_shadow": proxy_shadow,
    }


def collect_rankings(client: AkshareClient) -> dict[str, list[dict[str, Any]]]:
    output = {}
    for symbol in ["全部", "股票型", "混合型", "指数型", "QDII"]:
        output[symbol] = client.call(f"weekly ranking {symbol}", "fund_open_fund_rank_em", [{"symbol": symbol}, {}], limit=200)
    return output


def ranking_candidate_codes(rankings: dict[str, list[dict[str, Any]]], limit: int = 20) -> list[str]:
    candidates = []
    seen = set()
    for rows in rankings.values():
        for row in rows or []:
            code = next((str(value).zfill(6) for key, value in row.items() if "代码" in str(key) and value), "")
            weekly = next((parse_number(value) for key, value in row.items() if "近1周" in str(key)), None)
            if code and weekly is not None and code not in seen:
                seen.add(code)
                candidates.append((code, weekly))
    candidates.sort(key=lambda item: item[1], reverse=True)
    return [code for code, _ in candidates[:limit]]


def eastmoney_sector_flow_compat(indicator: str, sector_type: str) -> list[dict[str, Any]]:
    """Compatibility path for AkShare 1.18.x mixed-type 5/10-day sorting."""
    import requests

    sector_map = {"行业资金流": "2", "概念资金流": "3"}
    fields = {
        "今日": ("f62", "1", "f14,f3,f62"),
        "5日": ("f164", "5", "f14,f109,f164"),
        "10日": ("f174", "10", "f14,f160,f174"),
    }
    fid, stat, selected = fields[indicator]
    response = requests.get(
        "https://push2.eastmoney.com/api/qt/clist/get",
        params={"pn": 1, "pz": 500, "po": 1, "np": 1, "ut": "b2884a393a59ad64002292a3e90d46a5", "fltt": 2, "invt": 2,
                "fid0": fid, "fs": f"m:90 t:{sector_map[sector_type]}", "stat": stat, "fields": selected},
        headers={"User-Agent": "Mozilla/5.0"}, timeout=15,
    )
    response.raise_for_status()
    diff = ((response.json().get("data") or {}).get("diff") or [])
    return normalize_sector_flow_diff(diff, indicator)


def normalize_sector_flow_diff(diff: list[dict[str, Any]], indicator: str) -> list[dict[str, Any]]:
    """Normalize mixed Eastmoney values before any sorting or sign filtering."""
    return [
        {
            "名称": row.get("f14"),
            f"{indicator}涨跌幅": parse_number(row.get({"今日": "f3", "5日": "f109", "10日": "f160"}[indicator])),
            f"{indicator}主力净流入-净额": parse_number(row.get({"今日": "f62", "5日": "f164", "10日": "f174"}[indicator])),
            "资金单位": "元",
        }
        for row in diff if row.get("f14")
    ]


_SECTOR_CODE_MAP: dict[str, str] = {"油田服务": "BK1573"}


def eastmoney_sector_flow_history_compat(symbol: str) -> list[dict[str, Any]]:
    """Fetch industry daily flow without AkShare's repeated mapping request."""
    import requests

    global _SECTOR_CODE_MAP
    headers = {"User-Agent": "Mozilla/5.0"}
    if symbol not in _SECTOR_CODE_MAP:
        response = requests.get(
            "https://push2.eastmoney.com/api/qt/clist/get",
            params={
                "fid": "f62", "po": 1, "pz": 500, "pn": 1, "np": 1, "fltt": 2, "invt": 2,
                "ut": "8dec03ba335b81bf4ebdf7b29ec27d15", "fs": "m:90 t:2", "fields": "f12,f14",
            },
            headers=headers,
            timeout=15,
        )
        response.raise_for_status()
        diff = ((response.json().get("data") or {}).get("diff") or [])
        _SECTOR_CODE_MAP.update({str(row.get("f14")): str(row.get("f12")) for row in diff if row.get("f14") and row.get("f12")})
    code = _SECTOR_CODE_MAP.get(symbol)
    if not code:
        return []
    response = requests.get(
        "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get",
        params={
            "lmt": 0, "klt": 101, "fields1": "f1,f2,f3,f7",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
            "secid": f"90.{code}",
        },
        headers=headers,
        timeout=15,
    )
    response.raise_for_status()
    lines = ((response.json().get("data") or {}).get("klines") or [])
    records = []
    for line in lines:
        values = str(line).split(",")
        if len(values) >= 2:
            records.append({"日期": values[0], "主力净流入-净额": parse_number(values[1])})
    return records


def sector_row_value(row: dict[str, Any], tokens: list[str]) -> float | None:
    key = next((key for key in row if any(token in str(key) for token in tokens)), None)
    return parse_number(row.get(key)) if key else None


def sector_history_names(sectors: dict[str, Any], limit: int) -> list[str]:
    rows = (((sectors.get("fund_flow") or {}).get("5日") or {}).get("行业资金流") or [])
    valid = [row for row in rows if row.get("名称")]
    return_leaders = sorted(valid, key=lambda row: sector_row_value(row, ["5日涨跌幅"]) if sector_row_value(row, ["5日涨跌幅"]) is not None else -999, reverse=True)[:10]
    inflow = sorted(valid, key=lambda row: sector_row_value(row, ["5日主力净流入-净额"]) if sector_row_value(row, ["5日主力净流入-净额"]) is not None else -float("inf"), reverse=True)[:10]
    outflow = sorted(valid, key=lambda row: sector_row_value(row, ["5日主力净流入-净额"]) if sector_row_value(row, ["5日主力净流入-净额"]) is not None else float("inf"))[:10]
    return list(dict.fromkeys(str(row["名称"]) for row in return_leaders + inflow + outflow))[:limit]


def collect_sector_flow_histories(
    client: AkshareClient,
    sectors: dict[str, Any],
    week: dict[str, Any],
    datasets: list[dict[str, Any]],
    mode: str,
) -> None:
    names = sector_history_names(sectors, 30)
    histories: dict[str, list[dict[str, Any]]] = {}
    status_start = len(client.statuses)
    for name in names:
        rows = client.call(
            f"industry flow history {name}", "stock_sector_fund_flow_hist",
            [{"symbol": name}], key_extra={"end_date": week["end_date"], "dataset": "industry_flow_history"}, limit=260,
        )
        if not rows:
            rows = client.call_custom(
                f"industry flow history {name} compatibility",
                "eastmoney_sector_flow_history_compat",
                lambda sector=name: eastmoney_sector_flow_history_compat(sector),
                key_extra={"symbol": name, "end_date": week["end_date"], "dataset": "industry_flow_history"},
                limit=260,
            )
        if rows:
            histories[name] = rows
    sectors["industry_flow_history"] = histories
    sectors["history_coverage"] = {"requested": len(names), "available": len(histories), "source_date": week["end_date"]}
    logical = dataset_status(
        "industry_flow_history:leaders",
        client.statuses[status_start:],
        basis="板块逐日历史资金流",
        source_date=week["end_date"],
        requirement="optional",
        impact="score",
        empty_status="not_required" if not names else "failed",
        empty_reason="当前没有需要逐行补采的重点行业" if not names else None,
    )
    if names and len(histories) < len(names):
        logical["status"] = "partial" if histories else "failed"
        logical["reason"] = f"{len(histories)}/{len(names)}个重点行业取得历史资金流"
    datasets.append(logical)


def load_sector_snapshot(
    snapshot_dir: Path,
    indicator: str,
    sector_type: str,
    max_days: int = 10,
    expected_source_date: str | None = None,
) -> dict[str, Any]:
    path = snapshot_dir / f"{sector_type}_{indicator}.json"
    if not path.exists():
        candidates = sorted(
            snapshot_dir.parent.parent.glob(f"v*/sector_snapshots/{sector_type}_{indicator}.json"),
            key=lambda candidate: candidate.stat().st_mtime,
            reverse=True,
        )
        path = next((candidate for candidate in candidates if candidate != path), path)
    if not path.exists():
        return {}
    payload = load_json(path)
    source_day = parse_day(payload.get("source_date"))
    age = (dt.date.today() - source_day).days if source_day else max_days + 1
    if age > max_days or not payload.get("records") or (expected_source_date and payload.get("source_date") != expected_source_date):
        return {}
    payload["cache_age_days"] = age
    payload["cache_path"] = str(path)
    return payload


def save_sector_snapshot(snapshot_dir: Path, indicator: str, sector_type: str, records: list[dict[str, Any]], source_date: str) -> None:
    write_json(snapshot_dir / f"{sector_type}_{indicator}.json", {"source_date": source_date, "records": records})


def collect_sectors(client: AkshareClient, datasets: list[dict[str, Any]], week: dict[str, Any], snapshot_dir: Path, mode: str = "quick") -> dict[str, Any]:
    collection_trade_date = week.get("collection_trade_date") or week["end_date"]
    rolling_matches_report = collection_trade_date == week["end_date"]
    industry_start = len(client.statuses)
    industry_today = client.call("industry board today", "stock_board_industry_name_em", [{}], limit=160)
    if not industry_today:
        industry_today = client.call("industry board today THS fallback", "stock_board_industry_summary_ths", [{}], limit=160)
    datasets.append(dataset_status("industry_board:today", client.statuses[industry_start:], basis="今日快照", source_date=collection_trade_date))
    concept_start = len(client.statuses)
    concept_today = client.call("concept board today", "stock_board_concept_name_em", [{}], limit=220)
    concept_scope = "全市场"
    intraday_status = dataset_status(
        "concept_board:intraday",
        client.statuses[concept_start:],
        basis="盘中/当日快照",
        source_date=collection_trade_date,
        requirement="optional",
        impact="display",
        empty_status="optional_unavailable",
        empty_reason="当日概念快照不可用",
    )
    datasets.append(intraday_status)
    latest_close = concept_today if concept_today and rolling_matches_report else []
    datasets.append(dataset_status(
        "concept_board:latest_close",
        client.statuses[concept_start:] if latest_close else [],
        basis="最近有效收盘概念行情",
        source_date=week["end_date"],
        requirement="required",
        impact="report",
        empty_reason="尚无与报告截止日对齐的概念收盘行情",
    ))
    sectors = {
        "industry_today": industry_today,
        "concept_today": concept_today if concept_today and collection_trade_date == week["end_date"] else [],
        "concept_intraday": concept_today,
        "concept_latest_close": latest_close,
        "universe_scope": {"industry": "全市场", "concept": concept_scope},
        "fund_flow": {},
        "flow_meta": {},
    }
    for indicator in ["今日", "5日", "10日"]:
        sectors["fund_flow"][indicator] = {}
        sectors["flow_meta"][indicator] = {}
        for sector_type in ["行业资金流", "概念资金流"]:
            status_start = len(client.statuses)
            can_use_live = indicator == "今日" or rolling_matches_report
            rows = []
            if can_use_live:
                rows = client.call(
                    f"{sector_type} {indicator}",
                    "stock_sector_fund_flow_rank",
                    [{"indicator": indicator, "sector_type": sector_type}],
                    limit=160,
                )
                if not rows:
                    rows = client.call_custom(
                        f"{sector_type} {indicator} compatibility",
                        "eastmoney_sector_fund_flow_compat",
                        lambda p=indicator, s=sector_type: eastmoney_sector_flow_compat(p, s),
                        key_extra={"indicator": indicator, "sector_type": sector_type}, limit=160,
                    )
            if not rows and indicator == "今日" and sector_type == "行业资金流" and industry_today:
                rows = [{
                    "名称": row.get("板块"),
                    "今日涨跌幅": parse_number(row.get("涨跌幅")),
                    # stock_board_industry_summary_ths reports net inflow in 亿元.
                    "今日主力净流入-净额": (parse_number(row.get("净流入")) or 0) * 100_000_000,
                    "资金单位": "元",
                    "原始资金单位": "亿元",
                } for row in industry_today if row.get("板块") and parse_number(row.get("净流入")) is not None]
                if rows:
                    client.statuses.append({"label": "行业资金流 今日 THS summary", "function": "stock_board_industry_summary_ths", "status": "fallback_used", "cache_hit": False, "record_count": len(rows)})
            if rows and sector_type == "概念资金流" and concept_today:
                concept_names = {str(row.get("板块名称") or "").strip() for row in concept_today}
                flow_names = {str(row.get("名称") or "").strip() for row in rows}
                overlap = len(concept_names & flow_names) / max(1, min(len(concept_names), len(flow_names)))
                if overlap < 0.20:
                    rows = []
                    client.statuses.append({
                        "label": f"概念资金流 {indicator} taxonomy validation",
                        "function": "concept_taxonomy_overlap_check",
                        "status": "failed",
                        "record_count": 0,
                        "reason": f"concept taxonomy mismatch: overlap={overlap:.1%}",
                    })
            source_date = collection_trade_date if can_use_live else week["end_date"]
            cache_age_days = 0
            if rows:
                save_sector_snapshot(snapshot_dir, indicator, sector_type, rows, source_date)
            else:
                expected = week["end_date"] if indicator in {"5日", "10日"} else None
                cached = load_sector_snapshot(snapshot_dir, indicator, sector_type, expected_source_date=expected)
                if cached:
                    rows = cached["records"]
                    source_date = cached.get("source_date") or source_date
                    cache_age_days = cached.get("cache_age_days") or 0
                    client.statuses.append({"label": f"{sector_type} {indicator} snapshot", "function": "sector_snapshot_cache", "status": "fallback_used", "cache_hit": True, "record_count": len(rows), "source_date": cached.get("source_date"), "stale_days": cached.get("cache_age_days")})
            rows = [dict(row, 资金单位=row.get("资金单位") or "元") for row in rows]
            sectors["fund_flow"][indicator][sector_type] = rows
            sectors["flow_meta"][indicator][sector_type] = {
                "source_date": source_date,
                "cache_age_days": cache_age_days,
                "rolling_matches_report": rolling_matches_report,
                "normalized_flow_unit": "元",
            }
            prefix = "industry" if sector_type == "行业资金流" else "concept"
            datasets.append(dataset_status(f"{prefix}_flow:{indicator}", client.statuses[status_start:], basis=f"{indicator}资金流排名", source_date=source_date))
    collect_sector_flow_histories(client, sectors, week, datasets, mode)
    return sectors


def collect_profile(client: AkshareClient, code: str, prefix: str) -> dict[str, Any]:
    year = dt.date.today().year
    return {
        "basic_info": client.call(f"{prefix} basic info", "fund_individual_basic_info_xq", [{"symbol": code}]),
        "ths_info": client.call(f"{prefix} ths info", "fund_info_ths", [{"symbol": code}]),
        "stock_holdings": client.call(
            f"{prefix} holdings", "fund_portfolio_hold_em", [{"symbol": code, "date": str(year)}, {"symbol": code, "date": str(year - 1)}, {"symbol": code}], limit=80
        ),
        "industry_allocation": client.call(
            f"{prefix} industry", "fund_portfolio_industry_allocation_em", [{"symbol": code, "date": str(year)}, {"symbol": code, "date": str(year - 1)}, {"symbol": code}], limit=80
        ),
    }


def collect_etfs(
    client: AkshareClient,
    codes: list[str],
    week: dict[str, Any],
    datasets: list[dict[str, Any]],
    mode: str = "quick",
) -> dict[str, Any]:
    spot_start = len(client.statuses)
    spot_em = client.call_custom(
        "candidate ETF spot Eastmoney compatibility",
        "eastmoney_etf_spot_candidates",
        lambda: eastmoney_etf_spot_candidates(codes),
        key_extra={"codes": sorted(codes), "collection_trade_date": week.get("collection_trade_date")},
    )
    if not spot_em and mode == "full":
        spot_em = client.call("ETF spot full-market fallback", "fund_etf_spot_em", [{}])
    # Sina's ETF snapshot is a stable, fast fallback and is filtered to the
    # configured candidate list by the analyzer.
    spot_sina = [] if spot_em else client.call(
        "ETF spot Sina fallback", "fund_etf_category_sina", [{"symbol": "ETF基金"}]
    )
    datasets.append(dataset_status(
        "etf_live_quote",
        client.statuses[spot_start:],
        basis="实时行情或新浪快照",
        source_date=week.get("collection_trade_date") or week["end_date"],
        requirement="optional",
        impact="execution",
        empty_status="optional_unavailable",
        empty_reason="ETF盘中行情暂不可用；不影响报告截止日收盘分析",
    ))
    nav_spot_start = len(client.statuses)
    nav_spot_ths = client.call("ETF NAV snapshot THS", "fund_etf_spot_ths", [{}])
    datasets.append(dataset_status(
        "etf_nav_snapshot",
        client.statuses[nav_spot_start:],
        basis="同花顺最新交易日单位/累计净值",
        source_date=week["end_date"],
        requirement="optional",
        impact="action",
        empty_status="optional_unavailable",
    ))
    start_day = parse_day(week["baseline_date"]) - dt.timedelta(days=45)
    start = start_day.strftime("%Y%m%d")
    end = week["end_date"].replace("-", "")
    history: dict[str, Any] = {}
    nav: dict[str, Any] = {}
    feeder_nav: dict[str, Any] = {}
    history_sina: dict[str, Any] = {}
    for code in codes:
        status_start = len(client.statuses)
        history[code] = {}
        for adjust in ["hfq", "qfq", ""]:
            history[code][adjust or "none"] = client.call(
                f"{code} ETF history {adjust or 'none'}",
                "fund_etf_hist_em",
                [{"symbol": code, "period": "daily", "start_date": start, "end_date": end, "adjust": adjust}],
                key_extra=week,
            )
        adjusted_available = bool(history[code].get("hfq") and history[code].get("qfq"))
        nav[code] = [] if mode == "quick" and adjusted_available else client.call(
            f"{code} ETF NAV", "fund_etf_fund_info_em",
            [{"symbol": code, "indicator": "单位净值走势"}, {"fund": code}, {"symbol": code}],
            key_extra=week,
        )
        feeder = ETF_FEEDERS.get(code)
        if feeder and not (mode == "quick" and adjusted_available):
            feeder_nav[code] = {
                "feeder_code": feeder,
                "records": client.call(f"{code} feeder {feeder} NAV", "fund_open_fund_info_em", [{"symbol": feeder, "indicator": "累计净值走势"}, {"symbol": feeder, "indicator": "单位净值走势"}], key_extra=week),
            }
        if not any(history[code].values()):
            market_symbol = ("sh" if code.startswith(("5", "6")) else "sz") + code
            history_sina[code] = client.call(f"{code} ETF history Sina fallback", "fund_etf_hist_sina", [{"symbol": market_symbol}], key_extra=week)
        datasets.append(dataset_status(f"etf_return:{code}", client.statuses[status_start:], basis="复权价格/累计净值/联接代理/新浪价格", source_date=week["end_date"]))
    return {
        "spot": spot_em or spot_sina,
        "spot_em": spot_em,
        "spot_sina": spot_sina,
        "live_snapshot": {},
        "nav_spot_ths": nav_spot_ths,
        "history": history,
        "history_sina": history_sina,
        "nav": nav,
        "feeder_nav": feeder_nav,
        "iopv_snapshots": {},
        "codes": codes,
        "access": {code: ETF_CHANNELS.get(code, {"channel": "待核验", "market": "-", "verified_at": None}) for code in codes},
    }


def eastmoney_etf_spot_candidates(codes: list[str]) -> list[dict[str, Any]]:
    """Fetch the ETF quote universe once and retain only configured candidates."""
    import requests

    response = requests.get(
        "https://push2delay.eastmoney.com/api/qt/clist/get",
        params={
            "pn": "1", "pz": "5000", "po": "1", "np": "1",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281", "fltt": "2", "invt": "2",
            "fid": "f12", "fs": "b:MK0021,b:MK0022,b:MK0023,b:MK0024,b:MK0827",
            "fields": "f2,f3,f6,f12,f14,f124,f297,f402,f441",
        },
        timeout=12,
    )
    response.raise_for_status()
    rows = ((response.json().get("data") or {}).get("diff") or [])
    wanted = {str(code).zfill(6) for code in codes}
    output = []
    for row in rows:
        code = str(row.get("f12") or "").zfill(6)
        if code not in wanted:
            continue
        timestamp = parse_number(row.get("f124"))
        updated = dt.datetime.fromtimestamp(timestamp).isoformat(timespec="seconds") if timestamp else None
        output.append({
            "代码": code,
            "名称": row.get("f14"),
            "最新价": parse_number(row.get("f2")),
            "IOPV实时估值": parse_number(row.get("f441")),
            "基金折价率": parse_number(row.get("f402")),
            "涨跌幅": parse_number(row.get("f3")),
            "成交额": parse_number(row.get("f6")),
            "数据日期": row.get("f297"),
            "更新时间": updated,
        })
    return output


def load_profile_cache(profile_dir: Path, code: str, max_days: int = 180) -> dict[str, Any]:
    path = profile_dir / f"{code}.json"
    if not path.exists():
        candidates = sorted(
            profile_dir.parent.parent.glob(f"v*/profiles/{code}.json"),
            key=lambda candidate: candidate.stat().st_mtime,
            reverse=True,
        )
        path = next((candidate for candidate in candidates if candidate != path), path)
    if not path.exists():
        return {}
    payload = load_json(path)
    cached = parse_day(payload.get("cached_at"))
    age = (dt.date.today() - cached).days if cached else max_days + 1
    if age > max_days:
        return {}
    payload["stale_days"] = age
    payload["cache_path"] = str(path)
    components = payload.get("profile_components") or profile_components(payload.get("detail") or {})
    incomplete = not components.get("basic_info") or not (components.get("stock_holdings") or components.get("industry_allocation"))
    payload["profile_status"] = "stale_profile" if age > 90 else "stale_basic_info" if age > 30 else "partial_profile" if incomplete else "ok"
    payload["freshness"] = {"basic_scale_turnover_ttl_days": 30, "holdings_industry_ttl_days": 90, "maximum_reference_age_days": 180}
    payload.setdefault("disclosure_date", profile_disclosure_date(payload.get("detail") or {}))
    payload["profile_components"] = components
    return payload


def profile_disclosure_date(detail: dict[str, Any]) -> str | None:
    dates: list[dt.date] = []
    for row in (detail.get("stock_holdings") or []) + (detail.get("industry_allocation") or []):
        for key, value in row.items():
            if any(token in str(key) for token in ["截止时间", "报告期", "季度", "日期"]):
                day = parse_day(value)
                if day:
                    dates.append(day)
                else:
                    match = re.search(r"(20\d{2})年([1-4])季度", str(value))
                    if match:
                        month = int(match.group(2)) * 3
                        dates.append(dt.date(int(match.group(1)), month, 1))
    return max(dates).isoformat() if dates else None


def profile_components(detail: dict[str, Any]) -> dict[str, bool]:
    return {
        "basic_info": bool(detail.get("basic_info") or detail.get("ths_info")),
        "stock_holdings": bool(detail.get("stock_holdings")),
        "industry_allocation": bool(detail.get("industry_allocation")),
    }


def save_profile_cache(profile_dir: Path, code: str, detail: dict[str, Any]) -> dict[str, Any]:
    components = profile_components(detail)
    status = "ok" if components["basic_info"] and (components["stock_holdings"] or components["industry_allocation"]) else "partial_profile"
    payload = {
        "code": code, "cached_at": dt.date.today().isoformat(), "stale_days": 0,
        "profile_status": status, "disclosure_date": profile_disclosure_date(detail),
        "profile_components": components, "detail": detail,
    }
    write_json(profile_dir / f"{code}.json", payload)
    return payload


def mock_daily_series(
    week: dict[str, Any], start_value: float, weekly: float, monthly: float,
    *, date_key: str, value_key: str,
) -> list[dict[str, Any]]:
    end = parse_day(week["end_date"])
    baseline = parse_day(week["baseline_date"])
    month_start = end - dt.timedelta(days=32)
    dates = weekday_calendar(month_start, end)
    month_value = start_value / (1 + monthly / 100)
    end_value = start_value * (1 + weekly / 100)
    before = [day for day in dates if day <= baseline]
    after = [day for day in dates if day >= baseline]
    rows = []
    for day in dates:
        if day <= baseline:
            denominator = max(1, len(before) - 1)
            position = before.index(day) / denominator
            value = month_value + (start_value - month_value) * position
        else:
            denominator = max(1, len(after) - 1)
            position = after.index(day) / denominator
            value = start_value + (end_value - start_value) * position
        rows.append({date_key: day.isoformat(), value_key: round(value, 6)})
    return rows


def mock_series(week: dict[str, Any], start_value: float, weekly: float, monthly: float) -> list[dict[str, Any]]:
    return mock_daily_series(
        week, start_value, weekly, monthly, date_key="净值日期", value_key="单位净值",
    )


def _mock_payload_parts(holdings: list[dict[str, Any]], week: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    funds = {}
    for index, item in enumerate(holdings):
        funds[item["code"]] = {"nav": mock_series(week, 1 + index * 0.15, -4 + index * 1.4, -1 + index * 2.1)}
    industry_today = [{"板块名称": "半导体设备", "涨跌幅": 2.1}, {"板块名称": "通信设备", "涨跌幅": -1.2}]
    concept_today = [{"板块名称": "先进封装", "涨跌幅": 3.4}, {"板块名称": "光模块", "涨跌幅": -2.0}]
    flow = {
        "今日": {
            "行业资金流": [{"名称": "半导体设备", "今日主力净流入-净额": 1.2e9, "今日涨跌幅": 2.1}, {"名称": "通信设备", "今日主力净流入-净额": -8e8, "今日涨跌幅": -1.2}],
            "概念资金流": [{"名称": "先进封装", "今日主力净流入-净额": 8e8, "今日涨跌幅": 3.4}, {"名称": "光模块", "今日主力净流入-净额": -5e8, "今日涨跌幅": -2.0}],
        },
        "5日": {
            "行业资金流": [{"名称": "半导体设备", "5日主力净流入-净额": 3.6e9, "5日涨跌幅": 8.2}, {"名称": "通信设备", "5日主力净流入-净额": -2.0e9, "5日涨跌幅": -3.1}],
            "概念资金流": [{"名称": "先进封装", "5日主力净流入-净额": 1.5e9, "5日涨跌幅": 9.5}, {"名称": "光模块", "5日主力净流入-净额": -1.1e9, "5日涨跌幅": -4.0}],
        },
        "10日": {
            "行业资金流": [{"名称": "半导体设备", "10日主力净流入-净额": 5.8e9}, {"名称": "通信设备", "10日主力净流入-净额": -3.0e9}],
            "概念资金流": [{"名称": "先进封装", "10日主力净流入-净额": -2e8}, {"名称": "光模块", "10日主力净流入-净额": -1.8e9}],
        },
    }
    return funds, industry_today, concept_today, flow


def history_trade_dates(trade_dates: list[dt.date], end_date: dt.date, history_weeks: int) -> list[dt.date]:
    """Return enough real trading dates for three week periods plus 10-day context."""
    eligible = [day for day in trade_dates if day <= end_date]
    minimum = max(history_weeks * 5 + 12, 30)
    return eligible[-minimum:]


def chunked_tushare_flow(
    client: TushareProxyClient,
    dataset: str,
    api_name: str,
    dates: list[dt.date],
) -> list[dict[str, Any]]:
    """Collect sector flow in bounded windows so concept APIs cannot silently truncate."""
    if not dates:
        return []
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for offset in range(0, len(dates), 5):
        block = dates[offset : offset + 5]
        pending = [block]
        while pending:
            current = pending.pop(0)
            rows = client.call(
                dataset,
                api_name,
                {
                    "start_date": current[0].strftime("%Y%m%d"),
                    "end_date": current[-1].strftime("%Y%m%d"),
                    "limit": 5000,
                },
            )
            returned_dates = {str(row.get("trade_date") or "").replace("-", "") for row in rows}
            expected_dates = {day.strftime("%Y%m%d") for day in current}
            appears_truncated = len(rows) >= 4900 or not expected_dates.issubset(returned_dates)
            if appears_truncated and len(current) > 1:
                midpoint = max(1, len(current) // 2)
                pending[0:0] = [current[:midpoint], current[midpoint:]]
                continue
            for row in rows:
                day = str(row.get("trade_date") or "").replace("-", "")
                symbol = str(row.get("ts_code") or row.get("industry") or row.get("name") or "")
                if day and symbol:
                    merged[(day, symbol)] = row
    return sorted(merged.values(), key=lambda row: (str(row.get("trade_date") or ""), str(row.get("ts_code") or "")))


def persist_payload_to_cache(store: CacheStore, payload: dict[str, Any]) -> dict[str, int]:
    counts = {"fund_nav": 0, "style_index": 0, "etf_history": 0, "ranking_snapshot": 0, "fund_profile": 0}
    for code, content in (payload.get("funds") or {}).items():
        provider = str(content.get("provider") or "AkShare及公开备用源")
        if provider == "本地增量缓存":
            continue
        counts["fund_nav"] += store.upsert_series(provider, "fund_nav", code, content.get("nav") or [])
    for name, rows in ((payload.get("market") or {}).get("style_indexes") or {}).items():
        if (((payload.get("market") or {}).get("style_index_meta") or {}).get(name) or {}).get("resolved_source") == "sqlite_incremental_cache":
            continue
        counts["style_index"] += store.upsert_series("多源已验证", "style_index", name, rows)
    for code, histories in ((payload.get("candidate_etfs") or {}).get("history") or {}).items():
        selected = histories.get("hfq") if isinstance(histories, dict) else histories
        counts["etf_history"] += store.upsert_series("多源已验证", "etf_history_hfq", code, selected or [])
    report_end = str((payload.get("week") or {}).get("end_date") or "")
    for group, rows in (payload.get("rankings") or {}).items():
        by_date: dict[str, list[dict[str, Any]]] = {}
        for row in rows or []:
            as_of = str(row.get("日期") or row.get("净值日期") or "")[:10]
            if as_of and (not report_end or as_of <= report_end):
                by_date.setdefault(as_of, []).append(row)
        for as_of, dated_rows in by_date.items():
            store.put_snapshot("AkShare", "fund_ranking", group, as_of, dated_rows)
            counts["ranking_snapshot"] += len(dated_rows)
    for code, profile in (payload.get("fund_profiles") or {}).items():
        disclosure = str(profile.get("disclosure_date") or payload.get("as_of") or "")[:10]
        if disclosure:
            store.put_profile("公开披露", code, "quarterly_profile", disclosure, profile)
            counts["fund_profile"] += 1
    return counts


def complete_cached_sector_flow(
    store: CacheStore | None,
    kind: str,
    flow_dates: list[dt.date] | None,
    providers: tuple[str, ...] = (
        OFFICIAL_PROVIDER,
        THIRD_PARTY_PROVIDER,
        LEGACY_THIRD_PARTY_PROVIDER,
    ),
) -> list[dict[str, Any]]:
    """Return a frozen Tushare flow range only when every requested trading day is present."""
    if not store or not flow_dates:
        return []
    dataset = f"{kind}_flow_daily"
    expected = {day.isoformat() for day in flow_dates}
    for provider in providers:
        rows = [
            row
            for symbol in store.list_symbols(dataset, provider=provider)
            for row in store.get_series(
                dataset,
                symbol,
                provider=provider,
                start_date=flow_dates[0].isoformat(),
                end_date=flow_dates[-1].isoformat(),
            )
        ]
        present = {
            day.isoformat()
            for row in rows
            if (day := parse_day(row.get("trade_date"))) is not None
        }
        if expected.issubset(present):
            return rows
    return []


def apply_cached_sector_flow_overlay(
    payload: dict[str, Any],
    datasets: list[dict[str, Any]],
    store: CacheStore,
    flow_dates: list[dt.date],
    refresh_datasets: set[str] | None = None,
) -> list[str]:
    """Recover validated closed-day sector history when the proxy runtime is unavailable."""
    refresh_datasets = refresh_datasets or set()
    week = payload["week"]
    used: list[str] = []
    for kind, health_key, sector_type in (
        ("industry", "industry_flow", "行业资金流"),
        ("concept", "concept_flow", "概念资金流"),
    ):
        dataset_name = f"{kind}_flow_daily"
        if health_key in refresh_datasets or dataset_name in refresh_datasets:
            continue
        rows = complete_cached_sector_flow(store, kind, flow_dates)
        if not rows:
            continue
        period_rows = {
            period: aggregate_sector_flow(
                rows,
                period=period,
                end_date=week["end_date"],
                sector_type=sector_type,
                provider="本地增量缓存",
            )
            for period in (1, 5, 10)
        }
        if not period_rows[5]:
            continue
        prefix = "industry" if kind == "industry" else "concept"
        for period, label in ((1, "报告期末日"), (5, "5日"), (10, "10日")):
            existing = payload["market"]["sectors"]["fund_flow"].get(label, {}).get(sector_type) or []
            if existing:
                payload["market"]["sectors"].setdefault("fund_flow_alternates", {}).setdefault(
                    "AkShare及公开备用源", {}
                ).setdefault(label, {})[sector_type] = existing
            payload["market"]["sectors"]["fund_flow"].setdefault(label, {})[sector_type] = period_rows[period]
            payload["market"]["sectors"].setdefault("flow_meta", {}).setdefault(label, {})[sector_type] = {
                "source_date": week["end_date"], "cache_age_days": 0,
                "rolling_matches_report": True, "provider": "本地增量缓存",
                "flow_basis": "已验证net_amount逐日历史按报告截止日前真实交易日累计并换算为元",
                "normalized_flow_unit": "元",
                "taxonomy": "同花顺行业" if kind == "industry" else "同花顺概念",
                "universe_scope": "同花顺行业全量" if kind == "industry" else "同花顺概念全量",
            }
            replace_dataset_status(datasets, {
                "dataset": f"{prefix}_flow:{label}", "requirement": "required", "impact": "report",
                "attempted_sources": ["sqlite_incremental_cache"], "resolved_by": "sqlite_incremental_cache",
                "status": "fallback_used", "basis": "已验证闭市逐日资金流",
                "source_date": week["end_date"], "stale_days": 0,
                "record_count": len(period_rows[period]), "reason": None,
                "provider": "本地增量缓存", "transport": "sqlite", "cache_hit": True,
                "crosscheck_status": "cached_validated_history",
            })
        payload["market"]["sectors"].setdefault("universe_scope", {})[kind] = (
            "同花顺行业全量" if kind == "industry" else "同花顺概念全量"
        )
        if kind == "concept":
            report_key = week["end_date"].replace("-", "")
            latest_rows = [row for row in rows if str(row.get("trade_date") or "").replace("-", "") == report_key]
            concept_close = [
                {
                    "板块代码": row.get("ts_code"), "板块名称": row.get("name"),
                    "涨跌幅": parse_number(row.get("pct_change")),
                    "板块指数": parse_number(row.get("industry_index")),
                    "主力净流入": (parse_number(row.get("net_amount")) or 0) * 100_000_000,
                    "资金单位": "元", "source_date": week["end_date"],
                    "provider": "本地增量缓存", "taxonomy": "同花顺概念",
                }
                for row in latest_rows if row.get("name") and parse_number(row.get("pct_change")) is not None
            ]
            if concept_close:
                payload["market"]["sectors"]["concept_latest_close"] = concept_close
                replace_dataset_status(datasets, {
                    "dataset": "concept_board:latest_close", "requirement": "required", "impact": "report",
                    "attempted_sources": ["sqlite_incremental_cache"], "resolved_by": "sqlite_incremental_cache",
                    "status": "fallback_used", "basis": "报告截止日同花顺概念收盘行情",
                    "source_date": week["end_date"], "stale_days": 0,
                    "record_count": len(concept_close), "reason": None,
                    "provider": "本地增量缓存", "transport": "sqlite", "cache_hit": True,
                    "crosscheck_status": "cached_validated_history",
                })
        if kind == "industry":
            replace_dataset_status(datasets, {
                "dataset": "industry_flow_history:leaders", "requirement": "optional", "impact": "score",
                "attempted_sources": ["sqlite_incremental_cache"], "resolved_by": "sqlite_incremental_cache",
                "status": "ok", "basis": "全量行业逐日资金流已覆盖重点行业",
                "source_date": week["end_date"], "stale_days": 0,
                "record_count": len(rows), "reason": None,
                "provider": "本地增量缓存", "transport": "sqlite", "cache_hit": True,
                "crosscheck_status": "cached_validated_history",
            })
        used.append(health_key)
    return used


def apply_tushare_overlay(
    payload: dict[str, Any],
    client: TushareProxyClient,
    health: dict[str, Any],
    policy: str,
    datasets: list[dict[str, Any]],
    profile_dir: Path,
    store: CacheStore | None = None,
    flow_dates: list[dt.date] | None = None,
    refresh_datasets: set[str] | None = None,
) -> dict[str, Any]:
    """Collect Tushare evidence and apply only health-promoted datasets in auto mode."""
    week = payload["week"]
    holdings = payload["holdings"]
    provider = str(client.metadata.get("provider") or TUSHARE_PROVIDER)
    transport = str(client.metadata.get("transport") or "unknown")
    cache_providers = (
        (provider, LEGACY_THIRD_PARTY_PROVIDER)
        if provider == THIRD_PARTY_PROVIDER else (provider,)
    )
    start_long = (parse_day(week["end_date"]) - dt.timedelta(days=390)).strftime("%Y%m%d")
    end = week["end_date"].replace("-", "")
    shadow: dict[str, Any] = {}
    used_datasets: list[str] = []
    refresh_datasets = refresh_datasets or set()

    need_basic = any(
        proxy_enabled(policy, health, name) or any(
            proxy_enabled(policy, health, f"{name}:{item['code']}") for item in holdings
        )
        for name in ("fund_basic", "fund_nav", "fund_portfolio")
    )
    fund_basic_rows: list[dict[str, Any]] = []
    if need_basic:
        fund_basic_rows = collect_fund_master(client, [item["code"] for item in holdings])
        shadow["fund_basic"] = fund_basic_rows

    if proxy_enabled(policy, health, "fund_nav") or any(proxy_enabled(policy, health, f"fund_nav:{item['code']}") for item in holdings):
        for holding in holdings:
            code = holding["code"]
            if not proxy_enabled_for(policy, health, f"fund_nav:{code}", "fund_nav"):
                continue
            status_start = len(client.statuses)
            ts_code = resolve_fund_ts_code(fund_basic_rows, code)
            rows = client.call(
                f"fund_nav:{code}", "fund_nav",
                {"ts_code": ts_code, "start_date": start_long, "end_date": end} if ts_code else {"ts_code": f"UNRESOLVED:{code}"},
            ) if ts_code else []
            normalized = normalize_fund_nav(rows, week["end_date"])
            status = normalized_tushare_status(
                f"fund_nav:{code}", client.statuses[status_start:], "adj_nav优先，其次accum_nav",
                week["end_date"], provider=provider, transport=transport,
            )
            status["promotion_eligible"] = bool(normalized and status["promotion_eligible"])
            shadow[f"fund_nav:{code}"] = normalized
            if policy == "auto" and normalized:
                payload["funds"].setdefault(code, {})["nav"] = normalized
                payload["funds"][code].update({"provider": provider, "nav_basis": "adj_nav_then_accum_nav"})
                status["crosscheck_status"] = health_crosscheck_for(health, f"fund_nav:{code}", "fund_nav")
                replace_dataset_status(datasets, status)
                used_datasets.append(f"fund_nav:{code}")

    if payload.get("mode") == "full" and proxy_enabled(policy, health, "fund_portfolio"):
        stock_rows = client.call("security_master", "stock_basic", {"exchange": "", "list_status": "L", "fields": "ts_code,symbol,name"})
        stock_names = {
            str(row.get("symbol") or row.get("ts_code") or ""): str(row.get("name") or "")
            for row in stock_rows
        }
        for holding in holdings:
            code = holding["code"]
            status_start = len(client.statuses)
            ts_code = resolve_fund_ts_code(fund_basic_rows, code)
            raw = client.call(f"fund_profile:{code}", "fund_portfolio", {"ts_code": ts_code}) if ts_code else []
            selected = normalize_fund_portfolio(raw, week["end_date"])
            normalized = normalized_tushare_holdings(selected, stock_names, provider=provider)
            status = normalized_tushare_status(
                f"fund_profile:{code}", client.statuses[status_start:], "最新已公告季度持仓",
                week["end_date"], provider=provider, transport=transport,
            )
            status["promotion_eligible"] = bool(normalized and status["promotion_eligible"])
            shadow[f"fund_portfolio:{code}"] = normalized
            if policy == "auto" and normalized:
                existing = dict((payload.get("full_details") or {}).get(code) or {})
                existing["stock_holdings"] = normalized
                existing["provider"] = provider
                payload.setdefault("full_details", {})[code] = existing
                cached = save_profile_cache(profile_dir, code, existing)
                payload.setdefault("fund_profiles", {})[code] = cached
                status["crosscheck_status"] = health_crosscheck(health, "fund_portfolio")
                replace_dataset_status(datasets, status)
                used_datasets.append(f"fund_profile:{code}")

    if proxy_enabled(policy, health, "style_indexes") or any(proxy_enabled(policy, health, f"style_indexes:{symbols['primary']}") for symbols in STYLE_INDEXES.values()):
        for name, symbols in STYLE_INDEXES.items():
            if not proxy_enabled_for(policy, health, f"style_indexes:{symbols['primary']}", "style_indexes"):
                continue
            status_start = len(client.statuses)
            suffix = "SZ" if symbols["primary"].startswith("399") else "SH"
            rows = client.call(
                f"style_index:{symbols['primary']}", "index_daily",
                {"ts_code": f"{symbols['primary']}.{suffix}", "start_date": start_long, "end_date": end},
            )
            normalized = normalize_tushare_index(rows, week["end_date"], provider=provider)
            valid, _, latest = index_records_cover_week(normalized, week)
            status = normalized_tushare_status(
                f"style_index:{symbols['primary']}", client.statuses[status_start:], "指数历史收盘价",
                latest, provider=provider, transport=transport,
            )
            status["promotion_eligible"] = bool(valid and status["promotion_eligible"])
            shadow[f"style_index:{symbols['primary']}"] = normalized
            if policy == "auto" and valid:
                payload["market"]["style_indexes"][name] = normalized
                payload["market"]["style_index_meta"][name] = {
                    "return_basis": "指数历史收盘价",
                    "resolved_source": "index_daily",
                    "source_latest_date": latest,
                    "freshness_status": "周期完整",
                    "provider": provider,
                    "crosscheck_status": health_crosscheck_for(
                        health, f"style_indexes:{symbols['primary']}", "style_indexes"
                    ),
                }
                replace_dataset_status(datasets, status)
                used_datasets.append(f"style_index:{symbols['primary']}")

    flow_map = {"industry_flow": ("moneyflow_ind_ths", "行业资金流"), "concept_flow": ("moneyflow_cnt_ths", "概念资金流")}
    for health_key, (api_name, sector_type) in flow_map.items():
        source_enabled = proxy_enabled(policy, health, health_key)
        cache_allowed = policy == "auto" and health_key not in refresh_datasets and f"{'industry' if sector_type == '行业资金流' else 'concept'}_flow_daily" not in refresh_datasets
        if not source_enabled and not cache_allowed:
            continue
        status_start = len(client.statuses)
        kind = "industry" if sector_type == "行业资金流" else "concept"
        rows = complete_cached_sector_flow(store, kind, flow_dates, providers=cache_providers) if cache_allowed else []
        cache_used = bool(rows)
        if cache_used:
            client.statuses.append({
                "dataset": health_key, "label": health_key, "function": "sqlite_incremental_cache",
                "status": "ok", "cache_hit": True, "record_count": len(rows),
                "source_date": flow_dates[-1].isoformat(), "provider": "本地增量缓存",
            })
        if not rows and not source_enabled:
            continue
        if not rows:
            rows = chunked_tushare_flow(client, health_key, api_name, flow_dates or [])
        if not flow_dates:
            start_flow = (parse_day(week["baseline_date"]) - dt.timedelta(days=35)).strftime("%Y%m%d")
            rows = client.call(health_key, api_name, {"start_date": start_flow, "end_date": end, "limit": 5000})
        if store and rows:
            grouped: dict[str, list[dict[str, Any]]] = {}
            for row in rows:
                symbol = str(row.get("ts_code") or f"tushare:{kind}:{row.get('industry') or row.get('name') or ''}")
                grouped.setdefault(symbol, []).append(row)
            for symbol, values in grouped.items():
                store.upsert_series(provider, f"{kind}_flow_daily", symbol, values)
        period_rows = {
            period: aggregate_sector_flow(
                rows, period=period, end_date=week["end_date"], sector_type=sector_type, provider=provider
            )
            for period in (1, 5, 10)
        }
        shadow[health_key] = {("今日" if period == 1 else f"{period}日"): values for period, values in period_rows.items()}
        complete = bool(period_rows[5])
        prefix = "industry" if sector_type == "行业资金流" else "concept"
        if policy == "auto" and complete:
            for period, label in [(1, "今日"), (5, "5日"), (10, "10日")]:
                storage_label = "报告期末日" if period == 1 else label
                existing_rows = (
                    payload["market"]["sectors"]["fund_flow"].get(storage_label, {}).get(sector_type) or []
                )
                if existing_rows:
                    payload["market"]["sectors"].setdefault("fund_flow_alternates", {}).setdefault(
                        "AkShare及公开备用源", {}
                    ).setdefault(storage_label, {})[sector_type] = existing_rows
                payload["market"]["sectors"]["fund_flow"].setdefault(storage_label, {})[sector_type] = period_rows[period]
                payload["market"]["sectors"].setdefault("flow_meta", {}).setdefault(storage_label, {})[sector_type] = {
                    "source_date": week["end_date"], "cache_age_days": 0,
                    "rolling_matches_report": True, "provider": provider,
                    "flow_basis": "net_amount（原始单位亿元）按报告截止日前真实交易日累计并换算为元",
                    "normalized_flow_unit": "元",
                    "taxonomy": "同花顺行业" if sector_type == "行业资金流" else "同花顺概念",
                    "universe_scope": "同花顺行业全量" if sector_type == "行业资金流" else "同花顺概念全量",
                }
                status = normalized_tushare_status(
                    f"{prefix}_flow:{storage_label}", client.statuses[status_start:], f"{label}net_amount累计",
                    week["end_date"], provider=provider, transport=transport,
                )
                if cache_used:
                    status.update({"provider": "本地增量缓存", "transport": "sqlite", "resolved_by": "sqlite_incremental_cache", "cache_hit": True})
                status["crosscheck_status"] = "cached_validated_history" if cache_used else health_crosscheck(health, health_key)
                replace_dataset_status(datasets, status)
            scope_key = "industry" if sector_type == "行业资金流" else "concept"
            payload["market"]["sectors"].setdefault("universe_scope", {})[scope_key] = (
                "同花顺行业全量" if sector_type == "行业资金流" else "同花顺概念全量"
            )
            report_key = week["end_date"].replace("-", "")
            latest_rows = [row for row in rows if str(row.get("trade_date") or "").replace("-", "") == report_key]
            if kind == "concept" and latest_rows:
                concept_close = [
                    {
                        "板块代码": row.get("ts_code"),
                        "板块名称": row.get("name"),
                        "涨跌幅": parse_number(row.get("pct_change")),
                        "板块指数": parse_number(row.get("industry_index")),
                        "主力净流入": (parse_number(row.get("net_amount")) or 0) * 100_000_000,
                        "资金单位": "元",
                        "source_date": week["end_date"],
                        "provider": provider,
                        "taxonomy": "同花顺概念",
                    }
                    for row in latest_rows
                    if row.get("name") and parse_number(row.get("pct_change")) is not None
                ]
                if concept_close:
                    payload["market"]["sectors"]["concept_latest_close"] = concept_close
                    if week.get("collection_trade_date") == week["end_date"]:
                        payload["market"]["sectors"]["concept_today"] = concept_close
                    replace_dataset_status(datasets, {
                        "dataset": "concept_board:latest_close",
                        "requirement": "required",
                        "impact": "report",
                        "attempted_sources": [api_name],
                        "resolved_by": "sqlite_incremental_cache" if cache_used else api_name,
                        "status": "fallback_used",
                        "basis": "报告截止日同花顺概念收盘行情",
                        "source_date": week["end_date"],
                        "stale_days": 0,
                        "record_count": len(concept_close),
                        "reason": None,
                        "provider": "本地增量缓存" if cache_used else provider,
                        "transport": "sqlite" if cache_used else transport,
                        "cache_hit": cache_used,
                    })
            if kind == "industry":
                replace_dataset_status(datasets, {
                    "dataset": "industry_flow_history:leaders",
                    "requirement": "optional",
                    "impact": "score",
                    "attempted_sources": [api_name],
                    "resolved_by": "sqlite_incremental_cache" if cache_used else api_name,
                    "status": "ok" if cache_used else "fallback_used",
                    "basis": "全量行业逐日资金流已覆盖重点行业",
                    "source_date": week["end_date"],
                    "stale_days": 0,
                    "record_count": len(rows),
                    "reason": None,
                    "provider": "本地增量缓存" if cache_used else provider,
                    "transport": "sqlite" if cache_used else transport,
                    "cache_hit": cache_used,
                })
            used_datasets.append(health_key)

    etf_history_enabled = (
        (proxy_enabled(policy, health, "fund_daily") and proxy_enabled(policy, health, "fund_adj"))
        or any(proxy_enabled(policy, health, f"etf_return:{code}") for code in payload.get("candidate_etfs", {}).get("codes", []))
    )
    if etf_history_enabled:
        for code in payload.get("candidate_etfs", {}).get("codes", []):
            if not (policy == "shadow" or proxy_enabled(policy, health, f"etf_return:{code}") or (proxy_enabled(policy, health, "fund_daily") and proxy_enabled(policy, health, "fund_adj"))):
                continue
            status_start = len(client.statuses)
            ts_code = market_ts_code(code)
            daily = client.call(f"etf_return:{code}", "fund_daily", {"ts_code": ts_code, "start_date": start_long, "end_date": end})
            factors = client.call(f"etf_return:{code}", "fund_adj", {"ts_code": ts_code, "start_date": start_long, "end_date": end})
            histories = {
                "hfq": adjusted_etf_history(daily, factors, mode="hfq", cutoff=week["end_date"]),
                "qfq": adjusted_etf_history(daily, factors, mode="qfq", cutoff=week["end_date"]),
                "none": adjusted_etf_history(daily, factors, mode="none", cutoff=week["end_date"]),
            }
            shadow[f"etf_return:{code}"] = histories
            status = normalized_tushare_status(
                f"etf_return:{code}", client.statuses[status_start:], "fund_daily+fund_adj",
                week["end_date"], provider=provider, transport=transport,
            )
            status["promotion_eligible"] = bool(histories["hfq"] and status["promotion_eligible"])
            if policy == "auto" and histories["hfq"]:
                payload["candidate_etfs"]["history"][code] = histories
                payload["candidate_etfs"].setdefault("history_meta", {})[code] = {
                    "provider": provider,
                    "return_basis": "fund_daily+fund_adj",
                    "transport": transport,
                    "crosscheck_status": health_crosscheck_for(
                        health, f"etf_return:{code}", "fund_daily"
                    ),
                }
                status["crosscheck_status"] = payload["candidate_etfs"]["history_meta"][code]["crosscheck_status"]
                replace_dataset_status(datasets, status)
                used_datasets.append(f"etf_return:{code}")

    candidate_codes = [str(code).zfill(6) for code in payload.get("candidate_etfs", {}).get("codes", [])]
    if candidate_codes and proxy_enabled(policy, health, "etf_realtime_daily"):
        live_scope = ",".join(sorted(candidate_codes))
        live_as_of = str(week.get("collection_trade_date") or week["end_date"])
        live_rows = store.get_snapshot("etf_live_quote", live_scope, live_as_of, provider=provider) if store else None
        live_status_start = len(client.statuses)
        live_cache_hit = bool(live_rows)
        if not live_rows:
            live_rows = []
            if any(code.startswith(("5", "6")) for code in candidate_codes):
                live_rows.extend(client.call(
                    "etf_live_quote", "rt_etf_k", {"ts_code": "5*.SH", "topic": "HQ_FND_TICK"}
                ))
            if any(not code.startswith(("5", "6")) for code in candidate_codes):
                live_rows.extend(client.call("etf_live_quote", "rt_etf_k", {"ts_code": "15*.SZ"}))
            wanted = set(candidate_codes)
            live_rows = [row for row in live_rows if str(row.get("ts_code") or "")[:6] in wanted]
            if store and live_rows:
                store.put_snapshot(
                    provider,
                    "etf_live_quote",
                    live_scope,
                    live_as_of,
                    live_rows,
                    expires_at=(dt.datetime.now() + dt.timedelta(minutes=5)).isoformat(timespec="seconds"),
                )
        elif live_rows:
            client.statuses.append({
                "dataset": "etf_live_quote", "label": "etf_live_quote", "function": "sqlite_snapshot_cache",
                "provider": "本地增量缓存", "transport": "sqlite", "status": "ok",
                "cache_hit": True, "record_count": len(live_rows), "source_date": live_as_of,
            })
        shadow["etf_live_quote"] = live_rows or []
        if policy == "auto" and live_rows:
            payload["candidate_etfs"]["live_snapshot"] = {
                str(row.get("ts_code") or "")[:6]: {
                    "code": str(row.get("ts_code") or "")[:6],
                    "name": row.get("name"),
                    "price": parse_number(row.get("close")),
                    "turnover": parse_number(row.get("amount")),
                    "trade_time": row.get("trade_time"),
                    "price_source": "rt_etf_k",
                    "turnover_source": "rt_etf_k（元）",
                    "provider": provider,
                }
                for row in live_rows
                if str(row.get("ts_code") or "")[:6] in set(candidate_codes)
            }
            live_status = dataset_status(
                "etf_live_quote", client.statuses[live_status_start:],
                basis="ETF实时日线价格与成交额",
                source_date=live_as_of,
                requirement="optional",
                impact="execution",
            )
            if live_cache_hit:
                live_status.update({"status": "ok", "resolved_by": "sqlite_snapshot_cache", "provider": "本地增量缓存", "transport": "sqlite", "cache_hit": True})
            replace_dataset_status(datasets, live_status)
            used_datasets.append("etf_realtime_daily")

    sz_codes = [f"{code}.SZ" for code in candidate_codes if not code.startswith(("5", "6"))]
    if sz_codes and proxy_enabled(policy, health, "etf_iopv"):
        iopv_status_start = len(client.statuses)
        iopv_rows = client.call("etf_iopv", "rt_etf_sz_iopv", {"ts_code": ",".join(sz_codes)})
        wanted = {code[:6] for code in sz_codes}
        iopv_rows = [row for row in iopv_rows if str(row.get("ts_code") or "")[:6] in wanted]
        shadow["etf_iopv"] = iopv_rows
        if policy == "auto" and iopv_rows:
            live_map = payload["candidate_etfs"].setdefault("live_snapshot", {})
            for row in iopv_rows:
                code = str(row.get("ts_code") or "")[:6]
                target = live_map.setdefault(code, {"code": code, "provider": provider})
                target.update({
                    "price": parse_number(row.get("price")) if parse_number(row.get("price")) is not None else target.get("price"),
                    "iopv": parse_number(row.get("iopv")) if (parse_number(row.get("iopv")) or 0) > 0 else None,
                    "turnover": parse_number(row.get("amount")) if parse_number(row.get("amount")) is not None else target.get("turnover"),
                    "trade_time": row.get("trade_time") or target.get("trade_time"),
                    "iopv_source": "rt_etf_sz_iopv",
                })
            replace_dataset_status(datasets, dataset_status(
                "etf_iopv:sz", client.statuses[iopv_status_start:],
                basis="深市ETF盘中IOPV",
                source_date=str(week.get("collection_trade_date") or week["end_date"]),
                requirement="optional",
                impact="execution",
            ))
            used_datasets.append("etf_iopv")
    if any(code.startswith(("5", "6")) for code in candidate_codes):
        replace_dataset_status(datasets, {
            "dataset": "etf_iopv:sh", "requirement": "optional", "impact": "execution",
            "attempted_sources": [], "resolved_by": None, "status": "not_required",
            "basis": "沪市ETF盘中IOPV", "source_date": None, "stale_days": 0,
            "record_count": 0, "reason": "当前已接入的Tushare实时参考接口仅覆盖深市；沪市使用收盘净值溢价",
            "provider": provider, "transport": transport, "cache_hit": False,
        })

    payload["provider_policy"] = policy
    payload["provider_route"] = {
        "selected_provider": provider if used_datasets else "AkShare及公开备用源",
        "promoted_datasets": used_datasets,
        "health_created_at": health.get("created_at"),
        "source_mismatch": health.get("source_mismatch"),
        "endpoint_fingerprint": client.metadata.get("endpoint_fingerprint"),
        "credential_risk": (
            "官方Tushare Pro token仅通过官方SDK使用并必须保密。"
            if client.metadata.get("provider_mode") == "official" else
            "第三方代理凭据不是官方Tushare Pro token；不要把官方token发送给代理。"
        ),
    }
    if policy == "shadow":
        payload["provider_shadow"] = {
            "provider": provider,
            "endpoint_fingerprint": client.metadata.get("endpoint_fingerprint"),
            "datasets": shadow,
            "note": "影子数据不参与本次报告结论。",
        }
    return payload


def mock_payload(holdings: list[dict[str, Any]], week: dict[str, Any], mode: str, codes: list[str], portfolio_meta: dict[str, Any] | None = None) -> dict[str, Any]:
    funds, industry_today, concept_today, flow = _mock_payload_parts(holdings, week)
    rankings = {
        "全部": [
            {"基金代码": "020639", "基金简称": "广发半导体设备ETF联接A", "近1周": 7.2, "近1月": 24.0},
            {"基金代码": "005844", "基金简称": "东方人工智能主题混合A", "近1周": 5.8, "近1月": 18.0},
        ]
    }
    spot = [
        {"代码": "560780", "名称": "半导体设备ETF广发", "最新价": 1.03, "IOPV实时估值": 1.00, "基金折价率": -3.0, "成交额": 2.1e9, "更新时间": week["end_date"]},
        {"代码": "562590", "名称": "半导体设备ETF华夏", "最新价": 1.015, "IOPV实时估值": 1.00, "基金折价率": -1.5, "成交额": 1.5e9, "更新时间": week["end_date"]},
        {"代码": "159516", "名称": "半导体设备ETF国泰", "最新价": 1.002, "IOPV实时估值": 1.00, "基金折价率": -0.2, "成交额": 8.0e9, "更新时间": week["end_date"]},
        {"代码": "159558", "名称": "半导体设备ETF易方达", "最新价": 1.008, "IOPV实时估值": 1.00, "基金折价率": -0.8, "成交额": 3.0e9, "更新时间": week["end_date"]},
    ]
    history = {}
    for index, code in enumerate(codes):
        records = mock_series(week, 1.0, 4 + index, 10 + index * 2)
        history[code] = {"hfq": records, "qfq": records, "none": records}
    margin_exchanges = {"SSE": [], "SZSE": [], "BSE": []}
    margin_market = {"SSE": [], "SZSE": []}
    margin_end = parse_day(week["end_date"])
    for offset in range(620):
        day = (margin_end - dt.timedelta(days=619 - offset)).isoformat()
        for exchange, factor in (("SSE", 1.0), ("SZSE", 0.8)):
            financing = (10_000 + offset * 2) * 100_000_000 * factor
            lending = 100 * 100_000_000 * factor
            margin_exchanges[exchange].append({
                "trade_date": day, "exchange": exchange,
                "financing_balance": financing, "financing_buy": 400 * 100_000_000 * factor,
                "financing_repay": 390 * 100_000_000 * factor, "lending_balance": lending,
                "margin_balance": financing + lending, "unit": "元", "provider": "mock",
            })
            margin_market[exchange].append({
                "trade_date": day, "exchange": exchange,
                "float_market_cap": 500_000 * 100_000_000 * factor,
                "market_turnover": 6_000 * 100_000_000 * factor,
                "unit": "元", "provider": "mock", "market_scope": exchange,
            })
    return {
        "schema_version": SCHEMA_VERSION,
        "data_revision": DATA_REVISION,
        "as_of": dt.datetime.now().isoformat(timespec="seconds"),
        "source": "mock",
        "mode": mode,
        "week": week,
        "holdings": holdings,
        "portfolio_meta": portfolio_meta or {},
        "holdings_hash": holdings_hash(holdings),
        "funds": funds,
        "market": {
            "style_indexes": {
                "科创50": mock_daily_series(week, 1000, 4.5, 8.0, date_key="日期", value_key="收盘"),
                "沪深300": mock_daily_series(week, 4000, -1.25, 1.0, date_key="日期", value_key="收盘"),
            },
            "sectors": {"industry_today": industry_today, "concept_today": concept_today, "fund_flow": flow},
            "margin_raw": {
                "mode": "summary", "exchanges": margin_exchanges, "market_daily": margin_market,
                "concentration": {}, "policy_events": POLICY_EVENTS, "comparable_start": COMPARABLE_START,
            },
        },
        "rankings": rankings,
        "ranking_details": {
            str(row["基金代码"]).zfill(6): {"basic_info": [{"item": "最新规模", "value": "8.0亿"}], "ths_info": [{"item": "换手率", "value": "120%"}]}
            for row in rankings.get("全部", [])
        } if mode == "full" else {},
        "candidate_etfs": {
            "spot": spot,
            "history": history,
            "nav": {},
            "feeder_nav": {},
            "iopv_snapshots": {},
            "codes": codes,
            "access": {code: ETF_CHANNELS.get(code, {}) for code in codes},
        },
        "full_details": {} if mode == "quick" else {item["code"]: {"mock": True} for item in holdings},
        "warnings": ["mock data enabled"],
        "source_status": [],
    }


def collect_live(args: argparse.Namespace, holdings: list[dict[str, Any]]) -> dict[str, Any]:
    ak, import_warning = import_akshare()
    today = dt.date.today()
    explicit_end = dt.datetime.strptime(args.end_date, "%Y-%m-%d").date() if args.end_date else None
    if ak is None:
        fallback_dates = weekday_calendar(dt.date(today.year - 1, 1, 1), dt.date(today.year, 12, 31))
        week = resolve_week(fallback_dates, today, explicit_end)
        week["calendar_source"] = "weekday_fallback"
        return {
            "schema_version": SCHEMA_VERSION,
            "data_revision": DATA_REVISION,
            "as_of": dt.datetime.now().isoformat(timespec="seconds"),
            "source": "akshare",
            "mode": args.mode,
            "week": week,
            "holdings": holdings,
            "portfolio_meta": getattr(args, "portfolio_meta", {}),
            "holdings_hash": holdings_hash(holdings),
            "funds": {},
            "market": {},
            "rankings": {},
            "candidate_etfs": {"codes": args.etf},
            "warnings": [import_warning or "akshare unavailable"],
            "source_status": [{"label": "akshare import", "status": "failed"}],
        }

    shared_cache_root = Path(args.cache_root)
    request_cache_root = Path(args.cache_dir) if args.cache_dir else shared_cache_root / "requests"
    store = CacheStore(shared_cache_root)
    run_id = stable_hash({"as_of": dt.datetime.now().isoformat(), "holdings": holdings, "end_date": args.end_date})[:16]
    cache_audit: list[dict[str, Any]] = []
    proxy_client, proxy_health = create_tushare_client(args, request_cache_root, {"stage": "weekly", "mode": args.mode})
    tushare_provider = (
        str(proxy_client.metadata.get("provider") or TUSHARE_PROVIDER)
        if proxy_client else TUSHARE_PROVIDER
    )
    tushare_transport = (
        str(proxy_client.metadata.get("transport") or "unknown")
        if proxy_client else "unknown"
    )
    calendar_client = AkshareClient(
        ak,
        request_cache_root / "calendar",
        timeout=args.timeout,
        retries=args.retries,
        refresh=args.refresh,
        context={"calendar": True},
    )
    trade_dates: list[dt.date] = []
    calendar_source = ""
    if proxy_client and proxy_enabled(args.provider_policy, proxy_health, "trade_calendar"):
        anchor = explicit_end or today
        proxy_calendar = proxy_client.call(
            "trade_calendar", "trade_cal",
            {
                "exchange": "SSE",
                "start_date": dt.date(anchor.year - 1, 1, 1).strftime("%Y%m%d"),
                "end_date": dt.date(anchor.year, 12, 31).strftime("%Y%m%d"),
                "is_open": "1",
            },
        )
        proxy_dates = extract_trade_dates(proxy_calendar)
        if args.provider_policy == "auto" and proxy_dates:
            trade_dates, calendar_source = proxy_dates, tushare_provider
    if not trade_dates:
        trade_dates, calendar_source = collect_trade_calendar(calendar_client, today, explicit_end)
    week = resolve_week(trade_dates, today, explicit_end)
    week["calendar_source"] = calendar_source
    current_trade_dates = [day for day in trade_dates if day <= today]
    week["collection_trade_date"] = (current_trade_dates[-1] if current_trade_dates else today).isoformat()
    flow_dates = history_trade_dates(trade_dates, parse_day(week["end_date"]), args.history_weeks)
    week["history_start_date"] = flow_dates[0].isoformat() if flow_dates else week["baseline_date"]
    week["history_weeks"] = args.history_weeks
    period_key = f"{week['baseline_date']}_{week['end_date']}"
    client = AkshareClient(
        ak,
        request_cache_root / period_key,
        timeout=min(args.timeout, 8) if args.mode == "quick" else args.timeout,
        retries=0 if args.mode == "quick" else args.retries,
        refresh=args.refresh,
        context={"week": week, "mode": args.mode, "etfs": sorted(args.etf)},
    )
    if calendar_source == tushare_provider and proxy_client:
        datasets = [normalized_tushare_status(
            "trade_calendar",
            [row for row in proxy_client.statuses if row.get("dataset") == "trade_calendar"],
            "A股交易日历",
            provider=tushare_provider,
            transport=tushare_transport,
        )]
    else:
        datasets = [dataset_status("trade_calendar", calendar_client.statuses, basis="A股交易日历")]

    funds = {}
    for holding in holdings:
        code = holding["code"]
        cached_nav = []
        if not args.refresh and "fund_nav" not in args.refresh_dataset and f"fund_nav:{code}" not in args.refresh_dataset:
            cached_nav = store.get_series("fund_nav", code, start_date=week["history_start_date"], end_date=week["end_date"])
        cached_dates = extract_trade_dates(cached_nav)
        if cached_nav and cached_dates and cached_dates[-1] >= parse_day(week["end_date"]):
            funds[code] = {"nav": cached_nav, "provider": "本地增量缓存"}
            status = {
                "dataset": f"fund_nav:{code}", "attempted_sources": ["sqlite_incremental_cache"],
                "resolved_by": "sqlite_incremental_cache", "status": "ok", "basis": "共享历史净值缓存",
                "source_date": cached_dates[-1].isoformat(), "stale_days": 0, "record_count": len(cached_nav),
                "reason": None, "provider": "本地增量缓存", "transport": "sqlite", "cache_hit": True,
            }
            datasets.append(status)
            cache_audit.append(status)
            continue
        status_start = len(client.statuses)
        funds[code] = {
            "nav": client.call(f"{code} fund NAV", "fund_open_fund_info_em", [{"symbol": code, "indicator": "累计净值走势"}, {"symbol": code, "indicator": "单位净值走势"}])
        }
        datasets.append(dataset_status(f"fund_nav:{code}", client.statuses[status_start:], basis="累计净值优先", source_date=week["end_date"]))

    ranking_start = len(client.statuses)
    rankings = collect_rankings(client)
    report_end = week["end_date"]
    for group, rows in list(rankings.items()):
        rankings[group] = [
            row for row in rows
            if not (row.get("日期") or row.get("净值日期"))
            or str(row.get("日期") or row.get("净值日期"))[:10] <= report_end
        ]
    datasets.append(dataset_status("weekly_fund_rankings", client.statuses[ranking_start:], basis="近1周基金排行", source_date=week["end_date"]))

    profile_dir = shared_cache_root / "profiles"
    profile_codes = list(dict.fromkeys([item["code"] for item in holdings] + ranking_candidate_codes(rankings)))
    fund_profiles: dict[str, Any] = {}
    for code in profile_codes:
        if args.mode == "full":
            status_start = len(client.statuses)
            detail = collect_profile(client, code, f"fund profile {code}")
            if any(detail.values()):
                fund_profiles[code] = save_profile_cache(profile_dir, code, detail)
            else:
                fund_profiles[code] = load_profile_cache(profile_dir, code)
            profile_dataset = dataset_status(f"fund_profile:{code}", client.statuses[status_start:], basis="基本资料/规模/换手/季度持仓")
            if (fund_profiles.get(code) or {}).get("profile_status") == "partial_profile":
                profile_dataset["status"] = "partial"
            datasets.append(profile_dataset)
        else:
            cached = load_profile_cache(profile_dir, code)
            if cached:
                fund_profiles[code] = cached
                datasets.append({
                    "dataset": f"fund_profile:{code}", "attempted_sources": ["fund_profile_cache"],
                    "resolved_by": "fund_profile_cache", "status": "partial" if cached.get("profile_status") == "partial_profile" else "ok",
                    "basis": "缓存基金画像", "source_date": cached.get("disclosure_date"),
                    "stale_days": cached.get("stale_days", 0), "record_count": sum((cached.get("profile_components") or {}).values()),
                    "reason": None, "provider": "本地季度画像缓存", "transport": "local_cache",
                    "endpoint_fingerprint": None, "crosscheck_status": "cached_public_disclosure", "promotion_eligible": True,
                })

    details = {code: profile.get("detail") or {} for code, profile in fund_profiles.items() if code in {item["code"] for item in holdings}}
    ranking_details = {code: profile.get("detail") or {} for code, profile in fund_profiles.items() if code in set(ranking_candidate_codes(rankings))}
    style_indexes, style_index_meta = collect_style_indexes(client, week, datasets, store, args.refresh_dataset)
    sectors = collect_sectors(client, datasets, week, shared_cache_root / "sector_snapshots", args.mode)
    candidate_etfs = collect_etfs(client, args.etf, week, datasets, args.mode)
    margin_raw = collect_margin_leverage_data(
        client, proxy_client, proxy_health, args.provider_policy, store, datasets, week,
        args.margin_mode, args.refresh_dataset,
    )

    payload = {
        "schema_version": SCHEMA_VERSION,
        "data_revision": DATA_REVISION,
        "as_of": dt.datetime.now().isoformat(timespec="seconds"),
        "source": "multi_source",
        "mode": args.mode,
        "week": week,
        "holdings": holdings,
        "portfolio_meta": getattr(args, "portfolio_meta", {}),
        "holdings_hash": holdings_hash(holdings),
        "funds": funds,
        "market": {
            "style_indexes": style_indexes,
            "style_index_meta": style_index_meta,
            "sectors": sectors,
            "margin_raw": margin_raw,
        },
        "rankings": rankings,
        "ranking_details": ranking_details,
        "candidate_etfs": candidate_etfs,
        "full_details": details,
        "fund_profiles": fund_profiles,
        "warnings": unresolved_warnings(datasets),
        "dataset_status": datasets,
        "source_status": calendar_client.statuses + client.statuses,
    }
    if proxy_client:
        payload = apply_tushare_overlay(
            payload, proxy_client, proxy_health, args.provider_policy, datasets, profile_dir,
            store, flow_dates, args.refresh_dataset,
        )
        payload["source_status"] = calendar_client.statuses + client.statuses + proxy_client.statuses
        payload["warnings"] = unresolved_warnings(datasets)
        if args.provider_policy == "shadow" and margin_raw.get("proxy_shadow"):
            payload.setdefault("provider_shadow", {}).setdefault("datasets", {}).update(margin_raw["proxy_shadow"])
    else:
        cached_flow_datasets = []
        if args.provider_policy == "auto":
            cached_flow_datasets = apply_cached_sector_flow_overlay(
                payload, datasets, store, flow_dates, args.refresh_dataset
            )
            payload["warnings"] = unresolved_warnings(datasets)
        payload["provider_policy"] = args.provider_policy
        payload["provider_route"] = {
            "selected_provider": "AkShare及公开备用源 + 已验证本地历史" if cached_flow_datasets else "AkShare及公开备用源",
            "promoted_datasets": cached_flow_datasets,
            "health_created_at": proxy_health.get("created_at"),
            "source_mismatch": proxy_health.get("source_mismatch"),
            "runtime_status": proxy_health.get("runtime_unavailable") or "未配置Tushare凭据",
        }
    cache_counts = persist_payload_to_cache(store, payload)
    all_audit = cache_audit + calendar_client.statuses + client.statuses + (proxy_client.statuses if proxy_client else [])
    store.record_audit(run_id, all_audit)
    historical_logical = [
        row for row in datasets
        if (
            str(row.get("dataset") or "").startswith(("fund_nav:", "style_index:", "etf_return:", "margin_summary:", "market_daily_info:"))
            or row.get("dataset") == "weekly_fund_rankings"
            or str(row.get("dataset") or "") in {
                "industry_flow:报告期末日", "industry_flow:5日", "industry_flow:10日",
                "concept_flow:报告期末日", "concept_flow:5日", "concept_flow:10日",
            }
        )
    ]
    logical_hits = sum(bool(row.get("cache_hit")) for row in historical_logical)
    cache_stats = store.cache_stats(run_id)
    cache_stats.update({
        "historical_logical_datasets": len(historical_logical),
        "historical_logical_hits": logical_hits,
        "historical_hit_rate": logical_hits / len(historical_logical) if historical_logical else None,
        "realtime_excluded": True,
    })
    payload["cache"] = {
        "database": str(store.path), "root": str(shared_cache_root), "run_id": run_id,
        "writes": cache_counts, "stats": cache_stats, "history_weeks": args.history_weeks,
    }
    store.close()
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--holdings", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--mode", choices=["quick", "full"], default="quick")
    parser.add_argument("--end-date")
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--cache-dir", default=None, help="Legacy request-cache path; defaults below --cache-root")
    parser.add_argument("--cache-root", default="work/cache/fund-rotation")
    parser.add_argument("--history-weeks", type=int, default=3)
    parser.add_argument("--refresh-dataset", action="append", default=[])
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--etf", action="append", default=None)
    parser.add_argument("--provider-policy", choices=["auto", "shadow", "akshare-only"], default="auto")
    parser.add_argument("--margin-mode", choices=["off", "summary", "full"], default="summary")
    parser.add_argument("--tushare-health", default="work/tushare_proxy_health.json")
    parser.add_argument("--prompt-tushare-token", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.prompt_tushare_token and not os.environ.get("TUSHARE_TOKEN"):
        # Emit a non-secret readiness marker before getpass switches to the TTY.
        # This lets non-interactive orchestrators attach without exposing input.
        print("TUSHARE_TOKEN_INPUT_READY", flush=True)
        os.environ["TUSHARE_TOKEN"] = getpass.getpass("TUSHARE_TOKEN: ")
    args.etf = list(dict.fromkeys(args.etf or DEFAULT_CANDIDATE_ETFS))

    raw_holdings = load_json(args.holdings)
    holdings = normalize_holdings(raw_holdings)
    args.portfolio_meta = holdings_metadata(raw_holdings)
    if args.mock:
        today = dt.date.today()
        explicit = dt.datetime.strptime(args.end_date, "%Y-%m-%d").date() if args.end_date else None
        calendar = weekday_calendar(dt.date(today.year - 1, 1, 1), dt.date(today.year, 12, 31))
        week = resolve_week(calendar, today, explicit)
        week["calendar_source"] = "mock_weekday_calendar"
        payload = mock_payload(holdings, week, args.mode, args.etf, args.portfolio_meta)
        if args.margin_mode == "off":
            payload["market"]["margin_raw"] = {
                "mode": "off", "exchanges": {}, "market_daily": {},
                "concentration": {}, "policy_events": POLICY_EVENTS,
            }
    else:
        payload = collect_live(args, holdings)
    write_json(args.output, payload)


if __name__ == "__main__":
    main()
