#!/usr/bin/env python3
"""Analyze weekly fund data into an auditable decision model."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

from report_contract import MANDATORY_SECTION_ORDER, REPORT_FORMAT_VERSION

from cache_store import stable_hash
from data_access import holdings_hash, holdings_metadata, load_json, normalize_holdings, parse_number, write_json
from margin_leverage import analyze_margin_leverage, build_three_week_margin
from three_week_analysis import build_three_week_analysis


SCHEMA_VERSION = 2
DATA_REVISION = "2.8"
ETF_NAMES = {
    "560780": "广发中证半导体材料设备ETF",
    "562590": "华夏中证半导体材料设备ETF",
    "159516": "国泰中证半导体材料设备ETF",
    "159558": "易方达中证半导体材料设备ETF",
}
NON_ACTIONABLE_CONCEPT_TOKENS = {
    "融资融券", "深股通", "沪股通", "转融券", "标普", "MSCI", "富时罗素", "证金持股",
}
THEME_KEYWORDS = {
    "半导体设备/材料": ["半导体", "芯片", "集成电路", "先进封装", "中微公司", "北方华创", "拓荆科技", "华海清科"],
    "AI光模块/通信": ["光模块", "通信", "CPO", "新易盛", "中际旭创", "天孚通信", "长飞光纤"],
    "PCB/AI服务器": ["PCB", "服务器", "沪电股份", "胜宏科技", "生益科技", "深南电路"],
    "创新药/医药": ["创新药", "医药", "医疗", "CXO"],
    "红利价值": ["红利", "低波", "银行", "煤炭", "股息"],
    "新能源": ["新能源", "光伏", "储能", "电池"],
    "港股/海外科技": ["港股", "恒生", "互联网", "QDII", "海外"],
}
HIGH_VOLATILITY_THEMES = {"半导体设备/材料", "AI光模块/通信", "PCB/AI服务器", "新能源"}
SCORE_COMPONENT_LABELS = {
    "weekly_performance": "本周收益横向排名",
    "one_month_trend": "近1月趋势",
    "sector_confirmation": "板块收益与资金确认",
    "style_alignment": "市场风格一致性",
    "trading_quality": "产品/交易质量",
    "portfolio_fit": "组合适配度",
}


def load_sector_taxonomy() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[1] / "references" / "sector_taxonomy.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"exact": {}, "keyword_rules": []}


SECTOR_TAXONOMY = load_sector_taxonomy()


def classify_sector(name: str) -> dict[str, Any]:
    exact = (SECTOR_TAXONOMY.get("exact") or {}).get(name)
    if exact:
        return {**exact, "classification_basis": "行业名称精确映射", "classification_confidence": "高", "classification_status": "已分类"}
    for rule in SECTOR_TAXONOMY.get("keyword_rules") or []:
        if any(keyword.lower() in name.lower() for keyword in rule.get("keywords") or []):
            return {key: value for key, value in rule.items() if key != "keywords"} | {
                "classification_basis": "行业名称关键词映射", "classification_confidence": "中", "classification_status": "已分类"
            }
    return {
        "theme_l1": "待分类", "theme_l2": "待分类", "style_tags": [], "exposure_keys": [],
        "classification_basis": "现有行业名称未命中分类表；待使用板块成分股补充", "classification_confidence": "低", "classification_status": "待分类",
    }


def find_key(row: dict[str, Any], tokens: list[str]) -> str | None:
    for token in tokens:
        for key in row:
            if token.lower() in str(key).lower():
                return str(key)
    return None


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


def extract_series(records: list[dict[str, Any]], value_priority: list[str] | None = None) -> list[tuple[dt.date, float]]:
    if not records:
        return []
    sample = records[0]
    date_key = find_key(sample, ["净值日期", "日期", "date", "day"])
    value_key = find_key(sample, value_priority or ["分析净值", "复权单位净值", "复权净值", "累计净值", "单位净值", "收盘", "close", "最新价"])
    if not date_key or not value_key:
        return []
    parsed: dict[dt.date, float] = {}
    for row in records:
        day = parse_day(row.get(date_key))
        value = parse_number(row.get(value_key))
        if day and value is not None and value > 0:
            parsed[day] = value
    return sorted(parsed.items(), key=lambda item: item[0])


def value_on_or_before(series: list[tuple[dt.date, float]], day: dt.date) -> tuple[dt.date, float] | None:
    candidates = [item for item in series if item[0] <= day]
    return candidates[-1] if candidates else None


def return_between(series: list[tuple[dt.date, float]], start: dt.date, end: dt.date) -> float | None:
    baseline = value_on_or_before(series, start)
    latest = value_on_or_before(series, end)
    if not baseline or not latest or latest[0] <= baseline[0] or baseline[1] <= 0:
        return None
    return (latest[1] / baseline[1] - 1) * 100


def max_drawdown(series: list[tuple[dt.date, float]], end: dt.date, days: int = 365) -> float | None:
    values = [value for day, value in series if end - dt.timedelta(days=days) <= day <= end]
    if len(values) < 2:
        return None
    peak = values[0]
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        worst = min(worst, (value / peak - 1) * 100)
    return worst


def series_metrics(records: list[dict[str, Any]], week: dict[str, Any], value_priority: list[str] | None = None) -> dict[str, Any]:
    series = extract_series(records, value_priority)
    baseline_day = parse_day(week.get("baseline_date"))
    start_day = parse_day(week.get("start_date"))
    end_day = parse_day(week.get("end_date"))
    if not series or not baseline_day or not start_day or not end_day:
        return {"data_status": "insufficient_data", "week_return": None, "warning": "missing series or week dates"}
    bounded = [item for item in series if item[0] <= end_day]
    baseline = value_on_or_before(bounded, baseline_day)
    latest = value_on_or_before(bounded, end_day)
    if not baseline or not latest or latest[0] < start_day:
        return {"data_status": "insufficient_data", "week_return": None, "warning": "missing baseline or in-week endpoint"}
    baseline_lag = (baseline_day - baseline[0]).days
    if baseline_lag > 7:
        return {"data_status": "insufficient_data", "week_return": None, "warning": f"baseline is stale by {baseline_lag} days"}
    week_values = [value for day, value in bounded if baseline[0] <= day <= latest[0]]
    one_month = return_between(bounded, end_day - dt.timedelta(days=30), end_day)
    three_month = return_between(bounded, end_day - dt.timedelta(days=90), end_day)
    status = "ok" if latest[0] == end_day else "stale"
    return {
        "data_status": status,
        "baseline_date": baseline[0].isoformat(),
        "latest_date": latest[0].isoformat(),
        "baseline_value": baseline[1],
        "baseline_lag_days": baseline_lag,
        "latest_value": latest[1],
        "week_return": (latest[1] / baseline[1] - 1) * 100,
        "one_month": one_month,
        "three_month": three_month,
        "max_drawdown_1y": max_drawdown(bounded, end_day),
        "week_range": (max(week_values) / min(week_values) - 1) * 100 if week_values and min(week_values) > 0 else None,
    }


def infer_themes(*texts: Any) -> list[str]:
    joined = " ".join(str(text) for text in texts if text)
    themes = [theme for theme, words in THEME_KEYWORDS.items() if any(word.lower() in joined.lower() for word in words)]
    return themes or ["未识别"]


def profile_detail(data: dict[str, Any], code: str) -> tuple[dict[str, Any], dict[str, Any]]:
    wrapper = (data.get("fund_profiles") or {}).get(code) or {}
    return wrapper.get("detail") or (data.get("full_details") or {}).get(code) or {}, wrapper


def disclosure_key(row: dict[str, Any]) -> tuple[int, int, int]:
    for key, value in row.items():
        if any(token in str(key) for token in ["截止时间", "报告期", "季度", "日期"]):
            day = parse_day(value)
            if day:
                return day.year, day.month, day.day
            match = re.search(r"(20\d{2})年([1-4])季度", str(value))
            if match:
                return int(match.group(1)), int(match.group(2)) * 3, 1
    return 0, 0, 0


def latest_disclosure_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return []
    newest = max(disclosure_key(row) for row in rows)
    return [row for row in rows if disclosure_key(row) == newest] if newest != (0, 0, 0) else rows


def profile_evidence(detail: dict[str, Any]) -> dict[str, Any]:
    profile_rows = (detail.get("basic_info") or []) + (detail.get("ths_info") or [])
    latest_holdings = latest_disclosure_rows(detail.get("stock_holdings") or [])
    latest_industries = latest_disclosure_rows(detail.get("industry_allocation") or [])
    holding_text = " ".join(str(value) for row in latest_holdings for key, value in row.items() if any(token in str(key) for token in ["名称", "股票"]))
    industry_text = " ".join(str(value) for row in latest_industries for key, value in row.items() if any(token in str(key) for token in ["行业", "名称"]))
    size = turnover = None
    declared_type = ""
    for row in profile_rows:
        label = str(row.get("item") or row.get("项目") or row.get("字段") or row.get("指标") or "")
        value = row.get("value") or row.get("值") or row.get("内容")
        if size is None and any(token in label for token in ["最新规模", "基金规模", "资产净值"]):
            size = parse_number(value)
        if turnover is None and "换手率" in label:
            turnover = parse_number(value)
        if not declared_type and any(token in label for token in ["基金类型", "产品类型", "投资类型"]):
            declared_type = str(value or "")
    disclosure = max([disclosure_key(row) for row in latest_holdings + latest_industries] or [(0, 0, 0)])
    return {
        "profile_rows": profile_rows, "holding_text": holding_text, "industry_text": industry_text,
        "fund_size": size, "turnover": turnover, "declared_type": declared_type,
        "latest_holdings": latest_holdings, "latest_industries": latest_industries,
        "disclosure_period": "-" if disclosure == (0, 0, 0) else f"{disclosure[0]:04d}-{disclosure[1]:02d}",
    }


def classify_product(name: str, declared_type: str = "") -> str:
    evidence = f"{name} {declared_type}".upper()
    return "被动指数/ETF联接" if any(token in evidence for token in ["ETF", "指数", "联接"]) else "主动基金"


def fund_risk_flags(product_type: str, size: float | None, turnover: float | None) -> list[str]:
    flags = []
    if product_type == "主动基金" and size is not None and size < 200_000_000:
        flags.append("主动迷你规模")
    elif product_type == "主动基金" and size is not None and size < 500_000_000:
        flags.append("主动小规模")
    if product_type == "主动基金" and turnover is not None and turnover >= 300:
        flags.append("换手率高")
    return flags


def derive_weights(holdings: list[dict[str, Any]], portfolio_meta: dict[str, Any] | None = None) -> tuple[str, dict[str, float], str, str]:
    portfolio_meta = portfolio_meta or {}
    if portfolio_meta.get("weight_mode") == "assumed_equal":
        equal = 1 / len(holdings) if holdings else 0
        return "assumed_equal", {item["code"]: equal for item in holdings}, "假设等权", str(portfolio_meta.get("weight_note") or "未提供真实仓位，按等权假设分析。")
    explicit = [parse_number(item.get("current_weight")) for item in holdings]
    if holdings and all(value is not None and value > 0 for value in explicit):
        total = sum(float(value) for value in explicit if value is not None)
        return "user_weight", {item["code"]: float(value) / total for item, value in zip(holdings, explicit)}, "用户提供权重", "按用户提供的当前权重归一化。"
    total_amount = sum(parse_number(item.get("amount")) or 0 for item in holdings)
    if total_amount > 0:
        return "amount_weight", {item["code"]: (parse_number(item.get("amount")) or 0) / total_amount for item in holdings}, "持仓金额权重", "按持仓金额计算组合占比。"
    equal = 1 / len(holdings) if holdings else 0
    return "equal_weight", {item["code"]: equal for item in holdings}, "自动等权", "未提供有效金额或权重，自动按等权分析。"


def analyze_funds(data: dict[str, Any], holdings: list[dict[str, Any]], portfolio_meta: dict[str, Any] | None = None) -> dict[str, Any]:
    basis, weights, basis_display, assumption = derive_weights(holdings, portfolio_meta)
    rows = []
    warnings = []
    for holding in holdings:
        code = holding["code"]
        metrics = series_metrics((data.get("funds") or {}).get(code, {}).get("nav") or [], data.get("week") or {})
        if metrics.get("warning"):
            warnings.append(f"{code} {holding.get('name') or ''}: {metrics['warning']}")
        detail, wrapper = profile_detail(data, code)
        evidence = profile_evidence(detail)
        inferred = infer_themes(evidence["holding_text"], evidence["industry_text"], holding.get("name"), " ".join(holding.get("tags") or []))
        themes = sorted(set((holding.get("tags") or []) + ([] if inferred == ["未识别"] and holding.get("tags") else inferred)))
        product_type = classify_product(str(holding.get("name") or ""), evidence["declared_type"])
        risk_flags = fund_risk_flags(product_type, evidence["fund_size"], evidence["turnover"])
        rows.append(
            {
                "code": code,
                "name": holding.get("name") or code,
                "current_weight": round(weights.get(code, 0), 6),
                "is_core": bool(holding.get("is_core")),
                "themes": themes or ["未识别"],
                "theme_basis": "季度持仓/行业配置" if evidence["holding_text"] or evidence["industry_text"] else "名称/用户标签",
                "profile_status": ("partial_profile" if detail and not (evidence["holding_text"] or evidence["industry_text"]) else wrapper.get("profile_status")) or ("missing" if not detail else "ok"),
                "fund_size": evidence["fund_size"],
                "turnover": evidence["turnover"],
                "product_type": product_type,
                "disclosure_period": evidence["disclosure_period"],
                "candidate_kind": "fund",
                "product_evidence_available": bool(evidence["profile_rows"]),
                "theme_evidence_available": bool(evidence["holding_text"] or evidence["industry_text"] or product_type == "被动指数/ETF联接"),
                "quality_flags": risk_flags,
                **metrics,
            }
        )
    valid = [row for row in rows if row.get("week_return") is not None]
    coverage = sum(row["current_weight"] for row in valid)
    partial = sum(row["week_return"] * row["current_weight"] for row in valid) if valid else None
    formal = partial if coverage >= 0.90 else None
    return {
        "weight_basis": basis,
        "weight_basis_display": basis_display,
        "weight_assumption": assumption,
        "nav_coverage_weight": round(coverage, 6),
        "weekly_return": formal,
        "partial_weekly_return": partial,
        "return_status": "ok" if formal is not None else "insufficient_data",
        "funds": rows,
        "warnings": warnings,
    }


def analyze_style(data: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    metadata = (data.get("market") or {}).get("style_index_meta") or {}
    for name, records in ((data.get("market") or {}).get("style_indexes") or {}).items():
        metrics = series_metrics(records or [], data.get("week") or {})
        meta = metadata.get(name) or {}
        status_display = "正常" if metrics.get("week_return") is not None else "数据不足"
        rows.append({"name": name, **metrics, **meta, "data_status_display": status_display})
    return sorted(rows, key=lambda row: row.get("week_return") if row.get("week_return") is not None else -999, reverse=True)


def row_name(row: dict[str, Any]) -> str:
    key = find_key(row, ["板块名称", "板块", "名称", "行业", "概念"])
    return str(row.get(key)) if key and row.get(key) else ""


def period_value(row: dict[str, Any], period: str, kind: str) -> float | None:
    if kind == "return":
        tokens = [f"{period}涨跌幅", f"{period}涨幅", "阶段涨跌幅"]
    else:
        tokens = [f"{period}主力净流入-净额", f"{period}净流入-净额", "主力净流入-净额", "净流入-净额", "净额"]
    key = find_key(row, tokens)
    if not key or (kind == "flow" and any(skip in key for skip in ["占比", "净比", "%"])):
        return None
    return parse_number(row.get(key))


def flow_status(today: float | None, five: float | None, ten: float | None) -> str:
    available = [value for value in [today, five, ten] if value is not None]
    if len(available) < 2:
        return "数据不足"
    if sum(value != 0 for value in available) < 2:
        return "数据不足"
    positives = sum(value > 0 for value in available)
    negatives = sum(value < 0 for value in available)
    if today is not None and today > 0 and positives >= 2:
        return "持续流入"
    if today is not None and today < 0 and negatives >= 2:
        return "持续流出"
    if today is not None and today > 0:
        return "短线脉冲"
    return "分歧"


def flow_amount_yuan(row: dict[str, Any], value: float | None, period: str) -> float | None:
    if value is None:
        return None
    unit = str(row.get("资金单位") or "").strip()
    if unit == "亿元":
        return value * 100_000_000
    if unit == "万元":
        return value * 10_000
    if unit == "元":
        return value
    # Migrate compact v2.3 THS-summary fallback rows, whose 今日净流入
    # was expressed in 亿元 but carried no unit metadata.
    if period == "今日" and "今日涨跌幅" in row and len(row) <= 3 and abs(value) < 1_000_000:
        return value * 100_000_000
    return value


def build_flow_lookup(
    sectors: dict[str, Any],
    sector_type: str,
    *,
    completed_week: bool = True,
) -> dict[str, dict[str, float]]:
    output: dict[str, dict[str, float]] = {}
    has_period_end = bool(((sectors.get("fund_flow") or {}).get("报告期末日") or {}).get(sector_type))
    for period, period_data in (sectors.get("fund_flow") or {}).items():
        if period == "报告期末日":
            if not completed_week:
                continue
            logical_period = "今日"
        elif period == "今日" and completed_week and has_period_end:
            continue
        else:
            logical_period = period
        for row in period_data.get(sector_type) or []:
            name = row_name(row)
            value = period_value(row, logical_period, "flow")
            if name and value is not None:
                output.setdefault(name, {})[logical_period] = flow_amount_yuan(row, value, logical_period)
    return output


def historical_flow_evidence(records: list[dict[str, Any]], week: dict[str, Any]) -> dict[str, Any]:
    end = parse_day(week.get("end_date"))
    if not end:
        return {}
    parsed: list[tuple[dt.date, float]] = []
    for row in records or []:
        date_key = find_key(row, ["日期", "date"])
        flow_key = find_key(row, ["主力净流入-净额"])
        day = parse_day(row.get(date_key)) if date_key else None
        value = parse_number(row.get(flow_key)) if flow_key else None
        if day and day <= end and value is not None:
            parsed.append((day, value))
    by_day = dict(parsed)
    series = sorted(by_day.items())
    if not series or series[-1][0] != end:
        return {}
    last5, last10 = series[-5:], series[-10:]
    return {
        "今日": series[-1][1],
        "5日": sum(value for _, value in last5) if len(last5) == 5 else None,
        "10日": sum(value for _, value in last10) if len(last10) == 10 else None,
        "source_date": end.isoformat(),
        "period_dates": {
            "今日": [series[-1][0].isoformat(), series[-1][0].isoformat()],
            "5日": [last5[0][0].isoformat(), last5[-1][0].isoformat()] if len(last5) == 5 else None,
            "10日": [last10[0][0].isoformat(), last10[-1][0].isoformat()] if len(last10) == 10 else None,
        },
    }


def enrich_flows_with_history(flows: dict[str, dict[str, Any]], sectors: dict[str, Any], week: dict[str, Any]) -> None:
    for name, records in (sectors.get("industry_flow_history") or {}).items():
        evidence = historical_flow_evidence(records, week)
        if not evidence:
            continue
        values = flows.setdefault(name, {})
        official_five = values.get("5日")
        derived_five = evidence.get("5日")
        conflict = official_five is not None and derived_five is not None and official_five * derived_five < 0
        values["今日"] = evidence.get("今日")
        values["10日"] = evidence.get("10日")
        if official_five is None:
            values["5日"] = derived_five
        values["_conflict"] = conflict
        values["_derived_five"] = derived_five
        values["_source_dates"] = {period: evidence["source_date"] for period in ["今日", "5日", "10日"] if evidence.get(period) is not None}
        values["_period_dates"] = evidence.get("period_dates") or {}
        values["_history_basis"] = "板块逐日历史资金流聚合"


def flow_status_reason(status: str, values: dict[str, Any]) -> str:
    available = {period: values.get(period) for period in ["今日", "5日", "10日"] if values.get(period) is not None}
    missing = [period for period in ["今日", "5日", "10日"] if values.get(period) is None]
    sign_text = "、".join(f"{period}{'流入' if value > 0 else '流出' if value < 0 else '持平'}" for period, value in available.items())
    if status == "数据冲突":
        return "官方5日排名与逐日历史聚合方向相反，停止趋势判断"
    if status == "数据不足":
        if not missing and sum(value != 0 for value in available.values()) < 2:
            return f"方向证据不足：{sign_text}；仅一个周期出现非零资金方向"
        if len(available) == 1:
            period, value = next(iter(available.items()))
            direction = "净流入" if value > 0 else "净流出" if value < 0 else "持平"
            return f"单周期证据：仅有{period}累计{direction}；缺少{'、'.join(missing)}，无法判断资金持续性"
        return f"无足够资金周期；缺少{'、'.join(missing)}，无法判断资金持续性"
    return f"{status}：{sign_text}"


def portfolio_theme_context(portfolio: dict[str, Any]) -> tuple[Counter[str], dict[str, list[str]], dict[str, float]]:
    counter: Counter[str] = Counter()
    names: dict[str, list[str]] = {}
    weights: dict[str, float] = {}
    for fund in portfolio.get("funds") or []:
        for theme in fund.get("themes") or []:
            if theme not in THEME_KEYWORDS:
                continue
            counter[theme] += 1
            names.setdefault(theme, []).append(fund["name"])
            weights[theme] = weights.get(theme, 0) + fund.get("current_weight", 0)
    return counter, names, weights


def sector_item(
    name: str,
    kind: str,
    weekly_return: float | None,
    flows: dict[str, Any],
    return_basis: str,
    theme_names: dict[str, list[str]],
    theme_weights: dict[str, float],
    source_date: str,
    universe_scope: str = "全市场",
    flow_metadata: dict[str, dict[str, Any]] | None = None,
    cache_age_days: int = 0,
    flow_cutoff: str | None = None,
) -> dict[str, Any]:
    classification = classify_sector(name)
    exposure_keys = classification.get("exposure_keys") or []
    related = sorted({fund for theme in exposure_keys for fund in theme_names.get(theme, [])})
    coverage_weight = min(1.0, sum(theme_weights.get(theme, 0) for theme in exposure_keys))
    coverage = "高度覆盖" if coverage_weight >= 0.35 else "部分覆盖" if coverage_weight > 0 else "缺失"
    cutoff_day = parse_day(flow_cutoff)
    source_overrides = flows.get("_source_dates") or {}
    eligible_flows = {
        period: value for period, value in flows.items()
        if period in {"今日", "5日", "10日"} and (not cutoff_day or not parse_day(source_overrides.get(period) or (flow_metadata or {}).get(period, {}).get("source_date")) or parse_day(source_overrides.get(period) or (flow_metadata or {}).get(period, {}).get("source_date")) <= cutoff_day)
    }
    today, five, ten = eligible_flows.get("今日"), eligible_flows.get("5日"), eligible_flows.get("10日")
    status = "数据冲突" if flows.get("_conflict") else flow_status(today, five, ten)
    flow_metadata = flow_metadata or {}
    flow_dates = {period: source_overrides.get(period) or meta.get("source_date") for period, meta in flow_metadata.items() if period in eligible_flows and (source_overrides.get(period) or meta.get("source_date"))}
    for period in eligible_flows:
        if period not in flow_dates and source_overrides.get(period):
            flow_dates[period] = source_overrides[period]
    flow_basis = " / ".join(f"{period}@{day}" for period, day in flow_dates.items()) or "unavailable"
    relevant_ages = [flow_metadata.get(period, {}).get("cache_age_days") or 0 for period in eligible_flows]
    return {
        "name": name,
        "type": kind,
        "week_return": weekly_return,
        "today_flow": today,
        "five_day_flow": five,
        "ten_day_flow": ten,
        "flow_status": status,
        "flow_status_display": "仅单周期，暂不判断" if status == "数据不足" else status,
        "flow_status_reason": flow_status_reason(status, eligible_flows | {"_conflict": flows.get("_conflict")}),
        "flow_conflict": bool(flows.get("_conflict")),
        "flow_period_dates": flows.get("_period_dates") or {},
        "return_basis": return_basis,
        "flow_basis": f"{flow_basis} · {flows.get('_history_basis') or '资金净额统一换算为人民币元，报告按亿元展示'}",
        "flow_unit": "元",
        "flow_source_dates": flow_dates,
        "source_date": source_date,
        "universe_scope": universe_scope,
        "cache_age_days": max([cache_age_days] + relevant_ages),
        "theme": f"{classification['theme_l1']}/{classification['theme_l2']}" if classification.get("classification_status") == "已分类" else "待分类",
        "theme_l1": classification.get("theme_l1"),
        "theme_l2": classification.get("theme_l2"),
        "style_tags": classification.get("style_tags") or [],
        "exposure_keys": exposure_keys,
        "classification_basis": classification.get("classification_basis"),
        "classification_confidence": classification.get("classification_confidence"),
        "classification_status": classification.get("classification_status"),
        "current_coverage": coverage,
        "coverage_weight": coverage_weight,
        "related_holdings": related,
        "candidate_funds": [],
        "candidate_etfs": ["560780", "562590", "159516", "159558"] if "半导体设备/材料" in exposure_keys else [],
        "interpretation": f"{name} 周收益口径为{return_basis}；资金状态{status}；当前组合{coverage}。",
    }


def analyze_sectors(data: dict[str, Any], portfolio: dict[str, Any]) -> dict[str, Any]:
    sectors = ((data.get("market") or {}).get("sectors") or {})
    _, theme_names, theme_weights = portfolio_theme_context(portfolio)
    source_date = (data.get("week") or {}).get("end_date") or ""
    result: dict[str, Any] = {
        "industry_return": [],
        "concept_return": [],
        "industry_today": [],
        "concept_today": [],
        "industry_today_inflow": [],
        "concept_today_inflow": [],
        "industry_today_outflow": [],
        "concept_today_outflow": [],
        "industry_inflow": [],
        "concept_inflow": [],
        "industry_outflow": [],
        "concept_outflow": [],
        "theme_signal_proxy": [],
    }
    for sector_type, kind, prefix in [("行业资金流", "industry", "industry"), ("概念资金流", "concept", "concept")]:
        universe_scope = (sectors.get("universe_scope") or {}).get(prefix) or "全市场"
        flows = build_flow_lookup(sectors, sector_type, completed_week=True)
        current_flows = build_flow_lookup(sectors, sector_type, completed_week=False)
        if sector_type == "行业资金流":
            enrich_flows_with_history(flows, sectors, data.get("week") or {})
        all_flow_meta = sectors.get("flow_meta") or {}
        report_end_meta = ((all_flow_meta.get("报告期末日") or {}).get(sector_type) or {})
        current_meta = ((all_flow_meta.get("今日") or {}).get(sector_type) or {})
        flow_meta = {
            "今日": report_end_meta or current_meta,
            "5日": ((all_flow_meta.get("5日") or {}).get(sector_type) or {}),
            "10日": ((all_flow_meta.get("10日") or {}).get(sector_type) or {}),
        }
        five_rows = ((sectors.get("fund_flow") or {}).get("5日") or {}).get(sector_type) or []
        five_meta = flow_meta.get("5日") or {}
        weekly = []
        for row in five_rows:
            name = row_name(row)
            value = period_value(row, "5日", "return")
            non_actionable = kind == "concept" and any(token in name for token in NON_ACTIONABLE_CONCEPT_TOKENS)
            if name and value is not None and not non_actionable:
                weekly.append(sector_item(
                    name, kind, value, flows.get(name, {}),
                    row.get("return_basis") or five_meta.get("return_basis") or "5日资金流排行涨跌幅",
                    theme_names, theme_weights,
                    row.get("source_date") or five_meta.get("source_date") or source_date,
                    universe_scope, flow_meta, five_meta.get("cache_age_days") or 0,
                    (data.get("week") or {}).get("end_date"),
                ))
        result[f"{prefix}_return"] = sorted(weekly, key=lambda item: item["week_return"], reverse=True)[:10]

        # Completed-week flow rankings use the 5-day period ending on the
        # report date. A later current-day snapshot stays in the 今日 section.
        result[f"{prefix}_inflow"] = sorted(
            [item for item in weekly if (item.get("five_day_flow") or 0) > 0],
            key=lambda item: item["five_day_flow"], reverse=True,
        )[:10]
        result[f"{prefix}_outflow"] = sorted(
            [item for item in weekly if (item.get("five_day_flow") or 0) < 0],
            key=lambda item: item["five_day_flow"],
        )[:10]

        today_rows = sectors.get(f"{prefix}_today") or []
        snapshot_basis = "今日快照，不属于周收益"
        snapshot_date = current_meta.get("source_date") or (data.get("week") or {}).get("collection_trade_date") or source_date
        if prefix == "concept" and not today_rows:
            today_rows = sectors.get("concept_latest_close") or []
            snapshot_basis = "最近有效收盘快照，不属于周收益"
            snapshot_date = next((row.get("source_date") for row in today_rows if row.get("source_date")), source_date)
            result["concept_snapshot_kind"] = "latest_close"
            result["concept_snapshot_date"] = snapshot_date
        current_flow_date = current_meta.get("source_date")
        snapshot_flow_aligned = bool(
            parse_day(snapshot_date) and parse_day(current_flow_date)
            and parse_day(snapshot_date) == parse_day(current_flow_date)
        )
        today_items = []
        for row in today_rows:
            name = row_name(row)
            key = find_key(row, ["涨跌幅", "涨幅"])
            value = parse_number(row.get(key)) if key else None
            if name and value is not None:
                name_flows = current_flows.get(name, {}) if snapshot_flow_aligned else {}
                today_only = {
                    "今日": name_flows.get("今日"),
                    "_source_dates": {
                        "今日": (name_flows.get("_source_dates") or {}).get("今日") if snapshot_flow_aligned else None
                    },
                }
                item = sector_item(
                    name, kind, None, today_only, snapshot_basis, theme_names, theme_weights,
                    snapshot_date, universe_scope,
                    {"今日": current_meta} if snapshot_flow_aligned else {},
                    current_meta.get("cache_age_days") or 0,
                )
                item["today_return"] = value
                today_items.append(item)
        result[f"{prefix}_today"] = sorted(today_items, key=lambda item: item["today_return"], reverse=True)[:10]
        result[f"{prefix}_today_inflow"] = sorted(
            [item for item in today_items if (item.get("today_flow") or 0) > 0],
            key=lambda item: item["today_flow"], reverse=True,
        )[:10]
        result[f"{prefix}_today_outflow"] = sorted(
            [item for item in today_items if (item.get("today_flow") or 0) < 0],
            key=lambda item: item["today_flow"],
        )[:10]

    result["theme_signal_proxy"] = theme_signal_proxy(data, theme_names, theme_weights)
    return result


def delivery_readiness(
    sectors: dict[str, Any],
    three_week: dict[str, Any],
    data_quality: list[dict[str, Any]],
) -> dict[str, Any]:
    """Summarize whether a report is complete enough for normal user delivery."""
    counts = {
        key: len(sectors.get(key) or [])
        for key in (
            "industry_return", "concept_return",
            "industry_inflow", "concept_inflow",
            "industry_outflow", "concept_outflow",
            "industry_today", "concept_today",
        )
    }
    core_requirements = {
        "行业近5个交易日收益": counts["industry_return"] > 0,
        "概念近5个交易日收益": counts["concept_return"] > 0,
        "行业近5日资金流入": counts["industry_inflow"] > 0,
        "行业近5日资金流出": counts["industry_outflow"] > 0,
        "概念近5日资金流入": counts["concept_inflow"] > 0,
        "概念近5日资金流出": counts["concept_outflow"] > 0,
        "行业最新行情": counts["industry_today"] > 0,
        "概念最近有效收盘": counts["concept_today"] > 0,
    }
    unresolved = [
        str(row.get("dataset") or "unknown")
        for row in data_quality
        if row.get("requirement", "required") == "required"
        and row.get("status") in {"failed", "partial"}
    ]
    latest_period = next(
        (row.get("period_id") for row in reversed(three_week.get("periods") or []) if row.get("period_id")),
        None,
    )
    contradictions = []
    for kind, label, top10_key in (
        ("industries", "行业", "industry_return"),
        ("concepts", "概念", "concept_return"),
    ):
        valid_latest_rows = sum(
            ((row.get("periods") or {}).get(latest_period) or {}).get("data_status") == "ok"
            for row in three_week.get(kind) or []
        ) if latest_period else 0
        if valid_latest_rows and not counts[top10_key]:
            contradictions.append(
                f"三周{label}序列在{latest_period}有{valid_latest_rows}条有效记录，但单周{label}Top10为空"
            )
    blockers = [name for name, ready in core_requirements.items() if not ready]
    blockers.extend(f"未解决数据集：{name}" for name in unresolved)
    blockers.extend(contradictions)
    return {
        "status": "complete" if not blockers else "degraded",
        "core_requirements": core_requirements,
        "row_counts": counts,
        "unresolved_required_datasets": unresolved,
        "consistency_errors": contradictions,
        "blockers": list(dict.fromkeys(blockers)),
    }


def theme_signal_proxy(data: dict[str, Any], theme_names: dict[str, list[str]], theme_weights: dict[str, float]) -> list[dict[str, Any]]:
    values: dict[str, dict[str, float]] = {}
    seen_codes = set()
    for rows in (data.get("rankings") or {}).values():
        for row in rows or []:
            code_key = find_key(row, ["基金代码", "代码"])
            name_key = find_key(row, ["基金简称", "基金名称", "名称"])
            return_key = find_key(row, ["近1周"])
            if not name_key or not return_key:
                continue
            code = str(row.get(code_key) or row.get(name_key))
            if code in seen_codes:
                continue
            seen_codes.add(code)
            weekly = parse_number(row.get(return_key))
            if weekly is None:
                continue
            for theme in infer_themes(row.get(name_key)):
                if theme != "未识别":
                    values.setdefault(theme, {})[code] = weekly
    output = []
    for theme, by_code in values.items():
        observations = sorted(by_code.values(), reverse=True)[:20]
        output.append(
            {
                "name": theme,
                "type": "fund_ranking_proxy",
                "average_fund_week_return": sum(observations) / len(observations),
                "sample_size": len(observations),
                "return_basis": "近1周基金排行名称主题代理，非板块收益",
                "current_coverage": "部分覆盖" if theme_names.get(theme) else "缺失",
                "coverage_weight": theme_weights.get(theme, 0),
                "related_holdings": theme_names.get(theme, []),
            }
        )
    return sorted(output, key=lambda item: item["average_fund_week_return"], reverse=True)[:10]


def top_weekly_funds(data: dict[str, Any]) -> list[dict[str, Any]]:
    output, seen = [], set()
    for group, rows in (data.get("rankings") or {}).items():
        for row in rows or []:
            code_key = find_key(row, ["基金代码", "代码"])
            name_key = find_key(row, ["基金简称", "基金名称", "名称"])
            week_key = find_key(row, ["近1周"])
            month_key = find_key(row, ["近1月"])
            if not code_key or not name_key or not week_key:
                continue
            code = str(row.get(code_key)).zfill(6)
            weekly = parse_number(row.get(week_key))
            if code in seen or weekly is None:
                continue
            seen.add(code)
            detail = (data.get("ranking_details") or {}).get(code) or {}
            evidence = profile_evidence(detail)
            profile_rows = evidence["profile_rows"]
            size, turnover = evidence["fund_size"], evidence["turnover"]
            themes = infer_themes(evidence["holding_text"], evidence["industry_text"], row.get(name_key))
            fund_name = str(row.get(name_key))
            product_type = classify_product(fund_name, evidence["declared_type"])
            passive = product_type == "被动指数/ETF联接"
            risk_flags = fund_risk_flags(product_type, size, turnover)
            output.append(
                {
                    "code": code,
                    "name": fund_name,
                    "group": group,
                    "candidate_kind": "fund",
                    "week_return": weekly,
                    "one_month": parse_number(row.get(month_key)) if month_key else None,
                    "themes": themes,
                    "fund_size": size,
                    "turnover": turnover,
                    "product_type": product_type,
                    "theme_basis": "季度持仓/行业配置" if evidence["holding_text"] or evidence["industry_text"] else "基金名称",
                    "product_evidence_available": bool(profile_rows),
                    "theme_evidence_available": bool(evidence["holding_text"] or evidence["industry_text"] or passive),
                    "disclosure_period": evidence["disclosure_period"],
                    "quality_flags": risk_flags,
                    "return_basis": "基金排行收益字段",
                }
            )
    return sorted(output, key=lambda item: item["week_return"], reverse=True)[:20]


def spot_map(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    output = {}
    for row in rows or []:
        key = find_key(row, ["代码", "ts_code", "symbol", "code"])
        value = str(row.get(key) or "") if key else ""
        match = re.search(r"(\d{6})", value)
        if match:
            output[match.group(1)] = row
    return output


def has_price_discontinuity(records: list[dict[str, Any]], threshold: float = 25.0, value_priority: list[str] | None = None) -> bool:
    series = extract_series(records, value_priority)
    for (_, previous), (_, current) in zip(series, series[1:]):
        if previous > 0 and abs((current / previous - 1) * 100) > threshold:
            return True
    return False


def _select_etf_return_evidence(etf_data: dict[str, Any], code: str, week: dict[str, Any]) -> dict[str, Any]:
    history = (etf_data.get("history") or {}).get(code, {})
    rejected_flags = []
    price_priority = ["收盘", "close", "最新价"]
    hfq = series_metrics(history.get("hfq") or [], week, price_priority)
    qfq = series_metrics(history.get("qfq") or [], week, price_priority)
    if hfq.get("week_return") is not None and not has_price_discontinuity(history.get("hfq") or [], value_priority=price_priority):
        flags = []
        if qfq.get("week_return") is not None and abs(hfq["week_return"] - qfq["week_return"]) > 2:
            flags.append("前后复权差异较大")
        return {**hfq, "return_basis": "后复权价格", "quality_flags": flags}
    if hfq.get("week_return") is not None:
        rejected_flags.append("后复权序列异常断点")

    nav_records = (etf_data.get("nav") or {}).get(code) or []
    cumulative = series_metrics(nav_records, week, ["累计净值", "复权净值"])
    unit = series_metrics(nav_records, week, ["单位净值"])
    unit_split = unit.get("week_return") is not None and has_price_discontinuity(nav_records, value_priority=["单位净值"])
    cumulative_ok = cumulative.get("week_return") is not None and not has_price_discontinuity(nav_records, value_priority=["累计净值", "复权净值"])
    if cumulative_ok:
        flags = ["份额折算"] if unit_split else []
        return {**cumulative, "return_basis": "ETF累计净值", "nav_basis": "累计净值", "split_detected": unit_split, "quality_flags": flags}
    if unit.get("week_return") is not None and not unit_split:
        return {**unit, "return_basis": "ETF单位净值（无折算）", "nav_basis": "单位净值", "split_detected": False, "quality_flags": []}
    if unit_split:
        rejected_flags.append("ETF单位净值异常断点")

    snapshots = (etf_data.get("iopv_snapshots") or {}).get(code) or []
    iopv = series_metrics(snapshots, week)
    if iopv.get("week_return") is not None and not has_price_discontinuity(snapshots):
        return {**iopv, "return_basis": "IOPV同期快照", "quality_flags": []}
    if iopv.get("week_return") is not None:
        rejected_flags.append("IOPV异常断点")

    feeder = (etf_data.get("feeder_nav") or {}).get(code) or {}
    feeder_records = feeder.get("records") or []
    feeder_metrics = series_metrics(feeder_records, week, ["累计净值", "单位净值"])
    if feeder_metrics.get("week_return") is not None and not has_price_discontinuity(feeder_records, value_priority=["累计净值", "单位净值"]):
        return {**feeder_metrics, "return_basis": f"联接基金{feeder.get('feeder_code')}累计净值代理", "nav_basis": "联接基金累计净值", "split_detected": False, "quality_flags": ["代理收益"]}
    if feeder_metrics.get("week_return") is not None:
        rejected_flags.append("联接基金净值异常断点")

    raw = history.get("none") or []
    raw_metrics = series_metrics(raw, week, price_priority)
    if raw_metrics.get("week_return") is not None and not has_price_discontinuity(raw, value_priority=price_priority):
        return {**raw_metrics, "return_basis": "未复权价格（已检查断点）", "quality_flags": []}
    sina = (etf_data.get("history_sina") or {}).get(code) or []
    sina_metrics = series_metrics(sina, week, price_priority)
    if sina_metrics.get("week_return") is not None and not has_price_discontinuity(sina, value_priority=price_priority):
        return {**sina_metrics, "return_basis": "新浪历史价格（已检查断点）", "quality_flags": ["备用行情源"]}
    flags = rejected_flags + (["复权口径待确认"] if raw_metrics.get("week_return") is not None or sina_metrics.get("week_return") is not None else [])
    return {"data_status": "insufficient_data", "week_return": None, "one_month": None, "three_month": None, "return_basis": "不可确认", "quality_flags": flags}


def _compound_reported_growth(records: list[dict[str, Any]], week: dict[str, Any]) -> float | None:
    start = parse_day(week.get("start_date"))
    end = parse_day(week.get("end_date"))
    if not start or not end:
        return None
    values = []
    for row in records:
        day = parse_day(row_text(row, ["净值日期", "日期", "date", "trade_date"]))
        growth = row_number(row, ["日增长率", "涨跌幅", "pct_chg"])
        if day and start <= day <= end and growth is not None:
            values.append(growth)
    if not values:
        return None
    result = 1.0
    for value in values:
        result *= 1 + value / 100
    return (result - 1) * 100


def etf_return_evidence(etf_data: dict[str, Any], code: str, week: dict[str, Any]) -> dict[str, Any]:
    result = _select_etf_return_evidence(etf_data, code, week)
    flags = [flag for flag in result.get("quality_flags") or [] if flag != "份额折算"]
    corporate_actions = ["份额折算"] if result.get("split_detected") or "份额折算" in (result.get("quality_flags") or []) else []
    result.update({
        "quality_flags": flags,
        "corporate_actions": corporate_actions,
        "return_status": "ok" if result.get("week_return") is not None else "insufficient_data",
        "return_confidence": "高" if result.get("week_return") is not None else "低",
        "supports_recommendation": result.get("week_return") is not None,
        "return_crosschecks": [],
    })
    selected = result.get("week_return")
    if selected is None:
        return result

    nav_records = (etf_data.get("nav") or {}).get(code) or []
    history = (etf_data.get("history") or {}).get(code) or {}
    feeder = ((etf_data.get("feeder_nav") or {}).get(code) or {}).get("records") or []
    crosschecks: list[tuple[str, float]] = []
    adjusted = series_metrics(history.get("hfq") or [], week, ["收盘", "close"]).get("week_return")
    cumulative = series_metrics(nav_records, week, ["累计净值", "复权净值"]).get("week_return")
    feeder_value = series_metrics(feeder, week, ["累计净值", "单位净值"]).get("week_return")
    growth_value = _compound_reported_growth(nav_records, week)
    selected_group = "adjusted" if result.get("return_basis") == "后复权价格" else "nav" if str(result.get("return_basis")).startswith("ETF") else "feeder" if str(result.get("return_basis")).startswith("联接基金") else "other"
    for group, label, value in [
        ("adjusted", "复权价格", adjusted),
        ("nav", "ETF累计净值", cumulative),
        ("feeder", "联接基金累计净值", feeder_value),
    ]:
        if group != selected_group and value is not None:
            crosschecks.append((label, float(value)))
    result["return_crosschecks"] = [{"basis": label, "week_return": value, "difference": abs(value - selected)} for label, value in crosschecks]

    exceptional = bool(corporate_actions) or abs(float(selected)) > 15
    conflict_reasons = []
    if growth_value is not None and cumulative is not None and abs(float(growth_value) - float(cumulative)) > 0.5:
        conflict_reasons.append(f"每日增长率复合与累计净值相差{abs(float(growth_value) - float(cumulative)):.2f}个百分点")
    if exceptional and crosschecks:
        conflict_reasons.extend(
            f"{label}与主口径相差{abs(value - selected):.2f}个百分点"
            for label, value in crosschecks if abs(value - selected) > 1
        )
    if conflict_reasons:
        result.update({
            "observed_week_return": selected,
            "week_return": None,
            "return_status": "data_conflict",
            "return_confidence": "低",
            "supports_recommendation": False,
            "quality_flags": sorted(set(flags + ["收益跨源冲突"])),
            "return_conflict_reasons": conflict_reasons,
        })
    elif exceptional and not crosschecks:
        result.update({"return_confidence": "中", "supports_recommendation": False})
    return result


def row_number(row: dict[str, Any], tokens: list[str]) -> float | None:
    key = find_key(row, tokens)
    return parse_number(row.get(key)) if key else None


def row_text(row: dict[str, Any], tokens: list[str]) -> str:
    key = find_key(row, tokens)
    return str(row.get(key) or "") if key else ""


def endpoint_value(records: list[dict[str, Any]], day: dt.date | None, priority: list[str]) -> tuple[dt.date, float] | None:
    if not day:
        return None
    return value_on_or_before(extract_series(records, priority), day)


def analyze_etfs(data: dict[str, Any]) -> list[dict[str, Any]]:
    etf_data = data.get("candidate_etfs") or {}
    spots = spot_map(etf_data.get("spot") or [])
    nav_spots = spot_map(etf_data.get("nav_spot_ths") or [])
    live_by_code = etf_data.get("live_snapshot") or {}
    output = []
    for raw_code in etf_data.get("codes") or []:
        code = str(raw_code).zfill(6)
        spot = spots.get(code, {})
        live = dict(spot)
        live.update(live_by_code.get(code) or {})
        live_price = row_number(live, ["最新价", "price", "trade", "close", "最新"])
        iopv = row_number(live, ["IOPV实时估值", "IOPV", "iopv"])
        published_discount = row_number(live, ["基金折价率", "折价率"])
        live_premium = (live_price / iopv - 1) * 100 if live_price is not None and iopv and iopv > 0 else None
        evidence = etf_return_evidence(etf_data, code, data.get("week") or {})
        flags = list(evidence.pop("quality_flags", []))
        end_day = parse_day((data.get("week") or {}).get("end_date"))
        history = (etf_data.get("history") or {}).get(code) or {}
        raw_history = history.get("none") or []
        sina_history = (etf_data.get("history_sina") or {}).get(code) or []
        eod_history = raw_history or history.get("qfq") or sina_history
        close_record = endpoint_value(eod_history, end_day, ["原始收盘", "收盘", "close"])
        unit_nav = endpoint_value((etf_data.get("nav") or {}).get(code) or [], end_day, ["单位净值"])
        nav_snapshot = nav_spots.get(code, {})
        if not unit_nav and nav_snapshot:
            nav_day = parse_day(row_text(nav_snapshot, ["最新-交易日", "查询日期", "日期"]))
            nav_value = row_number(nav_snapshot, ["最新-单位净值", "当前-单位净值", "单位净值"])
            if nav_day and nav_value is not None and end_day and nav_day <= end_day:
                unit_nav = (nav_day, nav_value)
        closing_premium = None
        if close_record and unit_nav and close_record[0] == unit_nav[0] and unit_nav[1] > 0:
            closing_premium = (close_record[1] / unit_nav[1] - 1) * 100
        spot_day = parse_day(row_text(live, ["更新时间", "trade_time", "日期", "date"]))
        dated_published = published_discount if spot_day and end_day and spot_day == end_day else None
        premium = closing_premium if closing_premium is not None else (-dated_published if dated_published is not None else None)
        premium_basis = "收盘净值溢价" if closing_premium is not None else "同日公布折价率反算" if dated_published is not None else "不可确认"
        premium_as_of = close_record[0].isoformat() if closing_premium is not None and close_record else spot_day.isoformat() if dated_published is not None and spot_day else ""
        if premium is not None and premium >= 2:
            flags.append("追高风险")
        elif premium is not None and premium >= 1:
            flags.append("溢价偏高")
        if live_premium is not None and published_discount is not None and abs(live_premium + published_discount) > 0.2:
            flags.append("折溢价字段不一致")
        end_history_row = next(
            (
                row for row in reversed(eod_history)
                if parse_day(row_text(row, ["日期", "date", "trade_date"])) == end_day
            ),
            {},
        )
        price = close_record[1] if close_record else None
        turnover = row_number(end_history_row, ["成交额", "amount"])
        if turnover is None and spot_day and end_day and spot_day == end_day:
            turnover = row_number(live, ["成交额", "turnover", "amount"])
        if turnover is None:
            flags.append("成交额缺失")
        trade_time_text = row_text(live, ["trade_time", "更新时间", "日期", "date"])
        live_fresh = False
        if trade_time_text:
            try:
                trade_time = dt.datetime.fromisoformat(trade_time_text.replace("Z", "+00:00"))
                now = dt.datetime.now(trade_time.tzinfo) if trade_time.tzinfo else dt.datetime.now()
                live_fresh = dt.timedelta(0) <= now - trade_time <= dt.timedelta(minutes=5)
            except ValueError:
                live_fresh = False
        recommendation_eligible = bool(
            evidence.get("supports_recommendation")
            and evidence.get("week_return") is not None
            and turnover is not None
            and premium is not None
            and premium < 2
        )
        execution_ready = bool(recommendation_eligible and live_fresh and live_premium is not None and live_premium < 2)
        access = (etf_data.get("access") or {}).get(code, {})
        output.append(
            {
                "code": code,
                "name": row_text(live, ["名称", "name"]) or row_text(nav_snapshot, ["基金名称", "名称"]) or ETF_NAMES.get(code) or code,
                "listed_market": access.get("market") or "-",
                "channel": access.get("channel") or "待核验",
                "access_verified_at": access.get("verified_at"),
                "price": price,
                "iopv": iopv,
                "premium_rate": premium,
                "premium_basis": premium_basis,
                "premium_as_of": premium_as_of,
                "published_discount_rate": published_discount,
                "turnover": turnover,
                "updated_at": close_record[0].isoformat() if close_record else str(data.get("as_of") or ""),
                "price_source": (
                    "新浪ETF历史收盘价" if eod_history is sina_history and close_record else
                    "报告期末历史收盘价" if close_record else "不可用"
                ),
                "turnover_source": (
                    "新浪ETF历史成交额" if eod_history is sina_history and turnover is not None else
                    "报告期末历史成交额" if turnover is not None else "不可用"
                ),
                "eod_quality": {
                    "as_of": close_record[0].isoformat() if close_record else None,
                    "close": price,
                    "turnover": turnover,
                    "unit_nav": unit_nav[1] if unit_nav else None,
                    "premium_rate": premium,
                    "premium_basis": premium_basis,
                },
                "live_snapshot": {
                    "trade_time": trade_time_text or None,
                    "price": live_price,
                    "turnover": row_number(live, ["成交额", "turnover", "amount"]),
                    "iopv": iopv,
                    "premium_rate": live_premium,
                    "premium_basis": "实时IOPV溢价" if live_premium is not None else "不可确认",
                    "fresh_within_5m": live_fresh,
                    "price_source": live.get("price_source") or ("东方财富ETF快照" if (etf_data.get("spot_em") or []) else "新浪ETF快照" if spot else "不可用"),
                },
                "recommendation_eligible": recommendation_eligible,
                "execution_ready": execution_ready,
                "execution_note": "当前交易质量已核验" if execution_ready else "执行前需重新核对实时溢价",
                "quality_flags": sorted(set(flags)),
                **evidence,
            }
        )
    return sorted(output, key=lambda item: item.get("premium_rate") if item.get("premium_rate") is not None else 999)


def percentile_scores(rows: list[dict[str, Any]], key: str) -> dict[str, float]:
    valid = sorted([(str(row["code"]), float(row[key])) for row in rows if row.get(key) is not None], key=lambda item: item[1])
    if not valid:
        return {}
    if len(valid) == 1:
        return {valid[0][0]: 50.0}
    return {code: rank / (len(valid) - 1) * 100 for rank, (code, _) in enumerate(valid)}


def sector_confirmation(themes: list[str], sectors: dict[str, Any]) -> tuple[float | None, list[str]]:
    evidence = []
    scores = []
    rows = (sectors.get("industry_return") or []) + (sectors.get("concept_return") or [])
    for row in rows:
        row_themes = set(row.get("exposure_keys") or [])
        row_themes.update(filter(None, [row.get("theme_l1"), row.get("theme_l2")]))
        if row_themes & set(themes):
            flow_points = {"持续流入": 100, "短线脉冲": 65, "分歧": 40, "持续流出": 0, "数据不足": 35}.get(row.get("flow_status"), 35)
            return_points = 100 if (row.get("week_return") or 0) > 0 else 20
            scores.append((return_points + flow_points) / 2)
            evidence.append(f"{row.get('name')} {row.get('week_return'):.2f}%/{row.get('flow_status')}")
    if rows:
        return (max(scores) if scores else 25.0), evidence or ["未进入板块收益Top10，暂无资金确认"]
    return None, evidence


def style_alignment(themes: list[str], styles: list[dict[str, Any]]) -> float | None:
    valid = [row for row in styles if row.get("week_return") is not None]
    if not valid:
        return None
    growth_names = {"国证成长", "创业板指", "科创50"}
    value_names = {"国证价值", "中证红利", "上证50"}
    relevant = growth_names if any(theme in HIGH_VOLATILITY_THEMES for theme in themes) else value_names if "红利价值" in themes else set()
    if not relevant:
        return 50.0
    matched = [row["week_return"] for row in valid if row["name"] in relevant]
    return 80.0 if matched and max(matched) > 0 else 30.0


def product_quality(row: dict[str, Any]) -> float | None:
    if row.get("candidate_kind") == "fund":
        if not row.get("product_evidence_available"):
            return None
        score = 75.0
        flags = set(row.get("quality_flags") or [])
        if "主动迷你规模" in flags:
            score -= 45
        elif "主动小规模" in flags:
            score -= 20
        if "换手率高" in flags:
            score -= 25
        return max(0.0, score)
    if "premium_rate" not in row:
        return 70.0
    if row.get("turnover") is None or row.get("week_return") is None:
        return None
    flags = set(row.get("quality_flags") or [])
    if "追高风险" in flags or "复权口径待确认" in flags:
        return 0.0
    score = 100.0
    if "溢价偏高" in flags:
        score -= 30
    if row.get("turnover", 0) < 10_000_000:
        score -= 30
    return max(0.0, score)


def portfolio_fit(themes: list[str], portfolio_theme_counts: Counter[str]) -> float:
    recognized = [theme for theme in themes if theme in THEME_KEYWORDS]
    if not recognized:
        return 40.0
    counts = [portfolio_theme_counts.get(theme, 0) for theme in recognized]
    if min(counts) == 0:
        return 100.0
    if max(counts) >= 3:
        return 20.0
    return 65.0


def score_rows(rows: list[dict[str, Any]], sectors: dict[str, Any], styles: list[dict[str, Any]], portfolio: dict[str, Any]) -> None:
    week_percentile = percentile_scores(rows, "week_return")
    month_percentile = percentile_scores(rows, "one_month")
    theme_counts, _, _ = portfolio_theme_context(portfolio)
    weights = {"weekly_performance": 30, "one_month_trend": 20, "sector_confirmation": 20, "style_alignment": 10, "trading_quality": 10, "portfolio_fit": 10}
    for row in rows:
        sector_score, sector_evidence = sector_confirmation(row.get("themes") or [], sectors)
        components = {
            "weekly_performance": week_percentile.get(str(row["code"])),
            "one_month_trend": month_percentile.get(str(row["code"])),
            "sector_confirmation": sector_score,
            "style_alignment": style_alignment(row.get("themes") or [], styles),
            "trading_quality": product_quality(row),
            "portfolio_fit": portfolio_fit(row.get("themes") or [], theme_counts),
        }
        available_weight = sum(weights[key] for key, value in components.items() if value is not None)
        core_available = all(components[key] is not None for key in ["weekly_performance", "one_month_trend", "sector_confirmation"])
        total = (
            sum(components[key] * weights[key] for key in weights if components[key] is not None) / available_weight
            if available_weight >= 70 and core_available else None
        )
        row["score_components"] = components
        row["score_coverage"] = available_weight / 100
        row["weekly_score"] = round(total, 2) if total is not None else None
        row["score_confidence"] = "高" if available_weight == 100 else "中" if total is not None else "低"
        row["score_evidence"] = sector_evidence
        missing = [SCORE_COMPONENT_LABELS[key] for key, value in components.items() if value is None]
        row["score_missing_components"] = missing
        row["score_unavailable_reason"] = None if total is not None else f"未评分：缺少{'、'.join(missing) or '核心评分证据'}"


def replacement_decision_summary(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "证据不足，暂不生成替换建议；现阶段以观察和控制同质化为主。"
    execution_ready_count = sum(bool(row.get("execution_ready")) for row in rows)
    summary = f"已形成{len(rows)}组满足报告截止日证据门槛的替换观察。"
    if execution_ready_count:
        summary += f"其中{execution_ready_count}组已通过当前交易质量核验，可按3%至5%首期上限执行。"
    if execution_ready_count < len(rows):
        summary += f"其余{len(rows) - execution_ready_count}组需在执行前复核实时价格和溢价，不给出即时买入比例。"
    return summary


def has_matched_sector_evidence(row: dict[str, Any]) -> bool:
    """Only a named sector match qualifies; a Top10 non-match is negative evidence."""
    return any("%/" in str(item) for item in row.get("score_evidence") or [])


def compare_and_recommend(
    portfolio: dict[str, Any],
    sectors: dict[str, Any],
    styles: list[dict[str, Any]],
    etfs: list[dict[str, Any]],
    fund_candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    current = portfolio.get("funds") or []
    for row in etfs:
        row["themes"] = infer_themes(row.get("name"))
    held_codes = {row["code"] for row in current}
    etf_codes = {row.get("code") for row in etfs}
    fund_candidates = [
        row for row in fund_candidates
        if row.get("code") not in held_codes and row.get("code") not in etf_codes
    ]
    scoring_pool = current + etfs + fund_candidates
    score_rows(scoring_pool, sectors, styles, portfolio)

    strong_rows = [row for row in ((sectors.get("industry_return") or [])[:5] + (sectors.get("concept_return") or [])[:5]) if (row.get("week_return") or 0) > 0]
    strong_themes = Counter(theme for row in strong_rows for theme in row.get("exposure_keys") or [])
    portfolio_themes, _, _ = portfolio_theme_context(portfolio)
    covered = sorted(set(strong_themes) & set(portfolio_themes), key=lambda value: strong_themes[value], reverse=True)
    missing = sorted(set(strong_themes) - set(portfolio_themes), key=lambda value: strong_themes[value], reverse=True)

    current_rows = []
    for row in current:
        weak = row.get("weekly_score") is not None and row["weekly_score"] < 45
        negative_trend = row.get("one_month") is not None and row["one_month"] <= -3
        overlap = sorted(set(row.get("themes") or []) & set(strong_themes))
        if weak:
            action = "替换候选"
            reason = "有效周度综合分低于45，进入替换观察池"
        elif negative_trend:
            action = "观察"
            reason = "近1月趋势偏弱，但综合评分证据不足，暂不升级为替换候选"
        elif overlap:
            action = "保留但不加"
            reason = "已覆盖强势方向，新增资金需防止重复暴露"
        else:
            action = "观察"
            reason = "缺少与强势板块一致的充分证据"
        row.update({"decision_action": action, "decision_reason": reason, "strong_theme_overlap": overlap})
        current_rows.append(row)

    weak_current = sorted(
        [row for row in current_rows if row.get("decision_action") == "替换候选" and row.get("weekly_score") is not None],
        key=lambda row: row["weekly_score"],
    )
    eligible = []
    for candidate in etfs + fund_candidates:
        flags = set(candidate.get("quality_flags") or [])
        evidence_ok = has_matched_sector_evidence(candidate)
        common_ok = candidate.get("weekly_score") is not None and candidate.get("week_return") is not None and evidence_ok
        if candidate.get("candidate_kind") == "fund":
            product_ok = bool(candidate.get("product_evidence_available")) and bool(candidate.get("theme_evidence_available")) and "主动迷你规模" not in flags
        else:
            product_ok = bool(candidate.get("recommendation_eligible"))
        if common_ok and product_ok:
            eligible.append(candidate)
    eligible.sort(key=lambda row: row["weekly_score"], reverse=True)

    top3 = []
    used_candidates = set()
    for weak in weak_current:
        candidate = next(
            (
                row
                for row in eligible
                if row["code"] not in used_candidates and row["weekly_score"] - weak["weekly_score"] >= 5
            ),
            None,
        )
        if not candidate:
            continue
        used_candidates.add(candidate["code"])
        gap = round(candidate["weekly_score"] - weak["weekly_score"], 2)
        execution_ready = candidate.get("candidate_kind") == "fund" or bool(candidate.get("execution_ready"))
        first_step = (0.03 if gap < 15 else 0.05) if execution_ready else None
        top3.append(
            {
                "replace_code": weak["code"],
                "replace_name": weak["name"],
                "replace_week_return": weak.get("week_return"),
                "replace_score": weak.get("weekly_score"),
                "candidate_code": candidate["code"],
                "candidate_name": candidate["name"],
                "candidate_kind": candidate.get("candidate_kind") or "etf",
                "candidate_week_return": candidate.get("week_return"),
                "candidate_score": candidate.get("weekly_score"),
                "score_gap": gap,
                "candidate_premium_rate": candidate.get("premium_rate"),
                "candidate_turnover": candidate.get("turnover"),
                "candidate_return_basis": candidate.get("return_basis"),
                "candidate_score_components": candidate.get("score_components"),
                "evidence": candidate.get("score_evidence"),
                "risk_flags": candidate.get("quality_flags"),
                "recommendation_eligible": True,
                "execution_ready": execution_ready,
                "action": "小幅分批" if execution_ready else "替换观察，执行前复核实时溢价",
                "suggested_first_step_weight": first_step,
                "reason": (
                    f"候选综合分高出 {gap:.2f} 分，且收盘交易质量与板块证据完整；实时溢价已通过5分钟新鲜度门控。"
                    if execution_ready else
                    f"候选综合分高出 {gap:.2f} 分，收盘证据完整；缺少5分钟内实时溢价，仅进入替换观察。"
                ),
            }
        )
        if len(top3) >= 3:
            break

    unscored_current = [row for row in current_rows if row.get("weekly_score") is None]
    high_premium_candidates = [
        row for row in etfs
        if row.get("premium_rate") is not None and float(row["premium_rate"]) >= 2
    ]
    unknown_premium_candidates = [row for row in etfs if row.get("premium_rate") is None]
    execution_pending_candidates = [row for row in etfs if row.get("recommendation_eligible") and not row.get("execution_ready")]
    blockers = []
    if not weak_current:
        if unscored_current:
            blockers.append(f"{len(unscored_current)}只当前基金缺少有效综合分，无法确认应被替换的弱势持仓")
        else:
            blockers.append("当前持仓没有综合分低于45分的明确替换对象")
    if not eligible:
        blockers.append("没有候选同时通过收益、板块证据、产品质量和交易质量门槛")
    if high_premium_candidates:
        blockers.append(f"{len(high_premium_candidates)}只候选ETF溢价达到或超过2%，只能观察，不能给出分批买入建议")
    if unknown_premium_candidates:
        blockers.append(f"{len(unknown_premium_candidates)}只候选ETF报告截止日折溢价尚未确认，不能进入替换观察")
    if execution_pending_candidates:
        blockers.append(f"{len(execution_pending_candidates)}只ETF收盘证据已完整，但缺少5分钟内实时溢价，执行前需复核")
    if not sectors.get("concept_return"):
        blockers.append("概念板块缺少真实周收益证据")
    if len(top3) < 3:
        blockers.append(f"仅有{len(top3)}组候选满足至少5分分差及全部风险门槛")

    industry_leaders = [
        row for row in sectors.get("industry_return") or []
        if (row.get("week_return") or 0) > 0
    ]
    return_leaders = sorted(industry_leaders, key=lambda row: row.get("week_return") or -999, reverse=True)[:6]
    if not return_leaders:
        return_leaders = sorted(strong_rows, key=lambda row: row.get("week_return") or -999, reverse=True)[:6]
    confirmed = [row for row in return_leaders if row.get("flow_status") == "持续流入"]
    unconfirmed = [row for row in return_leaders if row.get("flow_status") != "持续流入"]
    covered_rows = [row for row in return_leaders if row.get("current_coverage") != "缺失"]
    missing_rows = [row for row in return_leaders if row.get("current_coverage") == "缺失"]

    def sector_names(rows: list[dict[str, Any]]) -> str:
        return "、".join(str(row.get("name")) for row in rows if row.get("name"))

    leader_text = sector_names(return_leaders) or "本周没有取得可靠正收益证据的行业"
    confirmed_text = sector_names(confirmed)
    unconfirmed_text = sector_names(unconfirmed)
    covered_text = sector_names(covered_rows)
    missing_text = sector_names(missing_rows)
    overlap_text = "、".join(theme for theme, count in portfolio_themes.items() if count >= 3)
    market_summary = f"{leader_text}等方向本周收益领先。" if return_leaders else f"{leader_text}。"
    confirmed_evidence = "；".join(
        f"{row.get('name')}：{row.get('flow_status_reason')}" for row in confirmed
    )
    flow_summary = (
        f"其中{confirmed_text}获得资金持续性确认（{confirmed_evidence}）。" if confirmed_text
        else "当前领涨方向尚未获得至少两个周期同向且单日同向的资金流确认。"
    )
    if unconfirmed_text:
        flow_summary += f" {unconfirmed_text}属于收益领先但资金待确认，不能仅按单周涨幅追高。"
    coverage_parts = []
    if covered_text:
        coverage_parts.append(f"组合对{covered_text}存在直接或主题映射覆盖")
    if missing_text:
        coverage_parts.append(f"对{missing_text}未发现可验证的直接主题覆盖")
    coverage_summary = "；".join(coverage_parts) + "。" if coverage_parts else "当前没有足够行业证据判断组合覆盖差异。"
    overlap_summary = f"组合最明显的重复暴露是{overlap_text}。" if overlap_text else "暂未识别出三只及以上基金共同暴露的重复主题。"
    decision_summary = replacement_decision_summary(top3)
    weekly_conclusion = {
        "market_summary": market_summary,
        "flow_summary": flow_summary,
        "coverage_summary": coverage_summary,
        "overlap_summary": overlap_summary,
        "decision_summary": decision_summary,
        "confidence_note": "只有正收益且资金持续流入的行业才视为已确认方向；其余仅作观察信号。",
        "return_leaders": return_leaders,
        "confirmed_leaders": confirmed,
        "unconfirmed_leaders": unconfirmed,
    }

    return {
        "covered_themes": covered,
        "missing_themes": missing,
        "strong_sectors_covered": [row for row in strong_rows if row.get("current_coverage") != "缺失"],
        "strong_sectors_missing": [row for row in strong_rows if row.get("current_coverage") == "缺失"],
        "current_vs_weekly": current_rows,
        "overlap_risk": [theme for theme, count in portfolio_themes.items() if count >= 3],
        "replacement_top3": top3,
        "replacement_status": "ok" if len(top3) == 3 else "insufficient_evidence",
        "replacement_status_display": "已形成三组证据完整的替换观察" if len(top3) == 3 else "证据不足，暂不生成或补足替换建议",
        "replacement_blockers": list(dict.fromkeys(blockers)),
        "replacement_note": None if len(top3) == 3 else f"仅有 {len(top3)} 组候选满足收益、分差、板块证据、流动性和溢价门槛，未补齐伪建议。",
        "weekly_conclusion": weekly_conclusion,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--holdings", required=True, type=Path)
    parser.add_argument("--weekly-data", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--history-weeks", type=int, default=3)
    parser.add_argument("--cache-root", type=Path, default=Path("work/cache/fund-rotation"))
    parser.add_argument("--llm-evidence-output", type=Path)
    args = parser.parse_args()

    data = load_json(args.weekly_data)
    raw_holdings = load_json(args.holdings)
    holdings = normalize_holdings(raw_holdings)
    portfolio_meta = holdings_metadata(raw_holdings)
    warnings = list(dict.fromkeys(data.get("warnings") or []))
    input_hash = holdings_hash(holdings)
    if data.get("holdings_hash") and data.get("holdings_hash") != input_hash:
        warnings.append(f"持仓文件已更新：分析使用 --holdings 快照 {input_hash}，覆盖采集快照 {data.get('holdings_hash')}")
    portfolio = analyze_funds(data, holdings, portfolio_meta)
    warnings.extend(portfolio.pop("warnings", []))
    styles = analyze_style(data)
    margin_leverage = analyze_margin_leverage(
        ((data.get("market") or {}).get("margin_raw") or {}),
        ((data.get("market") or {}).get("style_indexes") or {}),
        cutoff=(data.get("week") or {}).get("end_date") or dt.date.today().isoformat(),
        concentration=(((data.get("market") or {}).get("margin_raw") or {}).get("concentration") or {}),
    )
    calibration_path = args.cache_root / "margin_calibration_v1.json"
    if calibration_path.exists():
        try:
            calibration = load_json(calibration_path)
            calibration_end = str(calibration.get("end_date") or "")[:10]
            report_end = str((data.get("week") or {}).get("end_date") or "")[:10]
            if calibration.get("model_version") == margin_leverage.get("model_version") and calibration.get("evidence_hash") and calibration_end and calibration_end <= report_end:
                margin_leverage["calibration"] = calibration
            else:
                margin_leverage.setdefault("data_quality", []).append("历史校准文件版本、证据哈希或截止日不符合本报告，未用于风险统计")
        except (OSError, ValueError, TypeError):
            margin_leverage.setdefault("data_quality", []).append("历史校准文件无法读取，未用于风险统计")
    sectors = analyze_sectors(data, portfolio)
    etfs = analyze_etfs(data)
    weekly_top = top_weekly_funds(data)
    comparison = compare_and_recommend(portfolio, sectors, styles, etfs, weekly_top)
    analysis_notes = []
    if not sectors.get("industry_return") or not sectors.get("concept_return"):
        analysis_notes.append("板块周收益数据集部分不可用；报告仅展示有真实周期证据的榜单，并披露实际样本范围。")
    if margin_leverage.get("status") != "complete":
        normalization = margin_leverage.get("normalization") or {}
        if normalization.get("financing_to_float_cap") is not None and normalization.get("financing_buy_to_turnover") is not None:
            analysis_notes.append("两融当前余额、同日杠杆密度和交易强度已展示；长期市值/成交额历史不足时不发布滚动分位、热度或完整压力分，且不影响基金评分和调仓建议。")
        else:
            analysis_notes.append("两融模块缺少同日市场规模、成交额或足够历史样本；保留可确认的绝对余额并降级展示，不影响基金评分和调仓建议。")

    dataset_quality = data.get("dataset_status") or []
    recovered_count = sum(row.get("status") == "fallback_used" for row in dataset_quality)
    unresolved_count = sum(
        row.get("requirement", "required") == "required" and row.get("status") in {"failed", "partial"}
        for row in dataset_quality
    )
    optional_unavailable_count = sum(
        row.get("requirement") == "optional" and row.get("status") in {"failed", "partial", "optional_unavailable"}
        for row in dataset_quality
    )

    payload = {
        "schema_version": SCHEMA_VERSION,
        "data_revision": DATA_REVISION,
        "report_format_version": REPORT_FORMAT_VERSION,
        "report_contract": {
            "format_version": REPORT_FORMAT_VERSION,
            "mandatory_sections": MANDATORY_SECTION_ORDER,
            "render_targets": ["markdown", "html"],
        },
        "as_of": data.get("as_of"),
        "source": data.get("source"),
        "mode": data.get("mode"),
        "week": data.get("week"),
        "holdings_hash": input_hash,
        "portfolio": portfolio,
        "market": {
            "style_indexes": styles,
            "sector_top10": sectors,
            "weekly_top_funds": weekly_top,
            "margin_leverage": margin_leverage,
        },
        "candidate_etfs": etfs,
        "comparison": comparison,
        "warnings": list(dict.fromkeys(warnings)),
        "analysis_notes": analysis_notes,
        "data_quality": dataset_quality,
        "source_audit": data.get("source_status") or [],
        "provider_policy": data.get("provider_policy") or "akshare-only",
        "provider_route": data.get("provider_route") or {},
        "quality_summary": {
            "recovered_datasets": recovered_count,
            "unresolved_datasets": unresolved_count,
            "optional_unavailable_datasets": optional_unavailable_count,
        },
        "constraints": {"single_fund_cap": 0.25, "single_theme_cap": 0.40, "high_volatility_adjustment_cap": 0.10},
        "disclaimer": "本报告仅用于基金级别复盘与组合分析，不构成自动交易指令或收益承诺。",
    }
    data.setdefault("cache", {})["database"] = str(args.cache_root / "cache.sqlite3")
    three_week, evidence_bundle, deterministic_synthesis = build_three_week_analysis(data, payload, args.history_weeks)
    three_week_margin = build_three_week_margin(
        margin_leverage,
        three_week.get("periods") or [],
        ((data.get("market") or {}).get("margin_raw") or {}),
        ((data.get("market") or {}).get("style_indexes") or {}),
    )
    three_week["margin_leverage"] = three_week_margin
    margin_evidence = {}
    for metric, value, unit in [
        ("margin_balance", (margin_leverage.get("current") or {}).get("margin_balance"), "元"),
        ("financing_to_float_cap", (margin_leverage.get("normalization") or {}).get("financing_to_float_cap"), "%"),
        ("financing_buy_to_turnover", (margin_leverage.get("normalization") or {}).get("financing_buy_to_turnover"), "%"),
        ("heat_score", (margin_leverage.get("heat") or {}).get("score"), "分"),
        ("deleveraging_pressure", (margin_leverage.get("deleveraging_pressure") or {}).get("score"), "分"),
    ]:
        evidence_id = f"margin:SSE+SZSE:W0:{metric}"
        margin_evidence[evidence_id] = {
            "id": evidence_id, "entity_id": "SSE+SZSE", "entity_name": "沪深两融",
            "period": "W0", "metric": metric, "value": value, "unit": unit,
            "source": "交易所汇总/经验证备用源", "source_date": margin_leverage.get("as_of"),
        }
    period_dates = {row.get("period_id"): row.get("end_date") for row in three_week.get("periods") or []}
    for row in three_week_margin.get("periods") or []:
        for metric, unit in (
            ("financing_balance_change", "%"),
            ("average_financing_intensity", "%"),
            ("heat_score", "分"),
            ("deleveraging_pressure_score", "分"),
        ):
            evidence_id = f"margin:SSE+SZSE:{row.get('period_id')}:{metric}"
            margin_evidence[evidence_id] = {
                "id": evidence_id, "entity_id": "SSE+SZSE", "entity_name": "沪深两融",
                "period": row.get("period_id"), "metric": metric, "value": row.get(metric), "unit": unit,
                "source": "交易所汇总/经验证备用源", "source_date": period_dates.get(row.get("period_id")),
            }
    for index, row in enumerate(margin_leverage.get("historical_comparisons") or []):
        for metric, unit, source_date in (
            ("peak_margin_balance", "元", row.get("peak_date")),
            ("current_vs_peak_pct", "%", margin_leverage.get("as_of")),
            ("peak_financing_to_float_cap", "%", row.get("peak_financing_to_float_cap_date")),
            ("peak_financing_buy_to_turnover", "%", row.get("peak_financing_buy_to_turnover_date")),
        ):
            evidence_id = f"margin:SSE+SZSE:H{index}:{metric}"
            margin_evidence[evidence_id] = {
                "id": evidence_id, "entity_id": "SSE+SZSE", "entity_name": "沪深两融",
                "period": row.get("label"), "metric": metric, "value": row.get(metric), "unit": unit,
                "source": "同口径历史序列", "source_date": source_date,
            }
    evidence_bundle["evidence"].update(margin_evidence)
    evidence_bundle["evidence_hash"] = stable_hash(evidence_bundle["evidence"])
    three_week["evidence_index"] = evidence_bundle["evidence"]
    deterministic_synthesis["evidence_hash"] = evidence_bundle["evidence_hash"]
    payload["three_week_analysis"] = three_week
    payload["llm_synthesis"] = deterministic_synthesis
    payload["llm_evidence_hash"] = evidence_bundle["evidence_hash"]
    payload["cache"] = data.get("cache") or {}
    payload["delivery_readiness"] = delivery_readiness(sectors, three_week, dataset_quality)
    payload["quality_summary"]["delivery_status"] = payload["delivery_readiness"]["status"]
    payload["quality_summary"]["core_sections_complete"] = all(
        payload["delivery_readiness"]["core_requirements"].values()
    )
    if args.llm_evidence_output:
        write_json(args.llm_evidence_output, evidence_bundle)
    write_json(args.output, payload)


if __name__ == "__main__":
    main()
