#!/usr/bin/env python3
"""Normalize and analyze A-share margin-financing leverage evidence."""

from __future__ import annotations

import datetime as dt
import math
from statistics import median
from typing import Any

from data_access import parse_number


MODEL_VERSION = "margin-leverage-v1"
COMPARABLE_START = "2014-09-22"
TRADING_DAYS_5Y = 1250
MIN_PERCENTILE_SAMPLE = 500

POLICY_EVENTS = [
    {
        "date": "2014-09-22",
        "label": "两融统计口径调整",
        "description": "交易总量开始包含调出标的证券名单后的存量余额。",
        "impact": "comparison_boundary",
    },
    {
        "date": "2015-11-23",
        "label": "融资保证金比例调整",
        "description": "新开融资合约杠杆约束提高，前后增速需结合制度变化解释。",
        "impact": "policy_regime",
    },
    {
        "date": "2024-07-11",
        "label": "转融券暂停及融券保证金调整",
        "description": "融券余额可比性下降，评分使用融资余额而非总两融余额。",
        "impact": "lending_regime",
    },
    {
        "date": "2026-01-19",
        "label": "融资保证金比例调整",
        "description": "新开融资合约最低保证金比例变化，作为解释节点保留。",
        "impact": "policy_regime",
    },
]


def date_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)[:10].replace("/", "-")
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    try:
        return dt.date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def _amount(value: Any, multiplier: float) -> float | None:
    number = parse_number(value)
    return number * multiplier if number is not None else None


def normalize_margin_rows(
    rows: list[dict[str, Any]],
    exchange: str,
    provider: str,
    *,
    unit: str = "元",
    cutoff: str | None = None,
) -> list[dict[str, Any]]:
    """Normalize Tushare or AkShare aggregate margin rows to yuan."""
    multiplier = 100_000_000 if unit == "亿元" else 1
    output: dict[str, dict[str, Any]] = {}
    for row in rows:
        day = date_text(row.get("trade_date") or row.get("日期"))
        if not day or (cutoff and day > cutoff):
            continue
        financing = _amount(row.get("rzye", row.get("融资余额")), multiplier)
        financing_buy = _amount(row.get("rzmre", row.get("融资买入额")), multiplier)
        financing_repay = _amount(row.get("rzche", row.get("融资偿还额")), multiplier)
        lending = _amount(row.get("rqye", row.get("融券余额")), multiplier)
        total = _amount(row.get("rzrqye", row.get("融资融券余额")), multiplier)
        if total is None and financing is not None and lending is not None:
            total = financing + lending
        if financing is None and total is None:
            continue
        output[day] = {
            "trade_date": day,
            "exchange": exchange,
            "financing_balance": financing,
            "financing_buy": financing_buy,
            "financing_repay": financing_repay,
            "lending_balance": lending,
            "margin_balance": total,
            "unit": "元",
            "provider": provider,
        }
    return [output[day] for day in sorted(output)]


def normalize_daily_info_rows(
    rows: list[dict[str, Any]],
    exchange: str,
    provider: str,
    *,
    cutoff: str | None = None,
) -> list[dict[str, Any]]:
    """Normalize Tushare daily_info market aggregates from 亿元 to yuan."""
    wanted = "SH_A" if exchange == "SSE" else "SZ_MARKET"
    output: dict[str, dict[str, Any]] = {}
    for row in rows:
        if str(row.get("ts_code") or "") != wanted:
            continue
        day = date_text(row.get("trade_date"))
        if not day or (cutoff and day > cutoff):
            continue
        float_mv = _amount(row.get("float_mv"), 100_000_000)
        turnover = _amount(row.get("amount"), 100_000_000)
        if float_mv is None and turnover is None:
            continue
        output[day] = {
            "trade_date": day,
            "exchange": exchange,
            "float_market_cap": float_mv,
            "market_turnover": turnover,
            "unit": "元",
            "provider": provider,
            "market_scope": wanted,
        }
    return [output[day] for day in sorted(output)]


def normalize_exchange_market_snapshot(
    rows: list[dict[str, Any]], exchange: str, trade_date: str, provider: str
) -> list[dict[str, Any]]:
    """Normalize current exchange summaries with explicit source-specific units."""
    if exchange == "SSE":
        by_metric = {str(row.get("单日情况") or ""): row for row in rows}
        # Keep the denominator on an A-share basis. The exchange's aggregate
        # "股票" column also includes the very small B-share market.
        def sse_a_share_value(metric: str) -> Any:
            row = by_metric.get(metric) or {}
            parts = [parse_number(row.get("主板A")), parse_number(row.get("科创板"))]
            if all(value is not None for value in parts):
                return sum(float(value) for value in parts if value is not None)
            return row.get("股票")

        # SSE public summary is expressed in 亿元.
        float_mv = _amount(sse_a_share_value("流通市值"), 100_000_000)
        turnover = _amount(sse_a_share_value("成交金额"), 100_000_000)
        scope = "SSE A股（主板A+科创板）"
    else:
        a_share_names = {"主板A股", "创业板A股", "中小板", "创业板"}
        selected_rows = [
            row for row in rows
            if str(row.get("证券类别") or "").strip() in a_share_names
        ]
        selected = next((row for row in rows if str(row.get("证券类别") or "").strip() == "股票"), {})
        # SZSE public summary returns raw yuan for these fields.
        float_values = [parse_number(row.get("流通市值")) for row in selected_rows]
        turnover_values = [parse_number(row.get("成交金额")) for row in selected_rows]
        float_mv = (
            sum(float(value) for value in float_values if value is not None)
            if selected_rows and all(value is not None for value in float_values)
            else _amount(selected.get("流通市值"), 1)
        )
        turnover = (
            sum(float(value) for value in turnover_values if value is not None)
            if selected_rows and all(value is not None for value in turnover_values)
            else _amount(selected.get("成交金额"), 1)
        )
        scope = "SZSE A股（主板A+创业板）"
    if float_mv is None and turnover is None:
        return []
    return [{
        "trade_date": trade_date,
        "exchange": exchange,
        "float_market_cap": float_mv,
        "market_turnover": turnover,
        "unit": "元",
        "provider": provider,
        "market_scope": scope,
    }]


def normalize_sse_market_api_rows(
    rows: list[dict[str, Any]], trade_date: str, provider: str
) -> list[dict[str, Any]]:
    """Normalize raw SSE daily-overview JSON across old and current schemas."""
    a_share_rows = [row for row in rows if str(row.get("PRODUCT_CODE") or "") in {"01", "03"}]
    if not a_share_rows:
        a_share_rows = [row for row in rows if str(row.get("PRODUCT_CODE") or "") == "17"]
    float_values = [parse_number(row.get("NEGO_VALUE")) for row in a_share_rows]
    turnover_values = [parse_number(row.get("TRADE_AMT")) for row in a_share_rows]
    if not a_share_rows or not all(value is not None for value in float_values + turnover_values):
        return []
    return [{
        "trade_date": trade_date,
        "exchange": "SSE",
        "float_market_cap": sum(float(value) for value in float_values if value is not None) * 100_000_000,
        "market_turnover": sum(float(value) for value in turnover_values if value is not None) * 100_000_000,
        "unit": "元",
        "provider": provider,
        "market_scope": "SSE A股（主板A+科创板）",
    }]


def _sum_values(rows: list[dict[str, Any]], field: str) -> float | None:
    values = [parse_number(row.get(field)) for row in rows]
    return sum(value for value in values if value is not None) if all(value is not None for value in values) and values else None


def combine_exchanges(
    exchange_rows: dict[str, list[dict[str, Any]]],
    fields: tuple[str, ...],
    *,
    required: tuple[str, ...] = ("SSE", "SZSE"),
) -> list[dict[str, Any]]:
    by_exchange = {
        exchange: {row["trade_date"]: row for row in rows if row.get("trade_date")}
        for exchange, rows in exchange_rows.items()
    }
    common = set.intersection(*(set(by_exchange.get(exchange, {})) for exchange in required)) if required else set()
    output = []
    for day in sorted(common):
        selected = [by_exchange[exchange][day] for exchange in required]
        combined = {"trade_date": day, "scope": "+".join(required), "unit": "元"}
        for field in fields:
            combined[field] = _sum_values(selected, field)
        output.append(combined)
    return output


def validate_margin_identity(rows: list[dict[str, Any]], tolerance: float = 0.001) -> list[str]:
    errors = []
    for row in rows:
        financing = parse_number(row.get("financing_balance"))
        lending = parse_number(row.get("lending_balance"))
        total = parse_number(row.get("margin_balance"))
        if None in {financing, lending, total} or total == 0:
            continue
        if abs((financing + lending) / total - 1) > tolerance:
            errors.append(str(row.get("trade_date")))
    return errors


def _percent_change(rows: list[dict[str, Any]], field: str, sessions: int) -> float | None:
    if len(rows) <= sessions:
        return None
    current = parse_number(rows[-1].get(field))
    previous = parse_number(rows[-sessions - 1].get(field))
    if current is None or previous in {None, 0}:
        return None
    return (current / previous - 1) * 100


def percentile_rank(values: list[float], current: float, *, minimum: int = MIN_PERCENTILE_SAMPLE) -> float | None:
    clean = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    if len(clean) < minimum:
        return None
    return 100 * sum(value <= current for value in clean) / len(clean)


def _rolling_changes(rows: list[dict[str, Any]], field: str, sessions: int) -> list[float]:
    output = []
    for index in range(sessions, len(rows)):
        current = parse_number(rows[index].get(field))
        previous = parse_number(rows[index - sessions].get(field))
        if current is not None and previous not in {None, 0}:
            output.append((current / previous - 1) * 100)
    return output


def _label_heat(score: float | None) -> str:
    if score is None:
        return "数据不足"
    if score < 20:
        return "低杠杆"
    if score < 70:
        return "正常"
    if score < 85:
        return "升温"
    if score < 95:
        return "偏热"
    return "过热"


def _label_pressure(score: float | None) -> str:
    if score is None:
        return "数据不足"
    if score < 30:
        return "平稳"
    if score < 60:
        return "观察"
    if score < 80:
        return "降温"
    if score < 90:
        return "去杠杆"
    return "压力升高"


def _weighted_score(components: dict[str, tuple[float | None, float]], required: set[str]) -> tuple[float | None, float]:
    if any(components.get(name, (None, 0))[0] is None for name in required):
        return None, sum(weight for value, weight in components.values() if value is not None)
    available = [(float(value), weight) for value, weight in components.values() if value is not None]
    coverage = sum(weight for _value, weight in available)
    if coverage < 0.75:
        return None, coverage
    return sum(value * weight for value, weight in available) / coverage, coverage


def _bounded_stress(value: float | None, bad_at: float) -> float | None:
    if value is None:
        return None
    if value >= 0:
        return 0.0
    return min(100.0, abs(value / bad_at) * 100)


def _ratio_series(margin_rows: list[dict[str, Any]], market_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    markets = {row["trade_date"]: row for row in market_rows}
    output = []
    for margin in margin_rows:
        market = markets.get(margin.get("trade_date"))
        if not market:
            continue
        financing = parse_number(margin.get("financing_balance"))
        financing_buy = parse_number(margin.get("financing_buy"))
        float_mv = parse_number(market.get("float_market_cap"))
        turnover = parse_number(market.get("market_turnover"))
        output.append({
            **margin,
            "float_market_cap": float_mv,
            "market_turnover": turnover,
            "financing_to_float_cap": financing / float_mv * 100 if financing is not None and float_mv not in {None, 0} else None,
            "financing_buy_to_turnover": financing_buy / turnover * 100 if financing_buy is not None and turnover not in {None, 0} else None,
        })
    return output


def _latest_percentile(rows: list[dict[str, Any]], field: str) -> float | None:
    current = parse_number(rows[-1].get(field)) if rows else None
    if current is None:
        return None
    values = [parse_number(row.get(field)) for row in rows[:-1][-TRADING_DAYS_5Y:]]
    return percentile_rank([value for value in values if value is not None], current)


def _historical_comparisons(
    rows: list[dict[str, Any]], current: dict[str, Any], broad_index: list[tuple[str, float]] | None = None
) -> list[dict[str, Any]]:
    windows = [
        ("2014–2015杠杆牛市", "2014-09-22", "2015-12-31"),
        ("2020–2021结构性行情", "2020-01-01", "2021-12-31"),
        ("2024年至今", "2024-01-01", current["trade_date"]),
        ("全可比历史", COMPARABLE_START, current["trade_date"]),
    ]
    output = []
    for label, start, end in windows:
        selected = [row for row in rows if start <= row["trade_date"] <= end]
        valid = [row for row in selected if parse_number(row.get("margin_balance")) is not None]
        if not valid:
            continue
        peak = max(valid, key=lambda row: float(row["margin_balance"]))
        current_total = parse_number(current.get("margin_balance"))
        peak_total = parse_number(peak.get("margin_balance"))
        density_rows = [row for row in selected if parse_number(row.get("financing_to_float_cap")) is not None]
        intensity_rows = [row for row in selected if parse_number(row.get("financing_buy_to_turnover")) is not None]
        density_peak = max(density_rows, key=lambda row: row["financing_to_float_cap"], default=None)
        intensity_peak = max(intensity_rows, key=lambda row: row["financing_buy_to_turnover"], default=None)
        growth20 = []
        for index in range(20, len(selected)):
            current_financing = parse_number(selected[index].get("financing_balance"))
            prior_financing = parse_number(selected[index - 20].get("financing_balance"))
            if current_financing is not None and prior_financing not in {None, 0}:
                growth20.append((current_financing / prior_financing - 1) * 100)
        index_map = dict(broad_index or [])
        index_dates = [day for day, _value in broad_index or [] if peak["trade_date"] <= day <= end]
        peak_index = index_map.get(peak["trade_date"])
        forward_returns = [
            (index_map[day] / peak_index - 1) * 100 for day in index_dates[:60]
            if peak_index not in {None, 0} and day in index_map
        ]
        output.append({
            "label": label,
            "start_date": start,
            "end_date": end,
            "peak_date": peak["trade_date"],
            "peak_margin_balance": peak_total,
            "current_vs_peak_pct": (current_total / peak_total - 1) * 100 if current_total is not None and peak_total else None,
            "peak_financing_to_float_cap": density_peak.get("financing_to_float_cap") if density_peak else None,
            "peak_financing_to_float_cap_date": density_peak.get("trade_date") if density_peak else None,
            "peak_financing_buy_to_turnover": intensity_peak.get("financing_buy_to_turnover") if intensity_peak else None,
            "peak_financing_buy_to_turnover_date": intensity_peak.get("trade_date") if intensity_peak else None,
            "fastest_20d_financing_growth": max(growth20, default=None),
            "post_peak_20d_max_drawdown": min(forward_returns[:20]) if len(forward_returns) >= 20 else None,
            "post_peak_60d_max_drawdown": min(forward_returns[:60]) if len(forward_returns) >= 60 else None,
            "sample_days": len(selected),
        })
    return output


def _regime(heat: float | None, pressure: float | None) -> tuple[str, str]:
    if heat is None or pressure is None:
        return "数据不足", "缺少同日市场规模、成交额或足够历史样本，暂不判断杠杆状态。"
    if heat >= 80 and pressure >= 80:
        return "高杠杆去杠杆风险", "杠杆水位较高且下降压力增强；这是风险背景，不代表市场必然立即调整。"
    if heat >= 80 and pressure < 60:
        return "高位但趋势未破坏", "杠杆水位偏高，但尚未形成明确去杠杆压力。"
    if heat < 30 and pressure >= 60:
        return "风险偏好不足", "杠杆较低同时市场承压；低杠杆本身不是买入或见底信号。"
    if heat < 30 and pressure < 60:
        return "低杠杆或现金驱动", "杠杆参与度较低；是否存在上涨空间仍需由盈利、价格和资金轮动确认。"
    if pressure >= 60:
        return "温和降温", "融资活动正在降温，需观察是否与指数下跌形成负反馈。"
    if heat >= 70:
        return "正常扩张", "融资活动升温但尚未进入高压力组合状态。"
    return "过渡状态", "杠杆水位和压力均未给出极端信号。"


def analyze_margin_leverage(
    raw: dict[str, Any],
    style_records: dict[str, list[dict[str, Any]]],
    *,
    cutoff: str,
    concentration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    exchanges = raw.get("exchanges") or {}
    market_exchanges = raw.get("market_daily") or {}
    combined_margin = combine_exchanges(
        {key: value for key, value in exchanges.items() if key in {"SSE", "SZSE"}},
        ("financing_balance", "financing_buy", "financing_repay", "lending_balance", "margin_balance"),
    )
    combined_market = combine_exchanges(
        {key: value for key, value in market_exchanges.items() if key in {"SSE", "SZSE"}},
        ("float_market_cap", "market_turnover"),
    )
    combined_margin = [row for row in combined_margin if COMPARABLE_START <= row["trade_date"] <= cutoff]
    combined_market = [row for row in combined_market if COMPARABLE_START <= row["trade_date"] <= cutoff]
    identity_errors = validate_margin_identity(combined_margin)
    ratio_rows = _ratio_series(combined_margin, combined_market)
    ratio_by_date = {row["trade_date"]: row for row in ratio_rows}
    display_series = []
    for row in combined_margin:
        ratio = ratio_by_date.get(row["trade_date"]) or {}
        display_series.append({**row, **{key: value for key, value in ratio.items() if key not in row}})
    current_margin = combined_margin[-1] if combined_margin else None
    current_ratio = ratio_rows[-1] if ratio_rows and current_margin and ratio_rows[-1]["trade_date"] == current_margin["trade_date"] else None
    status = "partial" if current_margin else "degraded"
    if not current_margin:
        return {
            "model_version": MODEL_VERSION,
            "scope": "SSE+SZSE",
            "as_of": cutoff,
            "status": "degraded",
            "action_policy": "display_only",
            "current": {},
            "normalization": {},
            "history_position": {},
            "trends": {},
            "heat": {"score": None, "label": "数据不足", "coverage": 0},
            "deleveraging_pressure": {"score": None, "label": "数据不足", "coverage": 0},
            "regime": {"label": "数据不足", "explanation": "沪深两融汇总数据不足。"},
            "historical_comparisons": [],
            "concentration": concentration or {},
            "policy_events": POLICY_EVENTS,
            "data_quality": ["沪深任一市场缺失时不发布A股汇总和评分"],
        }

    current = {**current_margin, **({key: value for key, value in current_ratio.items() if key not in current_margin} if current_ratio else {})}
    total_values = [float(row["margin_balance"]) for row in combined_margin if parse_number(row.get("margin_balance")) is not None]
    peak = max(combined_margin, key=lambda row: parse_number(row.get("margin_balance")) or -1)
    trends = {f"change_{sessions}d_pct": _percent_change(combined_margin, "financing_balance", sessions) for sessions in (1, 5, 20, 60)}
    density_pct = _latest_percentile(ratio_rows, "financing_to_float_cap")
    intensity_pct = _latest_percentile(ratio_rows, "financing_buy_to_turnover")
    change20 = trends.get("change_20d_pct")
    change_values = _rolling_changes(combined_margin[-TRADING_DAYS_5Y - 21:-1], "financing_balance", 20)
    growth_pct = percentile_rank(change_values, change20) if change20 is not None else None
    concentration_pct = parse_number((concentration or {}).get("top100_percentile"))
    heat_components = {
        "financing_to_float_cap": (density_pct, 0.40),
        "financing_buy_to_turnover": (intensity_pct, 0.25),
        "financing_balance_20d_growth": (growth_pct, 0.20),
        "top100_concentration": (concentration_pct, 0.15),
    }
    heat_score, heat_coverage = _weighted_score(heat_components, {"financing_to_float_cap", "financing_buy_to_turnover"})

    balance_changes = [
        value for value in (trends.get("change_5d_pct"), trends.get("change_20d_pct"))
        if value is not None
    ]
    balance_stress = _bounded_stress(min(balance_changes), -8) if balance_changes else None
    index_returns = []
    broad_index_series: list[tuple[str, float]] = []
    broad_index_name: str | None = None
    normalized_styles: dict[str, list[tuple[str, float]]] = {}
    for name in ("中证全指", "沪深300", "中证1000"):
        records = style_records.get(name) or []
        normalized = []
        for row in records:
            day = date_text(row.get("日期") or row.get("date"))
            value = parse_number(row.get("收盘") or row.get("close"))
            if day and day <= cutoff and value is not None:
                normalized.append((day, value))
        normalized.sort()
        normalized_styles[name] = normalized
        if name == "中证全指" and normalized:
            broad_index_series = normalized
            broad_index_name = name
        if len(normalized) > 20 and normalized[-21][1]:
            index_returns.append((normalized[-1][1] / normalized[-21][1] - 1) * 100)
    if not broad_index_series:
        for fallback_name in ("沪深300", "中证1000"):
            if normalized_styles.get(fallback_name):
                broad_index_series = normalized_styles[fallback_name]
                broad_index_name = fallback_name
                break
    index_stress = _bounded_stress(median(index_returns) if index_returns else None, -10)
    turnover_values = [parse_number(row.get("market_turnover")) for row in ratio_rows]
    turnover_values = [value for value in turnover_values if value is not None]
    turnover_change = None
    if len(turnover_values) >= 25:
        recent = sum(turnover_values[-5:]) / 5
        prior = sum(turnover_values[-25:-5]) / 20
        turnover_change = (recent / prior - 1) * 100 if prior else None
    turnover_stress = _bounded_stress(turnover_change, -40)
    intensity_values = [parse_number(row.get("financing_buy_to_turnover")) for row in ratio_rows]
    intensity_values = [value for value in intensity_values if value is not None]
    intensity_change = None
    if len(intensity_values) >= 25:
        recent = sum(intensity_values[-5:]) / 5
        prior = sum(intensity_values[-25:-5]) / 20
        intensity_change = (recent / prior - 1) * 100 if prior else None
    intensity_stress = _bounded_stress(intensity_change, -40)
    pressure_components = {
        "financing_balance_decline": (balance_stress, 0.40),
        "broad_index_decline": (index_stress, 0.30),
        "market_turnover_contraction": (turnover_stress, 0.20),
        "financing_intensity_contraction": (intensity_stress, 0.10),
    }
    pressure_score, pressure_coverage = _weighted_score(pressure_components, set())
    status = "complete" if current_ratio and not identity_errors and heat_score is not None and pressure_score is not None else "partial"
    regime_label, regime_explanation = _regime(heat_score, pressure_score)
    if heat_score is not None and pressure_score is not None and density_pct is not None and intensity_pct is not None:
        if density_pct >= 90 and intensity_pct < 70:
            regime_explanation = (
                "融资杠杆密度处于可用历史样本高位，但融资交易强度未同步进入高位，"
                "说明存量杠杆偏高、当期融资买盘并未极端拥挤；"
                f"去杠杆压力为{_label_pressure(pressure_score)}，应继续观察余额下降是否与指数转弱形成负反馈。"
            )
        elif density_pct >= 90 and intensity_pct >= 85:
            regime_explanation = (
                "融资杠杆密度和融资交易强度均处于可用历史样本高位，杠杆水位与活跃度同时偏高；"
                f"去杠杆压力为{_label_pressure(pressure_score)}，这代表拥挤风险背景，不代表市场立即见顶。"
            )
    if current_ratio and (heat_score is None or pressure_score is None):
        regime_label = "历史样本不足"
        regime_explanation = "当前同日杠杆密度与融资交易强度已取得，但长期流通市值、成交额历史或评分分项不足，暂不判断杠杆热度与去杠杆状态。"
    current_total = parse_number(current.get("margin_balance"))
    peak_total = parse_number(peak.get("margin_balance"))
    historical_totals = total_values[:-1]
    all_pct = percentile_rank(historical_totals, current_total, minimum=30) if current_total is not None else None
    rolling_total_pct = percentile_rank(historical_totals[-TRADING_DAYS_5Y:], current_total) if current_total is not None else None
    all_density = [parse_number(row.get("financing_to_float_cap")) for row in ratio_rows[:-1]]
    all_density = [value for value in all_density if value is not None]
    all_intensity = [parse_number(row.get("financing_buy_to_turnover")) for row in ratio_rows[:-1]]
    all_intensity = [value for value in all_intensity if value is not None]
    ratio_start = ratio_rows[0]["trade_date"] if ratio_rows else None
    ratio_end = ratio_rows[-1]["trade_date"] if ratio_rows else None
    full_ratio_history = bool(ratio_start and ratio_start <= "2014-09-30")
    north = (exchanges.get("BSE") or [])[-1] if exchanges.get("BSE") else None
    quality = []
    if identity_errors:
        quality.append(f"{len(identity_errors)}个交易日的两融恒等式偏差超过0.1%")
    if current_ratio is None:
        quality.append("缺少与两融同日的沪深流通市值或成交额，未发布标准化热度")
    elif density_pct is None or intensity_pct is None:
        prior_observations = max(0, len(ratio_rows) - 1)
        quality.append(
            f"同日A股流通市值与成交额共{len(ratio_rows)}个交易日，排除当前日后仅{prior_observations}个历史样本，少于500日门槛；"
            "当前比例可展示，但近5年窗口分位和杠杆热度暂不发布"
        )
    if pressure_score is None:
        missing_pressure = [name for name, (value, _weight) in pressure_components.items() if value is None]
        pressure_labels = {
            "financing_balance_decline": "融资余额下降",
            "broad_index_decline": "宽基指数下跌压力",
            "market_turnover_contraction": "全市场成交额收缩",
            "financing_intensity_contraction": "融资买入强度收缩",
        }
        quality.append(f"去杠杆压力证据覆盖率仅{pressure_coverage * 100:.0f}%，缺少：{'、'.join(pressure_labels.get(name, name) for name in missing_pressure) or '可用分项'}")
    if broad_index_name and broad_index_name != "中证全指":
        quality.append(f"中证全指历史不可用，近60日宽基轨迹与历史回撤暂以{broad_index_name}替代并明确标注")
    if concentration_pct is None:
        quality.append("未取得Top100融资集中度，热度按其余85%权重计算")
    return {
        "model_version": MODEL_VERSION,
        "scope": "SSE+SZSE",
        "as_of": current["trade_date"],
        "status": status,
        "action_policy": "display_only",
        "current": {
            "financing_balance": current.get("financing_balance"),
            "lending_balance": current.get("lending_balance"),
            "margin_balance": current.get("margin_balance"),
            "financing_buy": current.get("financing_buy"),
            "financing_repay": current.get("financing_repay"),
            "financing_net_change": (current.get("financing_buy") - current.get("financing_repay")) if current.get("financing_buy") is not None and current.get("financing_repay") is not None else None,
            "exchanges": {key: (rows[-1] if rows else None) for key, rows in exchanges.items()},
            "bse_display_only": north,
        },
        "normalization": {
            "float_market_cap": current.get("float_market_cap"),
            "market_turnover": current.get("market_turnover"),
            "financing_to_float_cap": current.get("financing_to_float_cap"),
            "financing_buy_to_turnover": current.get("financing_buy_to_turnover"),
            "basis": "沪深A股同日流通市值与A股成交额",
        },
        "history_position": {
            "comparable_start": COMPARABLE_START,
            "peak_date": peak.get("trade_date"),
            "peak_margin_balance": peak_total,
            "peak_gap_amount": current_total - peak_total if current_total is not None and peak_total is not None else None,
            "peak_gap_pct": (current_total / peak_total - 1) * 100 if current_total is not None and peak_total else None,
            "peak_recovery_pct": current_total / peak_total * 100 if current_total is not None and peak_total else None,
            "absolute_all_history_percentile": all_pct,
            "absolute_5y_percentile": rolling_total_pct,
            "financing_density_5y_percentile": density_pct,
            "financing_intensity_5y_percentile": intensity_pct,
            "financing_density_all_history_percentile": percentile_rank(all_density, current.get("financing_to_float_cap")) if full_ratio_history and current.get("financing_to_float_cap") is not None else None,
            "financing_intensity_all_history_percentile": percentile_rank(all_intensity, current.get("financing_buy_to_turnover")) if full_ratio_history and current.get("financing_buy_to_turnover") is not None else None,
            "ratio_history_start": ratio_start,
            "ratio_history_end": ratio_end,
            "ratio_history_observations": len(ratio_rows),
            "full_ratio_history_available": full_ratio_history,
        },
        "trends": {**trends, "turnover_5d_vs_prior20d_pct": turnover_change, "financing_intensity_5d_vs_prior20d_pct": intensity_change},
        "heat": {
            "score": heat_score,
            "label": _label_heat(heat_score),
            "coverage": heat_coverage,
            "components": {key: {"score": value, "weight": weight} for key, (value, weight) in heat_components.items()},
        },
        "deleveraging_pressure": {
            "score": pressure_score,
            "label": _label_pressure(pressure_score),
            "coverage": pressure_coverage,
            "components": {key: {"score": value, "weight": weight} for key, (value, weight) in pressure_components.items()},
        },
        "metric_guide": {
            "financing_leverage_density": {
                "definition": "融资余额 / 沪深A股流通市值",
                "meaning": "衡量市场存量市值中有多少由融资资金支撑，反映杠杆水位和潜在拥挤度。",
                "direction": "不是越高越好。偏高表示杠杆参与更深、上涨弹性可能更强，但回撤时负反馈也更大；偏低只表示杠杆参与较少。",
            },
            "financing_trading_intensity": {
                "definition": "当日融资买入额 / 沪深A股成交额",
                "meaning": "衡量当天成交中融资买盘的参与程度，反映杠杆资金活跃度。",
                "direction": "不是越高越好。偏高表示融资资金更活跃，也可能更拥挤；偏低可能是现金主导，也可能是风险偏好较弱。",
            },
            "leverage_heat": {
                "direction": "没有单独的好坏方向；低分表示杠杆水位低，高分表示杠杆水位高。需与去杠杆压力共同判断。",
                "bands": "0–20低杠杆，20–70正常，70–85升温，85–95偏热，95以上过热。",
            },
            "deleveraging_pressure": {
                "direction": "通常越低越平稳、越高风险越大；但低压力不保证上涨，高压力也不代表立即暴跌。",
                "bands": "0–30平稳，30–60观察，60–80降温，80–90去杠杆，90以上压力升高。",
            },
        },
        "regime": {"label": regime_label, "explanation": regime_explanation},
        "historical_comparisons": _historical_comparisons(display_series, current, broad_index_series),
        "concentration": concentration or {},
        "policy_events": [event for event in POLICY_EVENTS if event["date"] <= cutoff],
        "series": display_series[-60:],
        "broad_index_series": [
            {"trade_date": day, "close": value} for day, value in broad_index_series[-60:]
        ],
        "broad_index_name": broad_index_name,
        "data_quality": quality,
    }


def build_three_week_margin(
    margin: dict[str, Any],
    periods: list[dict[str, Any]],
    raw: dict[str, Any] | None = None,
    style_records: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    series = margin.get("series") or []
    output = []
    for period in periods:
        start = period.get("start_date")
        end = period.get("end_date")
        selected = [row for row in series if start <= str(row.get("trade_date")) <= end]
        prior = [row for row in series if str(row.get("trade_date")) < start]
        ending = selected[-1] if selected else None
        baseline = prior[-1] if prior else None
        financing_change = None
        if ending and baseline and parse_number(baseline.get("financing_balance")) not in {None, 0}:
            financing_change = (ending["financing_balance"] / baseline["financing_balance"] - 1) * 100
        intensities = [parse_number(row.get("financing_buy_to_turnover")) for row in selected]
        intensities = [value for value in intensities if value is not None]
        data_status = "insufficient_data" if not ending or not baseline else "ok" if intensities else "partial"
        snapshot = None
        if raw is not None and style_records is not None and ending:
            snapshot = analyze_margin_leverage(raw, style_records, cutoff=ending["trade_date"], concentration=None)
        output.append({
            "period_id": period.get("period_id"),
            "completeness": period.get("completeness"),
            "end_financing_balance": ending.get("financing_balance") if ending else None,
            "financing_balance_change": financing_change,
            "average_financing_intensity": sum(intensities) / len(intensities) if intensities else None,
            "heat_score": ((snapshot or {}).get("heat") or {}).get("score"),
            "heat_label": ((snapshot or {}).get("heat") or {}).get("label") or "数据不足",
            "heat_coverage": ((snapshot or {}).get("heat") or {}).get("coverage"),
            "deleveraging_pressure_score": ((snapshot or {}).get("deleveraging_pressure") or {}).get("score"),
            "deleveraging_pressure_label": ((snapshot or {}).get("deleveraging_pressure") or {}).get("label") or "数据不足",
            "deleveraging_pressure_coverage": ((snapshot or {}).get("deleveraging_pressure") or {}).get("coverage"),
            "data_status": data_status,
        })
    return {"periods": output, "current_heat": margin.get("heat"), "current_pressure": margin.get("deleveraging_pressure")}
