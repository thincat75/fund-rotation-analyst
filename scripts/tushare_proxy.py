#!/usr/bin/env python3
"""Safe access and normalization for the authenticated third-party Tushare proxy."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from data_access import df_to_records, is_retryable, load_json, parse_number, wall_clock_timeout, write_json


PROVIDER = "第三方 Tushare 代理"
TRANSPORT = "http"
DEFAULT_HTTP_URL = "http://cheap-host1.cheapyun.com:24145"
ALLOWED_ENDPOINTS = {("cheap-host1.cheapyun.com", 24145)}
CLIENT_VERSION = "2.5"
FUND_MASTER_FIELDS = "ts_code,name,fund_type,invest_type,type,benchmark,status,m_fee,c_fee"


class ProxyConfigurationError(ValueError):
    """Raised when proxy credentials or endpoint configuration is unsafe or missing."""


def secret_fingerprint(value: str, length: int = 8) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def endpoint_fingerprint(value: str) -> str:
    return secret_fingerprint(value, 12)


def validate_endpoint(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    if parsed.scheme != "http" or (parsed.hostname, parsed.port) not in ALLOWED_ENDPOINTS:
        raise ProxyConfigurationError("TUSHARE_HTTP_URL is not the approved fixed HTTP endpoint")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ProxyConfigurationError("TUSHARE_HTTP_URL must contain only the approved host and port")
    return endpoint.rstrip("/")


def create_pro(
    token: str | None = None,
    endpoint: str | None = None,
    *,
    ts_module: Any | None = None,
) -> tuple[Any, Any, dict[str, Any]]:
    """Create the proxy client using the seller-required initialization contract."""
    token = token or os.environ.get("TUSHARE_TOKEN")
    endpoint = validate_endpoint(endpoint or os.environ.get("TUSHARE_HTTP_URL", DEFAULT_HTTP_URL))
    if not token:
        raise ProxyConfigurationError("TUSHARE_TOKEN is required")
    if ts_module is None:
        import tushare as ts_module  # type: ignore

    pro = ts_module.pro_api(token)
    # The seller requires this private SDK field. Keep the compatibility risk here.
    pro._DataApi__http_url = endpoint
    metadata = {
        "provider": PROVIDER,
        "transport": TRANSPORT,
        "endpoint_fingerprint": endpoint_fingerprint(endpoint),
        "token_fingerprint": secret_fingerprint(token),
        "sdk_version": getattr(ts_module, "__version__", "unknown"),
        "client_version": CLIENT_VERSION,
    }
    return pro, ts_module, metadata


def normalize_ts_code(value: Any) -> str:
    return str(value or "").strip().upper()


def resolve_fund_ts_code(rows: list[dict[str, Any]], code: str) -> str | None:
    code = str(code).zfill(6)
    candidates = []
    for row in rows:
        ts_code = normalize_ts_code(row.get("ts_code") or row.get("基金代码"))
        if ts_code.split(".")[0] == code:
            candidates.append(ts_code)
    if not candidates:
        return None
    suffix_order = {"OF": 0, "SH": 1, "SZ": 2}
    return sorted(candidates, key=lambda value: suffix_order.get(value.split(".")[-1], 9))[0]


def collect_fund_master(
    client: "TushareProxyClient",
    codes: list[str],
    *,
    page_size: int = 1000,
    max_pages: int = 30,
) -> list[dict[str, Any]]:
    """Resolve requested funds with a persistent compact code index."""
    wanted = {str(code).zfill(6) for code in codes}
    index_path = client.cache_dir / f"fund_master_index_{CLIENT_VERSION}.json"
    index: dict[str, dict[str, Any]] = {}
    if index_path.exists() and not client.refresh:
        try:
            cached = load_json(index_path)
            index = cached if isinstance(cached, dict) else {}
        except (OSError, ValueError):
            index = {}
    if wanted <= set(index):
        rows = [index[code] for code in sorted(wanted)]
        client.statuses.append({
            "dataset": "fund_basic", "label": "fund_basic", "function": "fund_master_index",
            "provider": PROVIDER, "transport": TRANSPORT,
            "endpoint_fingerprint": client.metadata["endpoint_fingerprint"], "status": "ok",
            "record_count": len(rows), "cache_hit": True, "pages_fetched": 0,
        })
        return rows

    status_start = len(client.statuses)
    found: set[str] = set(index) & wanted
    for market in ("O", "E"):
        for page in range(max_pages):
            rows = client.call(
                "fund_basic",
                "fund_basic",
                {
                    "market": market,
                    "status": "L",
                    "fields": FUND_MASTER_FIELDS,
                    "limit": page_size,
                    "offset": page * page_size,
                },
            )
            for row in rows:
                code = str(row.get("ts_code") or "").split(".")[0]
                if code:
                    index[code] = row
                    if code in wanted:
                        found.add(code)
            if found >= wanted or len(rows) < page_size:
                break
        if found >= wanted:
            break
    write_json(index_path, index)
    page_statuses = client.statuses[status_start:]
    del client.statuses[status_start:]
    matched = [index[code] for code in sorted(wanted & set(index))]
    client.statuses.append({
        "dataset": "fund_basic", "label": "fund_basic", "function": "fund_basic_paginated",
        "provider": PROVIDER, "transport": TRANSPORT,
        "endpoint_fingerprint": client.metadata["endpoint_fingerprint"],
        "status": "ok" if found >= wanted else "partial" if matched else "failed",
        "record_count": len(matched), "cache_hit": bool(page_statuses and all(row.get("cache_hit") for row in page_statuses)),
        "pages_fetched": len(page_statuses),
        "api_failures": sum(row.get("status") == "failed" for row in page_statuses),
        "latency_ms": round(sum(float(row.get("latency_ms") or 0) for row in page_statuses), 1),
        "missing_codes": sorted(wanted - found),
    })
    return matched


def normalize_fund_nav(rows: list[dict[str, Any]], cutoff: str | None = None) -> list[dict[str, Any]]:
    """Normalize NAV with adjusted NAV first and accumulated NAV second."""
    output = []
    for row in rows:
        day = str(row.get("nav_date") or row.get("end_date") or row.get("日期") or "")[:10].replace("-", "")
        if not day:
            continue
        if cutoff and day > cutoff.replace("-", ""):
            continue
        adjusted = parse_number(row.get("adj_nav"))
        accumulated = parse_number(row.get("accum_nav"))
        unit = parse_number(row.get("unit_nav"))
        value = adjusted if adjusted is not None else accumulated
        basis = "adj_nav" if adjusted is not None else "accum_nav" if accumulated is not None else None
        output.append(
            {
                "净值日期": f"{day[:4]}-{day[4:6]}-{day[6:8]}",
                "复权单位净值": adjusted,
                "累计净值": accumulated,
                "单位净值": unit,
                "分析净值": value,
                "nav_basis": basis,
                "ann_date": row.get("ann_date"),
            }
        )
    return sorted(output, key=lambda row: row["净值日期"])


def normalize_fund_portfolio(rows: list[dict[str, Any]], cutoff: str) -> list[dict[str, Any]]:
    """Keep the latest fully disclosed portfolio known by the report cutoff."""
    cutoff_key = cutoff.replace("-", "")
    eligible = []
    for row in rows:
        announcement = str(row.get("ann_date") or "").replace("-", "")
        period = str(row.get("end_date") or row.get("period") or "").replace("-", "")
        if announcement and announcement > cutoff_key:
            continue
        if period and period > cutoff_key:
            continue
        eligible.append(row)
    periods = [str(row.get("end_date") or row.get("period") or "").replace("-", "") for row in eligible]
    latest = max((period for period in periods if period), default="")
    selected = [row for row in eligible if str(row.get("end_date") or row.get("period") or "").replace("-", "") == latest]
    return sorted(selected, key=lambda row: parse_number(row.get("stk_mkv_ratio")) or -1, reverse=True)


def market_ts_code(code: str) -> str:
    code = str(code).zfill(6)
    return f"{code}.SH" if code.startswith(("5", "6")) else f"{code}.SZ"


def adjusted_etf_history(
    daily_rows: list[dict[str, Any]],
    factor_rows: list[dict[str, Any]],
    *,
    mode: str = "hfq",
    cutoff: str | None = None,
) -> list[dict[str, Any]]:
    """Build ETF adjusted prices from fund_daily and fund_adj evidence."""
    factors = {
        str(row.get("trade_date") or "").replace("-", ""): parse_number(row.get("adj_factor"))
        for row in factor_rows
        if parse_number(row.get("adj_factor")) is not None
    }
    end_factor = None
    if factors:
        eligible = [day for day in factors if not cutoff or day <= cutoff.replace("-", "")]
        if eligible:
            end_factor = factors[max(eligible)]
    output = []
    for row in daily_rows:
        day = str(row.get("trade_date") or "").replace("-", "")
        close = parse_number(row.get("close"))
        factor = factors.get(day)
        if not day or close is None or factor is None or (cutoff and day > cutoff.replace("-", "")):
            continue
        if mode == "qfq":
            adjusted = close * factor / end_factor if end_factor else None
        elif mode == "none":
            adjusted = close
        else:
            adjusted = close * factor
        if adjusted is not None:
            # fund_daily documents amount in thousand yuan; normalize every
            # provider to yuan before liquidity scoring.
            amount = parse_number(row.get("amount"))
            output.append(
                {
                    "日期": f"{day[:4]}-{day[4:6]}-{day[6:8]}",
                    "收盘": adjusted,
                    "原始收盘": close,
                    "复权因子": factor,
                    "成交额": amount * 1_000 if amount is not None else None,
                    "turnover_unit": "元",
                    "return_basis": "fund_daily+fund_adj",
                }
            )
    return sorted(output, key=lambda row: row["日期"])


def _compound_pct(values: list[float]) -> float | None:
    if not values:
        return None
    result = 1.0
    for value in values:
        result *= 1 + value / 100
    return (result - 1) * 100


def aggregate_sector_flow(
    rows: list[dict[str, Any]],
    *,
    period: int,
    end_date: str,
    sector_type: str,
) -> list[dict[str, Any]]:
    """Aggregate net_amount and index return for the latest N trading days."""
    end_key = end_date.replace("-", "")
    name_key = "industry" if sector_type == "行业资金流" else "name"
    index_key = "close" if sector_type == "行业资金流" else "industry_index"
    grouped: dict[str, list[dict[str, Any]]] = {}
    market_days: set[str] = set()
    for row in rows:
        day = str(row.get("trade_date") or "").replace("-", "")
        name = str(row.get(name_key) or row.get("name") or row.get("industry") or "").strip()
        if not day or day > end_key or not name:
            continue
        market_days.add(day)
        grouped.setdefault(name, []).append(row)
    expected_days = sorted(market_days)[-period:]
    if len(expected_days) < period:
        return []
    output = []
    label = "今日" if period == 1 else f"{period}日"
    for name, group in grouped.items():
        by_day = {str(row.get("trade_date") or "").replace("-", ""): row for row in group}
        ordered_days = sorted(by_day)
        ordered = [by_day[day] for day in ordered_days]
        if not all(day in by_day for day in expected_days):
            continue
        selected = [by_day[day] for day in expected_days]
        net_values = [parse_number(row.get("net_amount")) for row in selected]
        # Tushare moneyflow_ind_ths/moneyflow_cnt_ths documents net_amount in 亿元.
        net_amount_billion = sum(value for value in net_values if value is not None) if any(value is not None for value in net_values) else None
        net_amount = net_amount_billion * 100_000_000 if net_amount_billion is not None else None
        return_value = None
        return_basis = None
        baseline_days = [day for day in ordered_days if day < expected_days[0]]
        if baseline_days:
            baseline = parse_number(by_day[baseline_days[-1]].get(index_key))
            ending = parse_number(selected[-1].get(index_key))
            if baseline not in {None, 0} and ending is not None:
                return_value = (ending / baseline - 1) * 100
                return_basis = f"{index_key}首尾比值"
        if return_value is None:
            pct_values = [parse_number(row.get("pct_change")) for row in selected]
            if all(value is not None for value in pct_values):
                return_value = _compound_pct([float(value) for value in pct_values if value is not None])
                return_basis = "每日pct_change复合" if return_value is not None else "不可确认"
        source_key = str(selected[-1].get("trade_date") or "").replace("-", "")
        source_date = f"{source_key[:4]}-{source_key[4:6]}-{source_key[6:8]}" if len(source_key) == 8 else source_key
        output.append(
            {
                "名称": name,
                f"{label}主力净流入-净额": net_amount,
                f"{label}涨跌幅": return_value,
                "source_date": source_date,
                "provider": PROVIDER,
                "flow_basis": "net_amount（原始单位亿元）按真实交易日累计并换算为元",
                "资金单位": "元",
                "原始资金单位": "亿元",
                "return_basis": return_basis,
            }
        )
    return output


class TushareProxyClient:
    """Cached per-call client for data collection after health-based promotion."""

    def __init__(
        self,
        pro: Any,
        ts_module: Any,
        cache_dir: Path,
        metadata: dict[str, Any],
        *,
        timeout: int = 15,
        retries: int = 2,
        refresh: bool = False,
        context: dict[str, Any] | None = None,
    ) -> None:
        self.pro = pro
        self.ts = ts_module
        self.cache_dir = cache_dir
        self.metadata = metadata
        self.timeout = timeout
        self.retries = max(1, retries)
        self.refresh = refresh
        self.context = context or {}
        self.statuses: list[dict[str, Any]] = []

    def _cache_path(self, name: str, kwargs: dict[str, Any]) -> Path:
        payload = {
            "version": CLIENT_VERSION,
            "name": name,
            "kwargs": kwargs,
            "endpoint_fingerprint": self.metadata.get("endpoint_fingerprint"),
        }
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()[:20]
        return self.cache_dir / f"tushare_{name}_{digest}.json"

    @staticmethod
    def _cache_max_age_days(name: str, kwargs: dict[str, Any]) -> float | None:
        """Return cache TTL; explicit historical windows are immutable."""
        dated_value = kwargs.get("end_date") or kwargs.get("trade_date")
        if dated_value:
            try:
                dated_day = dt.datetime.strptime(str(dated_value)[:10].replace("-", ""), "%Y%m%d").date()
            except ValueError:
                dated_day = None
            if dated_day and dated_day < dt.date.today():
                return None
        if name.startswith("rt_"):
            return 5 / (24 * 60)
        if name in {"fund_basic", "stock_basic", "fund_portfolio"}:
            return 30
        return 1

    def _cached_records(
        self,
        path: Path,
        name: str,
        kwargs: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], bool, float | None]:
        if not path.exists():
            return [], False, None
        try:
            records = load_json(path)
        except (OSError, ValueError, TypeError):
            return [], False, None
        if not isinstance(records, list) or not records:
            return [], False, None
        age_days = max(0.0, (time.time() - path.stat().st_mtime) / 86400)
        max_age = self._cache_max_age_days(name, kwargs)
        return records, max_age is None or age_days <= max_age, age_days

    def call(self, dataset: str, name: str, kwargs: dict[str, Any], *, limit: int | None = None) -> list[dict[str, Any]]:
        path = self._cache_path(name, kwargs)
        cached, fresh, age_days = self._cached_records(path, name, kwargs)
        if cached and fresh and not self.refresh:
            self._status(dataset, name, "ok", cached, cache_hit=True, stale_days=round(age_days or 0, 3))
            return cached
        stale_fallback = [] if name.startswith("rt_") else cached
        records = self._run(dataset, name, getattr(self.pro, name, None), kwargs, path, limit)
        if records:
            return records
        if stale_fallback:
            if self.statuses and self.statuses[-1].get("dataset") == dataset and self.statuses[-1].get("status") == "failed":
                self.statuses.pop()
            self._status(
                dataset, name, "fallback_used", stale_fallback, cache_hit=True,
                reason="live_refresh_failed_stale_cache_used", stale_days=round(age_days or 0, 3),
            )
            return stale_fallback
        return []

    def pro_bar(self, dataset: str, kwargs: dict[str, Any], *, limit: int | None = None) -> list[dict[str, Any]]:
        """All pro_bar calls explicitly pass api=pro as required by the proxy."""
        path = self._cache_path("pro_bar", kwargs)
        cached, fresh, age_days = self._cached_records(path, "pro_bar", kwargs)
        if cached and fresh and not self.refresh:
            self._status(dataset, "pro_bar", "ok", cached, cache_hit=True, stale_days=round(age_days or 0, 3))
            return cached
        stale_fallback = cached
        function = lambda **params: self.ts.pro_bar(api=self.pro, **params)
        records = self._run(dataset, "pro_bar", function, kwargs, path, limit)
        if records:
            return records
        if stale_fallback:
            if self.statuses and self.statuses[-1].get("dataset") == dataset and self.statuses[-1].get("status") == "failed":
                self.statuses.pop()
            self._status(
                dataset, "pro_bar", "fallback_used", stale_fallback, cache_hit=True,
                reason="live_refresh_failed_stale_cache_used", stale_days=round(age_days or 0, 3),
            )
            return stale_fallback
        return []

    def _run(
        self,
        dataset: str,
        name: str,
        function: Callable[..., Any] | None,
        kwargs: dict[str, Any],
        path: Path,
        limit: int | None,
    ) -> list[dict[str, Any]]:
        if function is None:
            self._status(dataset, name, "failed", [], reason="function_not_found")
            return []
        errors = []
        for attempt in range(1, self.retries + 1):
            started = time.monotonic()
            try:
                with wall_clock_timeout(self.timeout) as enforced:
                    records = df_to_records(function(**kwargs), limit=limit)
                if records:
                    write_json(path, records)
                    self._status(
                        dataset, name, "ok", records, cache_hit=False, attempt=attempt,
                        timeout_enforced=enforced, latency_ms=round((time.monotonic() - started) * 1000, 1),
                    )
                    return records
                errors.append("empty")
                break
            except Exception as exc:  # external endpoint behavior
                errors.append(f"{type(exc).__name__}: {exc}")
                if not is_retryable(exc) or attempt >= self.retries:
                    break
                time.sleep(min(1.5, 0.35 * attempt))
        self._status(dataset, name, "failed", [], reason=errors[:3])
        return []

    def _status(self, dataset: str, name: str, status: str, records: list[dict[str, Any]], **extra: Any) -> None:
        self.statuses.append(
            {
                "dataset": dataset,
                "label": dataset,
                "function": name,
                "provider": PROVIDER,
                "transport": TRANSPORT,
                "endpoint_fingerprint": self.metadata["endpoint_fingerprint"],
                "status": status,
                "record_count": len(records),
                **extra,
            }
        )


def load_health(path: Path | None) -> dict[str, Any]:
    if not path or not path.exists():
        return {}
    try:
        payload = load_json(path)
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def promotion_eligible(health: dict[str, Any], dataset: str) -> bool:
    row = (health.get("datasets") or {}).get(dataset) or {}
    return row.get("promotion_eligible") is True
