#!/usr/bin/env python3
"""Walk-forward calibration for the informational A-share leverage model."""

from __future__ import annotations

import argparse
import math
from collections import defaultdict
from pathlib import Path
from statistics import median
from typing import Any

from cache_store import CacheStore, stable_hash
from data_access import parse_number, write_json
from margin_leverage import (
    COMPARABLE_START,
    MIN_PERCENTILE_SAMPLE,
    MODEL_VERSION,
    TRADING_DAYS_5Y,
    combine_exchanges,
    percentile_rank,
)


def _index_series(rows: list[dict[str, Any]], cutoff: str | None = None) -> dict[str, float]:
    output = {}
    for row in rows:
        day = str(row.get("日期") or row.get("date") or row.get("trade_date") or "")[:10]
        value = parse_number(row.get("收盘") or row.get("close"))
        if day and value is not None and (not cutoff or day <= cutoff):
            output[day] = value
    return output


def _band(value: float | None) -> str:
    if value is None:
        return "数据不足"
    if value < 20:
        return "低"
    if value < 70:
        return "正常"
    if value < 85:
        return "升温"
    if value < 95:
        return "偏热"
    return "过热"


def _pressure_band(value: float | None) -> str:
    if value is None:
        return "数据不足"
    if value < 30:
        return "平稳"
    if value < 60:
        return "观察"
    if value < 80:
        return "降温"
    if value < 90:
        return "去杠杆"
    return "压力升高"


def _change(rows: list[dict[str, Any]], field: str, index: int, sessions: int) -> float | None:
    if index < sessions:
        return None
    current = parse_number(rows[index].get(field))
    previous = parse_number(rows[index - sessions].get(field))
    return (current / previous - 1) * 100 if current is not None and previous not in {None, 0} else None


def _stress(value: float | None, bad_at: float) -> float | None:
    if value is None:
        return None
    return 0.0 if value >= 0 else min(100.0, abs(value / bad_at) * 100)


def _weighted(values: list[tuple[float | None, float]], minimum: float = 0.75) -> tuple[float | None, float]:
    available = [(float(value), weight) for value, weight in values if value is not None]
    coverage = sum(weight for _value, weight in available)
    if coverage < minimum:
        return None, coverage
    return sum(value * weight for value, weight in available) / coverage, coverage


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def calibrate(
    rows: list[dict[str, Any]],
    index_rows: list[dict[str, Any]] | dict[str, list[dict[str, Any]]],
    start_date: str,
) -> dict[str, Any]:
    source_indexes = index_rows if isinstance(index_rows, dict) else {"中证全指": index_rows}
    indexes = {name: _index_series(records) for name, records in source_indexes.items()}
    index = indexes.get("中证全指") or next(iter(indexes.values()), {})
    dated = [row for row in rows if row.get("trade_date") >= start_date and row.get("trade_date") in index]
    observations = []
    for i in range(MIN_PERCENTILE_SAMPLE, len(dated) - 60):
        history = dated[max(0, i - TRADING_DAYS_5Y):i]
        current = dated[i]
        density = parse_number(current.get("financing_to_float_cap"))
        intensity = parse_number(current.get("financing_buy_to_turnover"))
        if density is None or intensity is None:
            continue
        density_pct = percentile_rank(
            [value for value in (parse_number(row.get("financing_to_float_cap")) for row in history) if value is not None],
            density,
        )
        intensity_pct = percentile_rank(
            [value for value in (parse_number(row.get("financing_buy_to_turnover")) for row in history) if value is not None],
            intensity,
        )
        growth20 = _change(dated, "financing_balance", i, 20)
        historical_growth = [
            value for j in range(20, len(history))
            if (value := _change(history, "financing_balance", j, 20)) is not None
        ]
        growth_pct = percentile_rank(historical_growth, growth20) if growth20 is not None else None
        if density_pct is None or intensity_pct is None or growth_pct is None:
            continue
        heat = (density_pct * 0.40 + intensity_pct * 0.25 + growth_pct * 0.20) / 0.85
        day = current["trade_date"]
        current_index = index[day]
        balance_stress = _stress(min(_change(dated, "financing_balance", i, 5) or 0, growth20 or 0), -8)
        index_changes20 = []
        if i >= 20:
            prior_day = dated[i - 20]["trade_date"]
            for series in indexes.values():
                current_value = series.get(day)
                prior_value = series.get(prior_day)
                if current_value is not None and prior_value not in {None, 0}:
                    index_changes20.append((current_value / prior_value - 1) * 100)
        index_change20 = median(index_changes20) if index_changes20 else None
        index_stress = _stress(index_change20, -10)
        turnover_recent = [parse_number(row.get("market_turnover")) for row in dated[i - 4:i + 1]]
        turnover_prior = [parse_number(row.get("market_turnover")) for row in dated[i - 24:i - 4]]
        turnover_recent = [value for value in turnover_recent if value is not None]
        turnover_prior = [value for value in turnover_prior if value is not None]
        turnover_change = None
        if len(turnover_recent) == 5 and len(turnover_prior) == 20:
            base = sum(turnover_prior) / 20
            turnover_change = ((sum(turnover_recent) / 5) / base - 1) * 100 if base else None
        intensity_recent = [parse_number(row.get("financing_buy_to_turnover")) for row in dated[i - 4:i + 1]]
        intensity_prior = [parse_number(row.get("financing_buy_to_turnover")) for row in dated[i - 24:i - 4]]
        intensity_recent = [value for value in intensity_recent if value is not None]
        intensity_prior = [value for value in intensity_prior if value is not None]
        intensity_change = None
        if len(intensity_recent) == 5 and len(intensity_prior) == 20:
            base = sum(intensity_prior) / 20
            intensity_change = ((sum(intensity_recent) / 5) / base - 1) * 100 if base else None
        pressure, pressure_coverage = _weighted([
            (balance_stress, 0.40),
            (index_stress, 0.30),
            (_stress(turnover_change, -40), 0.20),
            (_stress(intensity_change, -40), 0.10),
        ])
        future_days = [row["trade_date"] for row in dated[i + 1:i + 61] if row["trade_date"] in index]
        drawdowns = [(index[future_day] / current_index - 1) * 100 for future_day in future_days]
        dd20 = min(drawdowns[:20]) if len(drawdowns) >= 20 else None
        dd60 = min(drawdowns) if len(drawdowns) >= 60 else None
        observations.append({
            "trade_date": day,
            "heat_score": heat,
            "heat_band": _band(heat),
            "pressure_score": pressure,
            "pressure_band": _pressure_band(pressure),
            "pressure_coverage": pressure_coverage,
            "forward_20d_max_drawdown": dd20,
            "forward_60d_max_drawdown": dd60,
        })
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in observations:
        groups[row["heat_band"]].append(row)
    summary = []
    for label in ["低", "正常", "升温", "偏热", "过热"]:
        group = groups.get(label, [])
        dd20 = [row["forward_20d_max_drawdown"] for row in group if row["forward_20d_max_drawdown"] is not None]
        dd60 = [row["forward_60d_max_drawdown"] for row in group if row["forward_60d_max_drawdown"] is not None]
        sufficient = len(group) >= 30
        summary.append({
            "band": label,
            "sample_count": len(group),
            "status": "ok" if sufficient else "insufficient_sample",
            "median_20d_max_drawdown": median(dd20) if sufficient and dd20 else None,
            "p10_20d_max_drawdown": _quantile(dd20, 0.10) if sufficient else None,
            "event_20d_le_5pct_rate": sum(value <= -5 for value in dd20) / len(dd20) if sufficient and dd20 else None,
            "median_60d_max_drawdown": median(dd60) if sufficient and dd60 else None,
            "p10_60d_max_drawdown": _quantile(dd60, 0.10) if sufficient else None,
            "event_60d_le_10pct_rate": sum(value <= -10 for value in dd60) / len(dd60) if sufficient and dd60 else None,
        })
    pressure_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in observations:
        pressure_groups[row["pressure_band"]].append(row)
    pressure_summary = []
    for label in ["平稳", "观察", "降温", "去杠杆", "压力升高"]:
        group = pressure_groups.get(label, [])
        dd20 = [row["forward_20d_max_drawdown"] for row in group if row["forward_20d_max_drawdown"] is not None]
        dd60 = [row["forward_60d_max_drawdown"] for row in group if row["forward_60d_max_drawdown"] is not None]
        sufficient = len(group) >= 30
        pressure_summary.append({
            "band": label,
            "sample_count": len(group),
            "status": "ok" if sufficient else "insufficient_sample",
            "median_20d_max_drawdown": median(dd20) if sufficient and dd20 else None,
            "event_20d_le_5pct_rate": sum(value <= -5 for value in dd20) / len(dd20) if sufficient and dd20 else None,
            "median_60d_max_drawdown": median(dd60) if sufficient and dd60 else None,
            "event_60d_le_10pct_rate": sum(value <= -10 for value in dd60) / len(dd60) if sufficient and dd60 else None,
        })
    evidence_hash = stable_hash({"rows": rows, "index": index_rows, "start_date": start_date})
    notes = [
        "每个历史日的分位仅使用该日前最多5年数据。",
        "校准只验证固定分位区间，不据此优化阈值。",
        "两融模块保持display_only，不修改基金动作。",
    ]
    if not observations:
        notes.append(f"同日两融、流通市值、成交额和宽基指数的联合样本仅{len(dated)}条；至少需要约{MIN_PERCENTILE_SAMPLE + 60}条才能完成首次走步校准。")
    return {
        "model_version": MODEL_VERSION,
        "calibration_method": "walk_forward_trailing_5y",
        "start_date": start_date,
        "end_date": observations[-1]["trade_date"] if observations else None,
        "observation_count": len(observations),
        "aligned_input_count": len(dated),
        "minimum_history_days": MIN_PERCENTILE_SAMPLE,
        "reserved_future_days": 60,
        "future_targets": {"20d": "最大回撤<=-5%", "60d": "最大回撤<=-10%"},
        "heat_bands": summary,
        "pressure_bands": pressure_summary,
        "evidence_hash": evidence_hash,
        "status": "ok" if observations else "insufficient_data",
        "notes": notes,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-root", type=Path, default=Path("work/cache/fund-rotation"))
    parser.add_argument("--start-date", default=COMPARABLE_START)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    with CacheStore(args.cache_root) as store:
        margin = combine_exchanges(
            {exchange: store.get_series("margin_summary", exchange) for exchange in ("SSE", "SZSE")},
            ("financing_balance", "financing_buy", "financing_repay", "lending_balance", "margin_balance"),
        )
        market = combine_exchanges(
            {exchange: store.get_series("market_daily_info", exchange) for exchange in ("SSE", "SZSE")},
            ("float_market_cap", "market_turnover"),
        )
        market_by_date = {row["trade_date"]: row for row in market}
        rows = []
        for row in margin:
            daily = market_by_date.get(row["trade_date"])
            financing = parse_number(row.get("financing_balance"))
            financing_buy = parse_number(row.get("financing_buy"))
            if not daily:
                continue
            float_mv = parse_number(daily.get("float_market_cap"))
            turnover = parse_number(daily.get("market_turnover"))
            rows.append({
                **row,
                "financing_to_float_cap": financing / float_mv * 100 if financing is not None and float_mv else None,
                "financing_buy_to_turnover": financing_buy / turnover * 100 if financing_buy is not None and turnover else None,
            })
        index_rows = {
            name: store.get_series("style_index", name)
            for name in ("中证全指", "沪深300", "中证1000")
        }
    write_json(args.output, calibrate(rows, index_rows, args.start_date))


if __name__ == "__main__":
    main()
