#!/usr/bin/env python3
"""Shared resilient AkShare access, caching, and value normalization."""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import math
import re
import signal
import time
from pathlib import Path
from typing import Any, Iterator


COLLECTOR_VERSION = "2.6"
RETRYABLE_NAMES = {
    "ConnectionError",
    "ConnectTimeout",
    "HTTPError",
    "ReadTimeout",
    "RemoteDisconnected",
    "Timeout",
    "TimeoutError",
}
REALTIME_FUNCTIONS = {
    "eastmoney_etf_spot_candidates",
    "fund_etf_spot_em",
    "fund_etf_spot_ths",
    "fund_etf_category_sina",
    "stock_board_industry_name_em",
    "stock_board_concept_name_em",
    "stock_board_industry_summary_ths",
    "stock_sector_fund_flow_rank",
}


class CallTimeout(TimeoutError):
    """Raised when an AkShare call exceeds the configured wall-clock timeout."""


def parse_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        return None if math.isnan(number) else number
    text = str(value).strip().replace(",", "").replace("%", "")
    if not text or text in {"-", "--", "nan", "None", "<NA>"}:
        return None
    multiplier = 1.0
    if "亿" in text:
        multiplier = 100_000_000.0
    elif "万" in text:
        multiplier = 10_000.0
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    return float(match.group(0)) * multiplier if match else None


def clean_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def df_to_records(df: Any, limit: int | None = None) -> list[dict[str, Any]]:
    if df is None or getattr(df, "empty", False):
        return []
    if limit is not None:
        df = df.head(limit)
    return [{str(key): clean_value(value) for key, value in row.items()} for row in df.to_dict(orient="records")]


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def normalize_holdings(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict) and "holdings" in raw:
        raw = raw["holdings"]
    if not isinstance(raw, list):
        raise ValueError("holdings JSON must be a list or an object with a holdings list")
    holdings = []
    for source in raw:
        item = dict(source)
        item["code"] = str(item["code"]).zfill(6)
        item["amount"] = parse_number(item.get("amount")) or 0.0
        if item.get("current_weight") is not None:
            weight = parse_number(item.get("current_weight")) or 0.0
            item["current_weight"] = weight / 100 if weight > 1 else weight
        item["tags"] = list(item.get("tags") or [])
        holdings.append(item)
    return holdings


def holdings_metadata(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        meta = dict(raw.get("portfolio_meta") or {})
        if meta:
            return meta
    return {
        "weight_mode": "legacy_auto",
        "weight_note": "旧格式：优先使用用户权重或金额；均未提供时使用等权。",
        "amounts_are_assumptions": False,
    }


def holdings_hash(holdings: list[dict[str, Any]]) -> str:
    normalized = json.dumps(holdings, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


@contextlib.contextmanager
def wall_clock_timeout(seconds: int) -> Iterator[bool]:
    """Enforce a real timeout on Unix main threads; yield False if unsupported."""
    if seconds <= 0 or not hasattr(signal, "setitimer"):
        yield False
        return

    def handler(_signum: int, _frame: Any) -> None:
        raise CallTimeout(f"call exceeded {seconds}s")

    previous = signal.getsignal(signal.SIGALRM)
    try:
        signal.signal(signal.SIGALRM, handler)
        signal.setitimer(signal.ITIMER_REAL, seconds)
        yield True
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def is_retryable(exc: Exception) -> bool:
    return type(exc).__name__ in RETRYABLE_NAMES or isinstance(exc, (ConnectionError, TimeoutError))


class AkshareClient:
    def __init__(
        self,
        ak: Any,
        cache_dir: Path,
        *,
        timeout: int = 20,
        retries: int = 2,
        refresh: bool = False,
        context: dict[str, Any] | None = None,
    ) -> None:
        self.ak = ak
        self.cache_dir = cache_dir
        self.timeout = timeout
        self.retries = max(1, retries)
        self.refresh = refresh
        self.context = context or {}
        self.statuses: list[dict[str, Any]] = []
        # Visible warnings are derived after all fallbacks have completed.  A
        # failed source is audit evidence, not necessarily a missing dataset.
        self.warnings: list[str] = []

    @staticmethod
    def _stable_cache_value(value: Any, *, keep_collection_date: bool) -> Any:
        if isinstance(value, dict):
            ignored = {"mode", "stage", "calendar_source", "as_of"}
            if not keep_collection_date:
                ignored.add("collection_trade_date")
            return {
                key: AkshareClient._stable_cache_value(item, keep_collection_date=keep_collection_date)
                for key, item in value.items()
                if key not in ignored
            }
        if isinstance(value, list):
            return [AkshareClient._stable_cache_value(item, keep_collection_date=keep_collection_date) for item in value]
        return value

    def _cache_path(
        self,
        function_name: str,
        variants: list[dict[str, Any]],
        key_extra: Any,
        limit: int | None = None,
    ) -> Path:
        keep_collection_date = function_name in REALTIME_FUNCTIONS
        payload = {
            "version": COLLECTOR_VERSION,
            "function": function_name,
            "variants": variants,
            "context": self._stable_cache_value(self.context, keep_collection_date=keep_collection_date),
            "extra": self._stable_cache_value(key_extra, keep_collection_date=keep_collection_date),
            "limit": limit,
        }
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()[:20]
        return self.cache_dir / f"{function_name}_{digest}.json"

    def call(
        self,
        label: str,
        function_name: str,
        variants: list[dict[str, Any]],
        *,
        limit: int | None = None,
        key_extra: Any = None,
    ) -> list[dict[str, Any]]:
        path = self._cache_path(function_name, variants, key_extra, limit)
        if path.exists() and not self.refresh:
            records = load_json(path)
            self.statuses.append(
                {"label": label, "function": function_name, "provider": "AkShare", "transport": "https", "status": "ok", "cache_hit": True, "record_count": len(records)}
            )
            return records

        function = getattr(self.ak, function_name, None)
        if function is None:
            self.statuses.append({"label": label, "function": function_name, "provider": "AkShare", "transport": "local_sdk", "status": "failed", "reason": "function_not_found"})
            return []

        errors: list[str] = []
        for variant_index, kwargs in enumerate(variants):
            for attempt in range(1, self.retries + 1):
                try:
                    with wall_clock_timeout(self.timeout) as enforced:
                        records = df_to_records(function(**kwargs), limit=limit)
                    if records:
                        write_json(path, records)
                        self.statuses.append(
                            {
                                "label": label,
                                "function": function_name,
                                "provider": "AkShare",
                                "transport": "https",
                                "status": "ok" if variant_index == 0 else "fallback_used",
                                "cache_hit": False,
                                "timeout_enforced": enforced,
                                "attempt": attempt,
                                "kwargs": kwargs,
                                "record_count": len(records),
                                "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
                            }
                        )
                        return records
                    errors.append(f"{kwargs}: empty")
                    break
                except (Exception, SystemExit) as exc:  # endpoint behavior is external
                    errors.append(f"{kwargs}: {type(exc).__name__}: {exc}")
                    if not is_retryable(exc):
                        break
                    if attempt < self.retries:
                        time.sleep(min(1.5, 0.35 * attempt))
        reason = errors[:4]
        self.statuses.append({"label": label, "function": function_name, "provider": "AkShare", "transport": "https", "status": "failed", "cache_hit": False, "reason": reason})
        return []

    def call_custom(
        self,
        label: str,
        function_name: str,
        function: Any,
        *,
        key_extra: Any = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Run and cache a local compatibility collector with normal auditing."""
        path = self._cache_path(function_name, [{}], key_extra, limit)
        if path.exists() and not self.refresh:
            records = load_json(path)
            self.statuses.append({"label": label, "function": function_name, "provider": "公开兼容数据源", "transport": "https", "status": "ok", "cache_hit": True, "record_count": len(records)})
            return records
        errors = []
        for attempt in range(1, self.retries + 1):
            try:
                with wall_clock_timeout(self.timeout) as enforced:
                    value = function()
                    records = value if isinstance(value, list) else df_to_records(value, limit=limit)
                records = records[:limit] if limit is not None else records
                if records:
                    write_json(path, records)
                    self.statuses.append({
                        "label": label, "function": function_name, "provider": "公开兼容数据源", "transport": "https", "status": "fallback_used",
                        "cache_hit": False, "timeout_enforced": enforced, "attempt": attempt,
                        "record_count": len(records), "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
                    })
                    return records
                errors.append("empty")
                break
            except (Exception, SystemExit) as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
                if not is_retryable(exc) or attempt >= self.retries:
                    break
                time.sleep(min(1.5, 0.35 * attempt))
        self.statuses.append({"label": label, "function": function_name, "provider": "公开兼容数据源", "transport": "https", "status": "failed", "cache_hit": False, "reason": errors[:4]})
        return []


def dataset_status(
    dataset: str,
    statuses: list[dict[str, Any]],
    *,
    basis: str | None = None,
    source_date: str | None = None,
    requirement: str = "required",
    impact: str = "report",
    empty_status: str = "failed",
    empty_reason: str | None = None,
) -> dict[str, Any]:
    """Collapse source attempts into one user-facing logical dataset status."""
    successful = [row for row in statuses if row.get("status") in {"ok", "fallback_used"} and (row.get("record_count") or 0) > 0]
    resolved = successful[-1] if successful else None
    status = ("fallback_used" if resolved and (len(statuses) > 1 or resolved.get("status") == "fallback_used") else "ok") if resolved else empty_status
    return {
        "dataset": dataset,
        "requirement": requirement,
        "impact": impact,
        "attempted_sources": [row.get("function") for row in statuses],
        "resolved_by": resolved.get("function") if resolved else None,
        "status": status,
        "basis": basis,
        "source_date": resolved.get("source_date") if resolved and resolved.get("source_date") else source_date,
        "stale_days": resolved.get("stale_days", 0) if resolved else 0,
        "record_count": resolved.get("record_count", 0) if resolved else 0,
        "reason": None if resolved else ([row.get("reason") for row in statuses if row.get("reason")] or empty_reason),
        "provider": (resolved or (statuses[-1] if statuses else {})).get("provider"),
        "transport": (resolved or (statuses[-1] if statuses else {})).get("transport"),
        "endpoint_fingerprint": resolved.get("endpoint_fingerprint") if resolved else None,
        "crosscheck_status": resolved.get("crosscheck_status") if resolved else None,
        "promotion_eligible": bool(resolved),
        "cache_hit": bool(resolved and resolved.get("cache_hit")),
    }


def unresolved_warnings(statuses: list[dict[str, Any]]) -> list[str]:
    output = []
    for row in statuses:
        if row.get("requirement", "required") != "required":
            continue
        if row.get("status") == "failed":
            output.append(f"{row['dataset']}：所有数据源均失败或为空")
        elif row.get("status") == "partial":
            output.append(f"{row['dataset']}：仅取得部分必要字段")
    return output
