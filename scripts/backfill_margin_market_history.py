#!/usr/bin/env python3
"""Backfill exact SSE/SZSE A-share market-cap and turnover denominators."""

from __future__ import annotations

import argparse
import datetime as dt
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from cache_store import CacheStore
from margin_leverage import normalize_exchange_market_snapshot, normalize_sse_market_api_rows


PROVIDER = "交易所公开历史汇总"
SSE_URL = "https://query.sse.com.cn/commonQuery.do"
SZSE_URL = "https://www.szse.cn/api/report/ShowReport"


def _fetch_sse(day: str, timeout: float) -> list[dict[str, Any]]:
    response = requests.get(
        SSE_URL,
        params={
            "sqlId": "COMMON_SSE_SJ_GPSJ_CJGK_MRGK_C",
            "PRODUCT_CODE": "01,02,03,11,17",
            "type": "inParams",
            "SEARCH_DATE": day,
        },
        headers={"Referer": "https://www.sse.com.cn/", "User-Agent": "Mozilla/5.0"},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    return normalize_sse_market_api_rows(payload.get("result") or [], day, PROVIDER)


def _fetch_szse(day: str, timeout: float) -> list[dict[str, Any]]:
    response = requests.get(
        SZSE_URL,
        params={
            "SHOWTYPE": "xlsx",
            "CATALOGID": "1803_sczm",
            "TABKEY": "tab1",
            "txtQueryDate": day,
            "random": "0.39339437497296137",
        },
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=timeout,
    )
    response.raise_for_status()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        frame = pd.read_excel(BytesIO(response.content), engine="openpyxl")
    if len(frame.columns) < 5:
        return []
    frame = frame.iloc[:, :5]
    frame.columns = ["证券类别", "数量", "成交金额", "总市值", "流通市值"]
    records = frame.to_dict("records")
    return normalize_exchange_market_snapshot(records, "SZSE", day, PROVIDER)


def _fetch(exchange: str, day: str, timeout: float, retries: int) -> tuple[str, str, list[dict[str, Any]], str | None]:
    function = _fetch_sse if exchange == "SSE" else _fetch_szse
    last_error = None
    for attempt in range(retries + 1):
        try:
            rows = function(day, timeout)
            if rows:
                return exchange, day, rows, None
            last_error = "empty_data"
        except Exception as exc:  # network/parser failures are retained in the audit summary
            last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
        if attempt < retries:
            time.sleep(0.25 * (attempt + 1))
    return exchange, day, [], last_error


def _target_dates(store: CacheStore, end_date: str, sessions: int) -> list[str]:
    margin_dates = []
    for exchange in ("SSE", "SZSE"):
        rows = store.get_series("margin_summary", exchange, end_date=end_date)
        margin_dates.append({str(row.get("trade_date")) for row in rows if row.get("trade_date")})
    common = sorted(set.intersection(*margin_dates)) if all(margin_dates) else []
    return common[-sessions:]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-root", type=Path, default=Path("work/cache/fund-rotation"))
    parser.add_argument("--end-date", default=dt.date.today().isoformat())
    parser.add_argument("--sessions", type=int, default=650)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--timeout", type=float, default=12)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    if args.sessions < 501:
        parser.error("--sessions must be at least 501: the current day is excluded from the 500-day percentile baseline")
    if not 1 <= args.workers <= 12:
        parser.error("--workers must be between 1 and 12")

    with CacheStore(args.cache_root) as store:
        dates = _target_dates(store, args.end_date, args.sessions)
        if len(dates) < 500:
            parser.error(f"margin cache exposes only {len(dates)} common trading dates")
        jobs = []
        for exchange in ("SSE", "SZSE"):
            existing = {
                str(row.get("trade_date"))
                for row in store.get_series("market_daily_info", exchange, end_date=args.end_date)
            }
            jobs.extend((exchange, day) for day in dates if args.refresh or day not in existing)

        successes: dict[str, list[dict[str, Any]]] = {"SSE": [], "SZSE": []}
        written_counts = {"SSE": 0, "SZSE": 0}
        failures: list[dict[str, str]] = []
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(_fetch, exchange, day, args.timeout, args.retries): (exchange, day)
                for exchange, day in jobs
            }
            for index, future in enumerate(as_completed(futures), start=1):
                exchange, day, rows, error = future.result()
                if rows:
                    successes[exchange].extend(rows)
                    if len(successes[exchange]) >= 25:
                        written_counts[exchange] += store.upsert_series(
                            PROVIDER, "market_daily_info", exchange, successes[exchange]
                        )
                        successes[exchange].clear()
                else:
                    failures.append({"exchange": exchange, "trade_date": day, "reason": error or "unknown"})
                if index % 100 == 0:
                    print(f"processed {index}/{len(futures)}")

        for exchange, rows in successes.items():
            written_counts[exchange] += store.upsert_series(PROVIDER, "market_daily_info", exchange, rows)

        final_counts = {
            exchange: len(store.get_series("market_daily_info", exchange, end_date=args.end_date))
            for exchange in ("SSE", "SZSE")
        }
        common_market_dates = set.intersection(*(
            {
                str(row.get("trade_date"))
                for row in store.get_series("market_daily_info", exchange, end_date=args.end_date)
            }
            for exchange in ("SSE", "SZSE")
        ))
        print({
            "requested_jobs": len(jobs),
            "written": written_counts,
            "failed": len(failures),
            "final_counts": final_counts,
            "common_observations": len(common_market_dates),
            "first_common_date": min(common_market_dates) if common_market_dates else None,
            "last_common_date": max(common_market_dates) if common_market_dates else None,
            "sample_failures": failures[:10],
        })


if __name__ == "__main__":
    main()
