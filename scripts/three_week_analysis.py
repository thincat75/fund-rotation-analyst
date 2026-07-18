#!/usr/bin/env python3
"""Deterministic three-week portfolio, style, and sector rotation analysis."""

from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

from cache_store import CacheStore, stable_hash
from data_access import parse_number


GENERIC_FUND_THEMES = {"主动权益", "混合型", "偏股混合", "质量成长", "LOF", "指数型", "ETF联接"}

# These rules describe disclosed portfolio exposure, not guaranteed real-time holdings.
THEME_SECTOR_RULES: dict[str, dict[str, tuple[str, ...]]] = {
    "AI光模块/通信": {
        "direct": ("通信设备", "通信服务", "光学光电子", "共封装光学", "CPO", "光纤", "铜缆高速连接", "F5G", "5G", "6G"),
        "indirect": ("人工智能", "AIGC", "ChatGPT", "AI应用", "AI智能体", "云计算", "算力", "英伟达", "华为昇腾"),
    },
    "PCB/AI服务器": {
        "direct": ("元件", "PCB", "液冷服务器", "算力租赁", "东数西算"),
        "indirect": ("计算机设备", "其他电子", "消费电子", "工业互联网", "数据中心", "英伟达"),
    },
    "半导体设备/材料": {
        "direct": ("半导体", "芯片", "集成电路", "光刻", "先进封装", "存储芯片", "MCU", "第三代半导体", "电子化学品"),
        "indirect": ("专用设备", "国家大基金", "中芯国际"),
    },
    "创新药/医药": {
        "direct": ("创新药", "化学制药", "生物制品", "医疗服务", "医疗器械", "CRO"),
        "indirect": ("医药商业", "智能医疗", "民营医院"),
    },
    "红利价值": {
        "direct": ("高股息", "银行", "保险", "煤炭", "电力"),
        "indirect": ("中特估", "公路铁路", "港口航运"),
    },
}


def parse_day(value: Any) -> dt.date | None:
    if value is None:
        return None
    text = str(value)[:10]
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def row_date(row: dict[str, Any]) -> dt.date | None:
    return parse_day(next((row.get(key) for key in ("trade_date", "日期", "净值日期", "date") if row.get(key) is not None), None))


def row_value(row: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        if row.get(key) is not None:
            value = parse_number(row.get(key))
            if value is not None:
                return value
    return None


def series(records: list[dict[str, Any]], keys: tuple[str, ...]) -> list[tuple[dt.date, float]]:
    values: dict[dt.date, float] = {}
    for row in records:
        day = row_date(row)
        value = row_value(row, keys)
        if day and value is not None and value > 0:
            values[day] = value
    return sorted(values.items())


def latest_on_or_before(values: list[tuple[dt.date, float]], target: dt.date) -> tuple[dt.date, float] | None:
    eligible = [item for item in values if item[0] <= target]
    return eligible[-1] if eligible else None


def period_return(
    values: list[tuple[dt.date, float]],
    baseline: dt.date,
    end: dt.date,
    start: dt.date,
    *,
    max_baseline_lag_days: int = 7,
) -> tuple[float | None, str | None]:
    left = latest_on_or_before(values, baseline)
    right = latest_on_or_before(values, end)
    if (
        not left
        or not right
        or (baseline - left[0]).days > max_baseline_lag_days
        or right[0] < start
        or right[0] <= left[0]
        or left[1] <= 0
    ):
        return None, right[0].isoformat() if right else None
    return (right[1] / left[1] - 1) * 100, right[0].isoformat()


def build_periods(trade_dates: list[dt.date], end_date: dt.date, history_weeks: int = 3, current_complete: bool = False) -> list[dict[str, Any]]:
    dates = sorted({day for day in trade_dates if day <= end_date})
    if not dates:
        return []
    weeks: list[list[dt.date]] = []
    for _, group in __import__("itertools").groupby(dates, key=lambda day: day.isocalendar()[:2]):
        values = list(group)
        if values:
            weeks.append(values)
    selected = weeks[-history_weeks:]
    output = []
    for index, week_dates in enumerate(reversed(selected)):
        start = week_dates[0]
        prior = [day for day in dates if day < start]
        if not prior:
            continue
        period_id = "W0" if index == 0 else f"W-{index}"
        complete = current_complete if index == 0 else True
        output.append({
            "period_id": period_id,
            "label": "本周" if index == 0 else "上周" if index == 1 else "上上周",
            "baseline_date": prior[-1].isoformat(),
            "start_date": start.isoformat(),
            "end_date": week_dates[-1].isoformat(),
            "trade_dates": [day.isoformat() for day in week_dates],
            "trade_days": len(week_dates),
            "completeness": "complete" if complete else "partial",
            "eligible_for_action": bool(complete),
        })
    return list(reversed(output))


def percentile_map(values: dict[str, float | None]) -> dict[str, float | None]:
    valid = sorted((value, key) for key, value in values.items() if value is not None)
    if not valid:
        return {key: None for key in values}
    count = len(valid)
    output: dict[str, float | None] = {key: None for key in values}
    for rank, (_, key) in enumerate(valid, start=1):
        output[key] = 100.0 if count == 1 else (rank - 1) / (count - 1) * 100
    return output


def analyze_portfolio(raw: dict[str, Any], portfolio: dict[str, Any], periods: list[dict[str, Any]]) -> dict[str, Any]:
    weight_by_code = {row.get("code"): float(row.get("current_weight") or 0) for row in portfolio.get("funds") or []}
    fund_rows = []
    portfolio_returns: dict[str, float | None] = {}
    coverage: dict[str, float] = {}
    for code, content in (raw.get("funds") or {}).items():
        values = series(content.get("nav") or [], ("累计净值", "复权单位净值", "单位净值", "close"))
        period_values = {}
        for period in periods:
            value, latest = period_return(
                values,
                parse_day(period["baseline_date"]),
                parse_day(period["end_date"]),
                parse_day(period["start_date"]),
            )
            period_values[period["period_id"]] = {"return": value, "latest_date": latest}
        fund_rows.append({"code": code, "name": next((row.get("name") for row in portfolio.get("funds") or [] if row.get("code") == code), code), "weight": weight_by_code.get(code, 0), "periods": period_values})
    for period in periods:
        pid = period["period_id"]
        available = [row for row in fund_rows if row["periods"][pid]["return"] is not None]
        coverage[pid] = sum(row["weight"] for row in available)
        portfolio_returns[pid] = sum(row["weight"] * row["periods"][pid]["return"] for row in available) if coverage[pid] >= 0.90 else None
        for row in fund_rows:
            value = row["periods"][pid]["return"]
            row["periods"][pid]["contribution"] = row["weight"] * value if value is not None else None
    compounded = None
    completed_compounded = None
    chronological = list(reversed(periods))
    if all(portfolio_returns.get(period["period_id"]) is not None for period in chronological):
        compounded = (math.prod(1 + portfolio_returns[period["period_id"]] / 100 for period in chronological) - 1) * 100
    completed_periods = [period for period in chronological if period.get("completeness") == "complete"]
    if completed_periods and all(portfolio_returns.get(period["period_id"]) is not None for period in completed_periods):
        completed_compounded = (math.prod(1 + portfolio_returns[period["period_id"]] / 100 for period in completed_periods) - 1) * 100
    for row in fund_rows:
        trajectory = [row["periods"][period["period_id"]]["return"] for period in periods]
        valid = [value for value in trajectory if value is not None]
        if len(valid) == len(periods) and all(left < right for left, right in zip(valid, valid[1:])):
            state = "连续改善但仍为负" if valid[-1] < 0 else "连续改善"
        elif len(valid) == len(periods) and all(left > right for left, right in zip(valid, valid[1:])):
            state = "连续走弱但仍为正" if valid[-1] > 0 else "连续走弱"
        elif len(valid) >= 2 and valid[-2] < 0 < valid[-1]:
            state = "由弱转强"
        elif len(valid) >= 2 and valid[-2] > 0 > valid[-1]:
            state = "由强转弱"
        else:
            state = "震荡/证据不足"
        row["trajectory_state"] = state
    return {
        "weekly_returns": portfolio_returns,
        "coverage": coverage,
        "three_week_compound_return": compounded,
        "completed_weeks_compound_return": completed_compounded,
        "compound_basis": "包含进行中周，仅作截至当前表现" if periods and periods[-1].get("completeness") == "partial" else "三个完整交易周",
        "action_return_basis": "仅使用完整交易周",
        "funds": fund_rows,
    }


def analyze_styles(raw: dict[str, Any], periods: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    output = []
    for name, records in ((raw.get("market") or {}).get("style_indexes") or {}).items():
        values = series(records, ("收盘", "close"))
        weekly = {}
        for period in periods:
            value, latest = period_return(values, parse_day(period["baseline_date"]), parse_day(period["end_date"]), parse_day(period["start_date"]))
            weekly[period["period_id"]] = {"return": value, "latest_date": latest}
        output.append({"name": name, "periods": weekly})
    for period in periods:
        pid = period["period_id"]
        ranked = sorted((row["periods"][pid]["return"], row) for row in output if row["periods"][pid]["return"] is not None)
        for rank, (_, row) in enumerate(reversed(ranked), start=1):
            row["periods"][pid]["rank"] = rank
    def classify(pid: str) -> dict[str, Any]:
        current = {row["name"]: row["periods"].get(pid, {}).get("return") for row in output}
        valid_count = sum(value is not None for value in current.values())
        growth = [current.get(name) for name in ("国证成长", "创业板指", "科创50") if current.get(name) is not None]
        value = [current.get(name) for name in ("国证价值", "中证红利") if current.get(name) is not None]
        large = [current.get(name) for name in ("沪深300", "上证50") if current.get(name) is not None]
        small = [current.get(name) for name in ("中证500", "中证1000") if current.get(name) is not None]
        average = lambda items: sum(items) / len(items) if items else None
        growth_avg, value_avg, large_avg, small_avg = average(growth), average(value), average(large), average(small)
        if valid_count < 7:
            regime = "数据不足"
        elif growth_avg is not None and value_avg is not None and growth_avg - value_avg >= 2:
            regime = "成长扩散"
        elif growth_avg is not None and value_avg is not None and value_avg - growth_avg >= 2:
            regime = "价值防御"
        elif large_avg is not None and small_avg is not None and abs(large_avg - small_avg) >= 2:
            regime = "大小盘分化"
        elif sum(value < 0 for value in current.values() if value is not None) >= max(6, valid_count - 1):
            regime = "全面降风险"
        else:
            regime = "无明确主线"
        return {
            "regime": regime, "valid_style_count": valid_count,
            "growth_minus_value": growth_avg - value_avg if growth_avg is not None and value_avg is not None else None,
            "large_minus_small": large_avg - small_avg if large_avg is not None and small_avg is not None else None,
        }
    regime_by_period = {period["period_id"]: classify(period["period_id"]) for period in periods}
    latest = periods[-1]["period_id"] if periods else "W0"
    action_periods = [period for period in periods if period.get("completeness") == "complete"]
    action_pid = action_periods[-1]["period_id"] if action_periods else latest
    current = regime_by_period.get(latest, {"regime": "数据不足", "valid_style_count": 0})
    return output, {
        "current_regime": current["regime"], "valid_style_count": current["valid_style_count"],
        "growth_minus_value": current.get("growth_minus_value"), "large_minus_small": current.get("large_minus_small"),
        "current_period": latest,
        "current_is_partial": bool(periods and periods[-1].get("completeness") == "partial"),
        "action_regime": (regime_by_period.get(action_pid) or {}).get("regime", "数据不足"),
        "action_period": action_pid,
        "regime_by_period": regime_by_period,
    }


def monitor_state(period_values: dict[str, dict[str, Any]], periods: list[dict[str, Any]], confirmed_state: str) -> str:
    if not periods or periods[-1].get("completeness") != "partial":
        return "无进行中周"
    current = period_values.get(periods[-1]["period_id"], {})
    if current.get("data_status") != "ok":
        return "进行中数据不足"
    positive = current.get("return", 0) > 0 and current.get("weekly_net_flow", 0) > 0
    negative = current.get("return", 0) < 0 and current.get("weekly_net_flow", 0) < 0
    if confirmed_state in {"持续主线", "加速", "新启动"} and negative:
        return "进行中转弱预警"
    if confirmed_state in {"退潮", "持续流出"} and positive:
        return "进行中修复观察"
    if positive:
        return "进行中延续"
    if negative:
        return "进行中转弱"
    return "进行中分歧"


def _flow_entity(rows: list[dict[str, Any]], kind: str, symbol: str) -> tuple[str, str]:
    sample = rows[-1] if rows else {}
    name = str(sample.get("industry") if kind == "industry" else sample.get("name") or sample.get("industry") or symbol)
    code = str(sample.get("ts_code") or symbol)
    return code, name


def sector_portfolio_coverage(name: str, portfolio: dict[str, Any]) -> dict[str, Any]:
    direct_funds: list[str] = []
    indirect_funds: list[str] = []
    direct_weight = 0.0
    indirect_weight = 0.0
    matched_themes: set[str] = set()
    for fund in portfolio.get("funds") or []:
        weight = float(fund.get("current_weight") or 0)
        fund_name = str(fund.get("name") or fund.get("code") or "未知基金")
        fund_direct = False
        fund_indirect = False
        for theme in fund.get("themes") or []:
            if theme in GENERIC_FUND_THEMES:
                continue
            rules = THEME_SECTOR_RULES.get(theme)
            if not rules:
                continue
            if any(token.lower() in name.lower() for token in rules["direct"]):
                fund_direct = True
                matched_themes.add(theme)
            elif any(token.lower() in name.lower() for token in rules["indirect"]):
                fund_indirect = True
                matched_themes.add(theme)
        if fund_direct:
            direct_funds.append(fund_name)
            direct_weight += weight
        elif fund_indirect:
            indirect_funds.append(fund_name)
            indirect_weight += weight
    if direct_funds:
        status = "直接主题覆盖"
    elif indirect_funds:
        status = "间接主题覆盖"
    else:
        status = "未发现已披露覆盖"
    return {
        "portfolio_coverage": status,
        "coverage_weight": min(1.0, direct_weight + indirect_weight),
        "direct_coverage_weight": min(1.0, direct_weight),
        "indirect_coverage_weight": min(1.0, indirect_weight),
        "related_holdings": direct_funds + indirect_funds,
        "matched_portfolio_themes": sorted(matched_themes),
        "coverage_basis": "按最新可用基金画像主题映射；主动基金实际持仓可能在披露后变化",
    }


def select_rotation_rows(rows: list[dict[str, Any]], periods: list[dict[str, Any]], limit: int = 15) -> list[dict[str, Any]]:
    """Return the same auditable three-week display universe for every renderer."""
    if not rows:
        return []
    signal_count: dict[str, int] = {str(row.get("entity_id")): 0 for row in rows}
    for period in periods:
        pid = period.get("period_id")
        for metric in ("return_percentile", "flow_percentile"):
            ranked = sorted(
                (row for row in rows if (row.get("periods") or {}).get(pid, {}).get(metric) is not None),
                key=lambda row: (row.get("periods") or {}).get(pid, {}).get(metric),
                reverse=True,
            )[:5]
            for row in ranked:
                signal_count[str(row.get("entity_id"))] += 1
    latest = periods[-1].get("period_id") if periods else "W0"
    signal_key = lambda row: (
        signal_count[str(row.get("entity_id"))],
        max(
            (row.get("periods") or {}).get(latest, {}).get("return_percentile") or -1,
            (row.get("periods") or {}).get(latest, {}).get("flow_percentile") or -1,
        ),
        str(row.get("name") or ""),
    )
    # Reserve roughly two thirds for market leaders and one third for portfolio exposures.
    signal_slots = min(10, limit)
    market_rows = sorted(
        (row for row in rows if signal_count[str(row.get("entity_id"))] > 0),
        key=signal_key,
        reverse=True,
    )[:signal_slots]
    held_rows = sorted(
        (row for row in rows if (row.get("coverage_weight") or 0) > 0),
        key=lambda row: (
            row.get("coverage_weight") or 0,
            signal_count[str(row.get("entity_id"))],
            max(
                (row.get("periods") or {}).get(latest, {}).get("return_percentile") or -1,
                (row.get("periods") or {}).get(latest, {}).get("flow_percentile") or -1,
            ),
            str(row.get("name") or ""),
        ),
        reverse=True,
    )[: max(0, limit - signal_slots)]
    selected = list(market_rows)
    selected_ids = {str(row.get("entity_id")) for row in selected}
    for row in held_rows:
        if str(row.get("entity_id")) not in selected_ids:
            selected.append(row)
            selected_ids.add(str(row.get("entity_id")))
    if len(selected) < limit:
        for row in sorted(rows, key=signal_key, reverse=True):
            if str(row.get("entity_id")) not in selected_ids:
                selected.append(row)
                selected_ids.add(str(row.get("entity_id")))
            if len(selected) >= limit:
                break
    return selected[:limit]


def _sector_week(rows: list[dict[str, Any]], kind: str, period: dict[str, Any]) -> dict[str, Any]:
    expected = set(period["trade_dates"])
    selected = [row for row in rows if row_date(row) and row_date(row).isoformat() in expected]
    present = {row_date(row).isoformat() for row in selected if row_date(row)}
    complete = expected.issubset(present)
    flow_values = [parse_number(row.get("net_amount")) for row in selected]
    weekly_flow = sum(value for value in flow_values if value is not None) if flow_values and all(value is not None for value in flow_values) else None
    if weekly_flow is not None:
        weekly_flow *= 100_000_000
    index_key = "close" if kind == "industry" else "industry_index"
    values = series(rows, (index_key,))
    weekly_return, latest = period_return(values, parse_day(period["baseline_date"]), parse_day(period["end_date"]), parse_day(period["start_date"]))
    if not complete:
        weekly_flow = None
        weekly_return = None
    evidence_complete = complete and weekly_flow is not None and weekly_return is not None
    return {
        "return": weekly_return,
        "weekly_net_flow": weekly_flow,
        "daily_average_flow": weekly_flow / len(expected) if weekly_flow is not None and expected else None,
        "covered_trade_days": len(present),
        "expected_trade_days": len(expected),
        "data_status": "ok" if evidence_complete else "insufficient_data",
        "missing_evidence": [] if evidence_complete else [
            label for label, missing in (
                ("交易日覆盖", not complete), ("周收益", weekly_return is None), ("周资金", weekly_flow is None)
            ) if missing
        ],
        "latest_date": latest,
    }


def rotation_state(period_values: dict[str, dict[str, Any]], periods: list[dict[str, Any]]) -> tuple[str, str]:
    ordered = [period for period in periods if period["completeness"] == "complete"]
    valid = [
        period for period in ordered
        if period_values.get(period["period_id"], {}).get("data_status") == "ok"
        and period_values.get(period["period_id"], {}).get("return") is not None
        and period_values.get(period["period_id"], {}).get("weekly_net_flow") is not None
    ]
    if len(valid) < 2:
        return "数据不足", "不足两个完整可比较周"
    recent = valid[-1]
    prior = valid[-2]
    current = period_values[recent["period_id"]]
    previous = period_values[prior["period_id"]]
    positive_pairs = sum(period_values[p["period_id"]].get("return", 0) > 0 and period_values[p["period_id"]].get("weekly_net_flow", 0) > 0 for p in valid)
    negative_pairs = sum(period_values[p["period_id"]].get("return", 0) < 0 and period_values[p["period_id"]].get("weekly_net_flow", 0) < 0 for p in valid)
    current_positive = current.get("return", 0) > 0 and current.get("weekly_net_flow", 0) > 0
    current_negative = current.get("return", 0) < 0 and current.get("weekly_net_flow", 0) < 0
    prior_positive = previous.get("return", 0) > 0 and previous.get("weekly_net_flow", 0) > 0
    if prior_positive and current_negative:
        return "退潮", "前一完整周强势，最新完整周收益和资金同时转负"
    if negative_pairs >= 2 and current_negative:
        return "持续流出", "至少两个完整周收益和资金同时为负"
    if positive_pairs >= 2 and current_positive:
        rp = current.get("return_percentile")
        pp = previous.get("return_percentile")
        fp = current.get("flow_percentile")
        pfp = previous.get("flow_percentile")
        if (rp is not None and pp is not None and rp - pp >= 20) or (fp is not None and pfp is not None and fp - pfp >= 20):
            return "加速", "连续两个完整周同向，最新收益或资金排名提升至少20个百分点"
        return "持续主线", "至少两个完整周收益和资金同时为正"
    if current.get("return", 0) > 0 and current.get("weekly_net_flow", 0) <= 0:
        return "高位分歧", "最新完整周收益为正但资金未同步"
    if not prior_positive and current_positive and (current.get("return_percentile") or 0) >= 70 and (current.get("flow_percentile") or 0) >= 70:
        return "新启动", "最新完整周收益和资金转正并进入前30%"
    if current_positive:
        return "单周脉冲", "仅最新完整周转强，前周未确认"
    return "高位分歧", "收益与资金方向未形成连续一致证据"


def analyze_sectors(cache_database: str | None, periods: list[dict[str, Any]], portfolio: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    if not cache_database or not Path(cache_database).exists():
        return [], [], {"status": "degraded", "reason": "incremental cache unavailable"}
    outputs: dict[str, list[dict[str, Any]]] = {"industry": [], "concept": []}
    with CacheStore(Path(cache_database)) as store:
        for kind in ("industry", "concept"):
            dataset = f"{kind}_flow_daily"
            entities = []
            for symbol in store.list_symbols(dataset):
                rows = store.get_series(dataset, symbol)
                code, name = _flow_entity(rows, kind, symbol)
                weekly = {period["period_id"]: _sector_week(rows, kind, period) for period in periods}
                entities.append({"entity_id": code, "name": name, "kind": kind, "periods": weekly})
            for period in periods:
                pid = period["period_id"]
                return_percentiles = percentile_map({row["entity_id"]: row["periods"][pid].get("return") for row in entities})
                flow_percentiles = percentile_map({row["entity_id"]: row["periods"][pid].get("weekly_net_flow") for row in entities})
                for row in entities:
                    row["periods"][pid]["return_percentile"] = return_percentiles[row["entity_id"]]
                    row["periods"][pid]["flow_percentile"] = flow_percentiles[row["entity_id"]]
            for row in entities:
                state, reason = rotation_state(row["periods"], periods)
                row["rotation_state"] = state
                row["rotation_reason"] = reason
                row["monitor_state"] = monitor_state(row["periods"], periods, state)
                row.update(sector_portfolio_coverage(row["name"], portfolio))
            outputs[kind] = entities
    return outputs["industry"], outputs["concept"], {"status": "ok", "industry_count": len(outputs["industry"]), "concept_count": len(outputs["concept"])}


def build_evidence(periods: list[dict[str, Any]], portfolio: dict[str, Any], styles: list[dict[str, Any]], industries: list[dict[str, Any]], concepts: list[dict[str, Any]], style_regime: dict[str, Any]) -> dict[str, Any]:
    evidence: dict[str, dict[str, Any]] = {}
    for pid, value in portfolio.get("weekly_returns", {}).items():
        evidence[f"portfolio:{pid}:return"] = {"id": f"portfolio:{pid}:return", "entity_id": "portfolio", "entity_name": "当前组合", "period": pid, "metric": "weekly_return", "value": value, "unit": "%", "source": "基金累计净值"}
    for row in styles:
        for pid, values in row["periods"].items():
            evidence[f"style:{row['name']}:{pid}:return"] = {"id": f"style:{row['name']}:{pid}:return", "entity_id": row["name"], "entity_name": row["name"], "period": pid, "metric": "weekly_return", "value": values.get("return"), "unit": "%", "source": "指数历史收盘价", "source_date": values.get("latest_date")}
    for row in industries + concepts:
        for pid, values in row["periods"].items():
            prefix = row["kind"]
            for metric, unit in (("return", "%"), ("weekly_net_flow", "元")):
                evidence_id = f"{prefix}:{row['entity_id']}:{pid}:{metric}"
                evidence[evidence_id] = {"id": evidence_id, "entity_id": row["entity_id"], "entity_name": row["name"], "period": pid, "metric": metric, "value": values.get(metric), "unit": unit, "source": "第三方 Tushare 代理", "source_date": values.get("latest_date")}
    return {"prompt_version": "three-week-v1", "periods": periods, "style_regime": style_regime, "evidence": evidence, "evidence_hash": stable_hash(evidence)}


def deterministic_synthesis(periods: list[dict[str, Any]], style_regime: dict[str, Any], industries: list[dict[str, Any]], portfolio: dict[str, Any], evidence_bundle: dict[str, Any]) -> dict[str, Any]:
    latest_pid = periods[-1]["period_id"] if periods else "W0"
    priority = {"加速": 0, "持续主线": 1, "新启动": 2, "高位分歧": 3, "单周脉冲": 4, "退潮": 5, "持续流出": 6, "数据不足": 7}
    sorted_rows = sorted(industries, key=lambda row: (priority.get(row["rotation_state"], 9), -(row["periods"].get(latest_pid, {}).get("return_percentile") or -1)))
    leaders = [row for row in sorted_rows if row["rotation_state"] in {"加速", "持续主线", "新启动"} and row.get("monitor_state") in {"进行中延续", "无进行中周"}][:5]
    divergent = [row for row in sorted_rows if row["rotation_state"] in {"加速", "持续主线", "新启动"} and row.get("monitor_state") == "进行中分歧"][:5]
    fading = [
        row for row in sorted_rows
        if (
            row.get("monitor_state") == "进行中转弱预警"
            or (row["rotation_state"] in {"退潮", "持续流出"} and row.get("monitor_state") != "进行中修复观察")
        )
    ][:5]
    recovering = [row for row in sorted_rows if row.get("monitor_state") == "进行中修复观察"][:5]
    refs = []
    reference_periods = [period["period_id"] for period in periods[-2:]]
    for row in leaders + divergent + fading:
        refs.extend([f"industry:{row['entity_id']}:{pid}:return" for pid in reference_periods])
        refs.extend([f"industry:{row['entity_id']}:{pid}:weekly_net_flow" for pid in reference_periods])
    return {
        "status": "deterministic_fallback",
        "market_regime": style_regime.get("current_regime"),
        "rotation_path": [{"entity": row["name"], "state": row["rotation_state"], "monitor_state": row.get("monitor_state"), "reason": f"{row['rotation_reason']}；{row.get('monitor_state')}"} for row in leaders + divergent + fading],
        "persistent_leaders": [row["name"] for row in leaders],
        "emerging_sectors": [row["name"] for row in leaders if row["rotation_state"] == "新启动"],
        "fading_sectors": [row["name"] for row in fading],
        "portfolio_implications": ["组合三周收益与市场风格切换需结合当前重复暴露判断。"],
        "action_explanations": ["进行中周只用于监测；正式动作至少需要两个完整周证据。"],
        "uncertainties": (
            [f"{row['name']}在进行中周收益与资金分歧，等待周度收盘确认。" for row in divergent]
            + [f"{row['name']}此前退潮，但进行中周出现修复，暂不再归入当前退潮方向。" for row in recovering]
            if industries else ["板块逐日历史不足，无法形成三周轮动判断。"]
        ),
        "confidence": "中" if len(periods) >= 3 and industries else "低",
        "evidence_refs": list(dict.fromkeys(ref for ref in refs if ref in evidence_bundle["evidence"])),
        "evidence_hash": evidence_bundle["evidence_hash"],
        "prompt_version": evidence_bundle["prompt_version"],
    }


def build_three_week_analysis(raw: dict[str, Any], current_analysis: dict[str, Any], history_weeks: int = 3) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    style_records = ((raw.get("market") or {}).get("style_indexes") or {})
    calendar_rows = next(iter(style_records.values()), [])
    trade_dates = sorted({row_date(row) for row in calendar_rows if row_date(row)})
    end = parse_day((raw.get("week") or {}).get("end_date")) or (trade_dates[-1] if trade_dates else dt.date.today())
    current_complete = (raw.get("week") or {}).get("completeness") == "complete"
    periods = build_periods(trade_dates, end, history_weeks, current_complete=current_complete)
    portfolio = analyze_portfolio(raw, current_analysis.get("portfolio") or {}, periods)
    portfolio["overlap_risk"] = ((current_analysis.get("comparison") or {}).get("overlap_risk") or [])
    styles, style_regime = analyze_styles(raw, periods)
    cache_database = ((raw.get("cache") or {}).get("database"))
    industries, concepts, sector_status = analyze_sectors(cache_database, periods, current_analysis.get("portfolio") or {})
    evidence = build_evidence(periods, portfolio, styles, industries, concepts, style_regime)
    synthesis = deterministic_synthesis(periods, style_regime, industries, portfolio, evidence)
    status = "degraded" if sector_status.get("status") != "ok" or len(periods) < 3 else "partial_current_week" if periods and periods[-1]["completeness"] == "partial" else "complete"
    return {
        "status": status,
        "periods": periods,
        "portfolio": portfolio,
        "styles": styles,
        "style_regime": style_regime,
        "industries": industries,
        "concepts": concepts,
        "rotation_path": synthesis["rotation_path"],
        "sector_status": sector_status,
        "evidence_index": evidence["evidence"],
    }, evidence, synthesis
