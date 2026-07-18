#!/usr/bin/env python3
"""Isolated health and permission check for the fixed third-party Tushare proxy."""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import hashlib
import json
import multiprocessing as mp
import os
import queue as queue_module
import statistics
import sys
import time
from pathlib import Path
from typing import Any

from data_access import df_to_records, load_json, parse_number, write_json
from tushare_proxy import DEFAULT_HTTP_URL, PROVIDER, TRANSPORT, create_pro


CRITICAL_DATASETS = {
    "sample_index_basic", "sample_pro_bar", "fund_nav", "fund_portfolio",
    "industry_flow", "concept_flow", "margin_summary", "market_daily_info",
}
REQUIRE_SHADOW_CROSSCHECK = {
    "fund_nav", "fund_portfolio", "fund_daily", "fund_adj", "style_indexes",
    "margin_summary", "market_daily_info",
}
CACHEABLE_BACKGROUND_DATASETS = {"fund_portfolio", "industry_flow", "concept_flow"}
FLOW_DATASETS = {"industry_flow", "concept_flow"}
DATE_FIELDS = ("trade_date", "nav_date", "ann_date", "end_date", "cal_date")


def _specs(group: str, today: dt.date) -> list[dict[str, Any]]:
    start = (today - dt.timedelta(days=50)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    base = [
        {"dataset": "sample_index_basic", "api": "index_basic", "kwargs": {"limit": 5}, "required": ["ts_code"]},
        {"dataset": "sample_pro_bar", "api": "pro_bar", "kwargs": {"ts_code": "000001.SZ", "limit": 3}, "required": ["trade_date", "close"], "max_age_days": 10},
    ]
    foundation = [
        {"dataset": "trade_calendar", "api": "trade_cal", "kwargs": {"exchange": "SSE", "start_date": start, "end_date": end, "is_open": "1"}, "required": ["cal_date"], "max_age_days": 10},
        {"dataset": "fund_basic", "api": "fund_basic", "kwargs": {"market": "O", "status": "L", "fields": "ts_code,name,fund_type,status", "limit": 1000, "offset": 0}, "required": ["ts_code", "name"]},
        {"dataset": "fund_nav", "api": "fund_nav", "kwargs": {"ts_code": "__RESOLVED_FUND__", "start_date": start, "end_date": end}, "required_any": ["adj_nav", "accum_nav"], "max_age_days": 15},
        {"dataset": "fund_portfolio", "api": "fund_portfolio", "kwargs": {"ts_code": "__RESOLVED_FUND__"}, "required": ["ts_code", "end_date", "symbol"], "max_age_days": 180},
        {"dataset": "fund_daily", "api": "fund_daily", "kwargs": {"ts_code": "560780.SH", "start_date": start, "end_date": end}, "required": ["trade_date", "close"], "max_age_days": 10},
        {"dataset": "fund_adj", "api": "fund_adj", "kwargs": {"ts_code": "560780.SH", "start_date": start, "end_date": end}, "required": ["trade_date", "adj_factor"], "max_age_days": 10},
        {"dataset": "style_indexes", "api": "index_daily", "kwargs": {"ts_code": "000300.SH", "start_date": start, "end_date": end}, "required": ["trade_date", "close"], "max_age_days": 10},
        {"dataset": "industry_flow", "api": "moneyflow_ind_ths", "kwargs": {"start_date": start, "end_date": end}, "required": ["trade_date", "industry", "net_amount"], "max_age_days": 10},
        {"dataset": "concept_flow", "api": "moneyflow_cnt_ths", "kwargs": {"start_date": start, "end_date": end}, "required": ["trade_date", "name", "industry_index", "net_amount"], "max_age_days": 10},
        {"dataset": "margin_summary", "api": "margin", "kwargs": {"exchange_id": "SSE", "start_date": start, "end_date": end}, "required": ["trade_date", "exchange_id", "rzye", "rzrqye"], "max_age_days": 10},
        {"dataset": "market_daily_info", "api": "daily_info", "kwargs": {"ts_code": "SH_A", "exchange": "SH", "start_date": start, "end_date": end}, "required": ["trade_date", "ts_code", "float_mv", "amount"], "max_age_days": 10},
    ]
    optional = [
        {"dataset": "etf_realtime_daily", "api": "rt_etf_k", "kwargs": {"ts_code": "5*.SH", "topic": "HQ_FND_TICK"}, "required_any": ["close", "price"], "optional": True},
        {"dataset": "etf_iopv", "api": "rt_etf_sz_iopv", "kwargs": {"ts_code": "159516.SZ,159558.SZ"}, "required_any": ["iopv", "price"], "optional": True},
        {"dataset": "etf_share", "api": "fund_share", "kwargs": {"ts_code": "560780.SH", "start_date": start, "end_date": end}, "required_any": ["fd_share", "fund_share"], "optional": True},
        {"dataset": "etf_realtime_minute", "api": "rt_etf_min", "kwargs": {"ts_code": "560780.SH"}, "required_any": ["close", "price"], "optional": True},
        {"dataset": "margin_concentration", "api": "margin_detail", "kwargs": {"trade_date": end}, "required": ["trade_date", "ts_code", "rzye"], "optional": True, "max_age_days": 10},
    ]
    if group == "sample":
        return base
    if group == "foundation":
        return base + foundation
    if group == "optional":
        return optional
    return base + foundation + optional


def _resolve_fund(pro: Any, code: str) -> str:
    for market in ("O", "E"):
        for page in range(30):
            records = df_to_records(
                pro.fund_basic(
                    market=market, status="L", fields="ts_code,name,fund_type,status",
                    limit=1000, offset=page * 1000,
                )
            )
            matches = [str(row.get("ts_code")) for row in records if str(row.get("ts_code", "")).split(".")[0] == code]
            if matches:
                return matches[0]
            if len(records) < 1000:
                break
    raise ValueError(f"fund_basic did not resolve code {code}")


def _execute(token: str, endpoint: str, spec: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    try:
        pro, ts, metadata = create_pro(token, endpoint)
        api = spec["api"]
        kwargs = dict(spec.get("kwargs") or {})
        if api == "pro_bar":
            value = ts.pro_bar(api=pro, **kwargs)
        elif api == "resolved_fund_nav":
            code = _resolve_fund(pro, kwargs.pop("code"))
            value = pro.fund_nav(ts_code=code, **kwargs)
        elif api == "resolved_fund_portfolio":
            code = _resolve_fund(pro, kwargs.pop("code"))
            value = pro.fund_portfolio(ts_code=code, **kwargs)
        else:
            function = getattr(pro, api, None)
            if function is None:
                raise AttributeError(f"proxy SDK does not expose {api}")
            value = function(**kwargs)
        records = df_to_records(value)
        fields = sorted({str(key) for row in records[:20] for key in row})
        required = spec.get("required") or []
        required_any = spec.get("required_any") or []
        missing = [field for field in required if field not in fields]
        if required_any and not any(field in fields for field in required_any):
            missing.append("one_of:" + ",".join(required_any))
        latest = max(
            (str(row.get(field)) for row in records for field in DATE_FIELDS if row.get(field)),
            default=None,
        )
        latest_day = None
        if latest:
            try:
                latest_day = dt.datetime.strptime(str(latest)[:10].replace("-", ""), "%Y%m%d").date()
            except ValueError:
                latest_day = None
        stale = bool(spec.get("max_age_days") is not None and latest_day and (dt.date.today() - latest_day).days > spec["max_age_days"])
        if not records:
            status, reason = "failed", "empty_data"
        elif missing:
            status, reason = "failed", "missing_fields:" + ",".join(missing)
        elif stale:
            status, reason = "failed", f"stale_data:{latest_day.isoformat()}"
        else:
            status, reason = "ok", None
        resolved_ts_code = next(
            (str(row.get("ts_code")) for row in records if str(row.get("ts_code") or "").split(".")[0] == "001170"),
            None,
        )
        content_fingerprint = None
        flow_numeric_coverage = None
        flow_duplicate_keys = None
        flow_max_abs_net_amount = None
        flow_positive_count = None
        flow_negative_count = None
        if spec["dataset"] in FLOW_DATASETS and records:
            name_key = "industry" if spec["dataset"] == "industry_flow" else "name"
            normalized = []
            keys = []
            numeric_values = []
            for row in records:
                day = str(row.get("trade_date") or "")
                name = str(row.get(name_key) or "").strip()
                value = parse_number(row.get("net_amount"))
                change = parse_number(row.get("pct_change"))
                keys.append((day, name))
                if value is not None:
                    numeric_values.append(value)
                normalized.append((day, name, value, change))
            encoded = json.dumps(sorted(normalized), ensure_ascii=False, separators=(",", ":"), default=str)
            content_fingerprint = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]
            flow_numeric_coverage = len(numeric_values) / len(records)
            flow_duplicate_keys = len(keys) - len(set(keys))
            flow_max_abs_net_amount = max((abs(value) for value in numeric_values), default=None)
            flow_positive_count = sum(value > 0 for value in numeric_values)
            flow_negative_count = sum(value < 0 for value in numeric_values)
        return {
            "status": status,
            "reason": reason,
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
            "row_count": len(records),
            "fields": fields,
            "latest_date": latest,
            "sdk_version": metadata["sdk_version"],
            "resolved_ts_code": resolved_ts_code,
            "content_fingerprint": content_fingerprint,
            "flow_numeric_coverage": flow_numeric_coverage,
            "flow_duplicate_keys": flow_duplicate_keys,
            "flow_max_abs_net_amount": flow_max_abs_net_amount,
            "flow_positive_count": flow_positive_count,
            "flow_negative_count": flow_negative_count,
            "provider": PROVIDER,
            "transport": TRANSPORT,
        }
    except Exception as exc:
        safe_reason = f"{type(exc).__name__}: {str(exc)[:300]}".replace(token, "[REDACTED]")
        return {
            "status": "failed",
            "reason": safe_reason,
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
            "row_count": 0,
            "fields": [],
            "latest_date": None,
            "provider": PROVIDER,
            "transport": TRANSPORT,
        }


def _worker(queue: Any, token: str, endpoint: str, spec: dict[str, Any]) -> None:
    queue.put(_execute(token, endpoint, spec))


def isolated_call(token: str, endpoint: str, spec: dict[str, Any], timeout: float) -> dict[str, Any]:
    context = mp.get_context("spawn")
    queue = context.Queue()
    process = context.Process(target=_worker, args=(queue, token, endpoint, spec))
    process.start()
    process.join(timeout)
    if process.is_alive():
        process.terminate()
        process.join(2)
        return {"status": "failed", "reason": f"hard_timeout:{timeout}s", "latency_ms": timeout * 1000, "row_count": 0, "fields": [], "latest_date": None}
    try:
        return queue.get(timeout=1)
    except queue_module.Empty:
        return {"status": "failed", "reason": f"worker_exit:{process.exitcode}", "latency_ms": None, "row_count": 0, "fields": [], "latest_date": None}


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    return ordered[index]


def summarize(dataset: str, spec: dict[str, Any], attempts: list[dict[str, Any]], rounds: int) -> dict[str, Any]:
    successful = [row for row in attempts if row.get("status") == "ok"]
    latencies = [float(row["latency_ms"]) for row in successful if row.get("latency_ms") is not None]
    success_rate = len(successful) / rounds if rounds else 0.0
    strict = dataset in CRITICAL_DATASETS
    median_latency = statistics.median(latencies) if latencies else None
    p95_latency = percentile(latencies, 0.95)
    complete_success = len(successful) == rounds if strict else success_rate >= 0.95
    latency_required = dataset not in {"sample_index_basic", "sample_pro_bar"}
    quick_latency_ok = bool(median_latency is not None and p95_latency is not None and median_latency <= 3000 and p95_latency <= 10000)
    background_latency_ok = bool(median_latency is not None and p95_latency is not None and median_latency <= 8000 and p95_latency <= 15000)
    quick_eligible = bool(complete_success and (quick_latency_ok or not latency_required))
    operational = bool(
        complete_success
        and (
            quick_latency_ok
            or not latency_required
            or (dataset in CACHEABLE_BACKGROUND_DATASETS and background_latency_ok)
        )
    )
    consistency_status = "not_applicable"
    flow_checks: dict[str, Any] = {}
    if dataset in FLOW_DATASETS:
        fingerprints = [row.get("content_fingerprint") for row in successful if row.get("content_fingerprint")]
        coverages = [float(row.get("flow_numeric_coverage") or 0) for row in successful]
        duplicates = [int(row.get("flow_duplicate_keys") or 0) for row in successful]
        max_values = [float(row.get("flow_max_abs_net_amount") or 0) for row in successful]
        directions_present = all(
            int(row.get("flow_positive_count") or 0) > 0 and int(row.get("flow_negative_count") or 0) > 0
            for row in successful
        ) if successful else False
        consistency_ok = bool(
            len(fingerprints) == rounds
            and len(set(fingerprints)) == 1
            and coverages
            and min(coverages) >= 0.99
            and max(duplicates, default=1) == 0
            and max(max_values, default=100001) <= 100000
            and directions_present
        )
        consistency_status = "consistent" if consistency_ok else "failed"
        operational = bool(operational and consistency_ok)
        quick_eligible = bool(quick_eligible and consistency_ok)
        flow_checks = {
            "content_consistent": len(fingerprints) == rounds and len(set(fingerprints)) == 1,
            "numeric_coverage_min": round(min(coverages), 4) if coverages else 0,
            "duplicate_keys_max": max(duplicates, default=0),
            "max_abs_net_amount_yi": max(max_values, default=None),
            "positive_and_negative_present": directions_present,
        }
    crosscheck_status = "pending_shadow_crosscheck" if dataset in REQUIRE_SHADOW_CROSSCHECK and operational else "not_required" if operational else "not_run"
    eligible = operational and dataset not in REQUIRE_SHADOW_CROSSCHECK
    return {
        "api": spec["api"],
        "required_for_foundation": not spec.get("optional", False),
        "successes": len(successful),
        "rounds": rounds,
        "success_rate": round(success_rate, 4),
        "median_latency_ms": round(median_latency, 1) if median_latency is not None else None,
        "p95_latency_ms": round(p95_latency, 1) if p95_latency is not None else None,
        "latest_date": max((row.get("latest_date") for row in successful if row.get("latest_date")), default=None),
        "row_count_min": min((row.get("row_count", 0) for row in successful), default=0),
        "fields": sorted({field for row in successful for field in row.get("fields", [])}),
        "permission": "available" if successful else "unavailable_or_denied",
        "operational_eligible": bool(operational and successful),
        "quick_eligible": bool(quick_eligible and successful),
        "usage_scope": "quick" if quick_eligible else "cached/background" if operational else "unavailable",
        "consistency_status": consistency_status,
        "flow_checks": flow_checks,
        "crosscheck_status": crosscheck_status,
        "promotion_eligible": bool(eligible and successful),
        "attempts": attempts,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--group", choices=["sample", "foundation", "optional", "all"], default="all")
    parser.add_argument("--dataset", action="append", help="run only selected dataset names")
    parser.add_argument("--base-health", type=Path, help="merge selected retests into an existing health file")
    parser.add_argument("--endpoint", default=os.environ.get("TUSHARE_HTTP_URL", DEFAULT_HTTP_URL), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error("--rounds must be positive")
    token = os.environ.get("TUSHARE_TOKEN") or getpass.getpass("TUSHARE_TOKEN: ")
    if not token:
        parser.error("TUSHARE_TOKEN is required")

    _, _, metadata = create_pro(token, args.endpoint)
    datasets = {}
    specs = _specs(args.group, dt.date.today())
    if args.dataset:
        requested = set(args.dataset)
        specs = [spec for spec in specs if spec["dataset"] in requested]
        missing = requested - {spec["dataset"] for spec in specs}
        if missing:
            parser.error("unknown --dataset: " + ",".join(sorted(missing)))
    resolved_fund_code = None
    resolution_audit: list[dict[str, Any]] = []
    for source_spec in specs:
        spec = dict(source_spec)
        spec["kwargs"] = dict(source_spec.get("kwargs") or {})
        if spec["kwargs"].get("ts_code") == "__RESOLVED_FUND__":
            if not resolved_fund_code:
                attempts = [{"status": "failed", "reason": "fund_basic_resolution_missing", "latency_ms": None, "row_count": 0, "fields": [], "latest_date": None}] * args.rounds
                datasets[spec["dataset"]] = summarize(spec["dataset"], spec, attempts, args.rounds)
                print(f"FAIL {spec['dataset']:<22} 0/{args.rounds}")
                continue
            spec["kwargs"]["ts_code"] = resolved_fund_code
        attempts = [isolated_call(token, args.endpoint, spec, args.timeout) for _ in range(args.rounds)]
        datasets[spec["dataset"]] = summarize(spec["dataset"], spec, attempts, args.rounds)
        if spec["dataset"] == "fund_basic":
            resolved_fund_code = next((row.get("resolved_ts_code") for row in attempts if row.get("resolved_ts_code")), None)
            if not resolved_fund_code and any(row.get("status") == "ok" for row in attempts):
                for page in range(1, 30):
                    resolution_spec = {
                        "dataset": "fund_code_resolution",
                        "api": "fund_basic",
                        "kwargs": {
                            "market": "O", "status": "L", "fields": "ts_code,name,fund_type,status",
                            "limit": 1000, "offset": page * 1000,
                        },
                        "required": ["ts_code", "name"],
                    }
                    result = isolated_call(token, args.endpoint, resolution_spec, args.timeout)
                    resolution_audit.append({
                        "page": page, "status": result.get("status"), "row_count": result.get("row_count"),
                        "latency_ms": result.get("latency_ms"), "resolved": bool(result.get("resolved_ts_code")),
                    })
                    if result.get("resolved_ts_code"):
                        resolved_fund_code = result["resolved_ts_code"]
                        break
                    if (result.get("row_count") or 0) < 1000:
                        break
        summary = datasets[spec["dataset"]]
        status = "PASS" if summary["promotion_eligible"] else "PEND" if summary["operational_eligible"] else "FAIL"
        print(f"{status:4} {spec['dataset']:<22} {datasets[spec['dataset']]['successes']}/{args.rounds}")

    base_payload: dict[str, Any] = {}
    if args.base_health:
        try:
            base_payload = load_json(args.base_health)
        except (OSError, ValueError, TypeError) as exc:
            parser.error(f"cannot read --base-health: {exc}")
        merged = dict(base_payload.get("datasets") or {})
        merged.update(datasets)
        datasets = merged
    foundation = [row for row in datasets.values() if row["required_for_foundation"]]
    payload = {
        "schema_version": 1,
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "provider": PROVIDER,
        "transport": TRANSPORT,
        "endpoint_fingerprint": metadata["endpoint_fingerprint"],
        "token_fingerprint": metadata["token_fingerprint"],
        "sdk_version": metadata["sdk_version"],
        "group": args.group,
        "rounds": args.rounds,
        "hard_timeout_seconds": args.timeout,
        "base_health": str(args.base_health) if args.base_health else None,
        "credential_risk": "当前凭据曾在聊天中暴露；自动任务启用前必须更换。",
        "operational_ready": bool(foundation and all(row["operational_eligible"] for row in foundation)),
        "foundation_ready": bool(foundation and all(row["promotion_eligible"] for row in foundation)),
        "datasets": datasets,
        "fund_code_resolution": {
            "target_code": "001170",
            "resolved": bool(resolved_fund_code),
            "resolved_ts_code": resolved_fund_code,
            "pages": resolution_audit,
        } if resolved_fund_code or resolution_audit or not base_payload else base_payload.get("fund_code_resolution", {}),
    }
    write_json(args.output, payload)
    print(json.dumps({"foundation_ready": payload["foundation_ready"], "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
