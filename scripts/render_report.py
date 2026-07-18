#!/usr/bin/env python3
"""Render a fund rotation analysis JSON file as a Chinese Markdown report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def fmt_pct(value: Any, digits: int = 2) -> str:
    if value is None:
        return "-"
    return f"{float(value):.{digits}f}%"


def fmt_weight(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value) * 100:.1f}%"


def fmt_score(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):.2f}"


def fmt_amount(value: Any) -> str:
    if value is None:
        return "-"
    value = float(value)
    if abs(value) >= 100000000:
        return f"{value / 100000000:.2f}亿"
    if abs(value) >= 10000:
        return f"{value / 10000:.2f}万"
    return f"{value:.0f}"


def product_badge(profile: dict[str, Any] | None) -> str:
    profile = profile or {}
    label = profile.get("management_style") or "未识别"
    colors = {
        "被动指数": ("#e8f1ff", "#1d4ed8"),
        "指数增强": ("#eef2ff", "#4f46e5"),
        "主动权益": ("#ecfdf5", "#047857"),
        "QDII": ("#f5f3ff", "#6d28d9"),
        "债券/固收": ("#f8fafc", "#475569"),
        "未识别": ("#f3f4f6", "#4b5563"),
    }
    bg, fg = colors.get(str(label), colors["未识别"])
    return f'<span style="background:{bg};color:{fg};padding:2px 6px;border-radius:4px;">{label}</span>'


def fmt_fund_size(profile: dict[str, Any] | None) -> str:
    profile = profile or {}
    text = profile.get("fund_size_text") or "-"
    share_text = profile.get("share_size_text")
    if share_text and share_text != "-":
        return f"{text}<br>份额:{share_text}"
    return str(text)


def fmt_turnover(profile: dict[str, Any] | None) -> str:
    profile = profile or {}
    if profile.get("turnover_text") and profile.get("turnover_text") != "-":
        return str(profile["turnover_text"])
    churn = profile.get("holding_churn") or {}
    if churn.get("rate") is not None:
        return f"{churn.get('level')}<br>{churn.get('note')}"
    return churn.get("level") or "-"


def risk_badges(profile: dict[str, Any] | None) -> str:
    profile = profile or {}
    flags = list(profile.get("risk_flags") or [])
    if profile.get("is_active_equity") and profile.get("fund_size") is None:
        flags.append("规模待补")
    if not flags:
        return "-"
    parts = []
    for flag in flags:
        bg, fg = ("#fff7ed", "#c2410c") if flag != "指数工具" else ("#e8f1ff", "#1d4ed8")
        parts.append(f'<span style="background:{bg};color:{fg};padding:2px 6px;border-radius:4px;">{flag}</span>')
    return " ".join(parts)


def cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", "<br>")


def table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return "_暂无可用数据。_\n"
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(cell(item) for item in row) + " |")
    return "\n".join(lines) + "\n"


def render_funds(analysis: dict[str, Any]) -> str:
    rows = []
    for fund in analysis["portfolio"]["funds"]:
        profile = fund.get("product_profile") or {}
        rows.append(
            [
                fund["code"],
                fund["name"],
                fmt_weight(fund["current_weight"]),
                product_badge(profile),
                fmt_fund_size(profile),
                fmt_turnover(profile),
                risk_badges(profile),
                "、".join(fund.get("themes") or []),
                fund.get("theme_confidence", "-"),
                fund.get("theme_basis", "-"),
                fmt_pct(fund["returns"].get("1月")),
                fmt_pct(fund["returns"].get("3月")),
                fmt_pct(fund.get("max_drawdown")),
                fund.get("score", "-"),
                fund.get("confidence", "-"),
            ]
        )
    return table(["代码", "基金", "当前占比", "产品属性", "规模", "换手线索", "风险标识", "主题", "主题置信度", "主题依据", "近1月", "近3月", "最大回撤", "评分", "净值置信度"], rows)


def render_style(analysis: dict[str, Any]) -> str:
    rows = []
    for item in analysis["market"]["style"]:
        rows.append([item["name"], fmt_pct(item.get("return_1m")), fmt_pct(item.get("return_3m")), item["status"]])
    return table(["指数/风格", "近1月", "近3月", "状态"], rows)


def render_flow_block(title: str, block: dict[str, Any]) -> str:
    inflow_rows = [
        [item["name"], fmt_amount(item.get("today")), fmt_amount(item.get("five_day")), fmt_amount(item.get("ten_day")), item["status"]]
        for item in block.get("inflow", [])[:8]
    ]
    outflow_rows = [
        [item["name"], fmt_amount(item.get("today")), fmt_amount(item.get("five_day")), fmt_amount(item.get("ten_day")), item["status"]]
        for item in block.get("outflow", [])[:8]
    ]
    return (
        f"### {title}流入\n\n"
        + table(["名称", "今日", "5日", "10日", "状态"], inflow_rows)
        + f"\n### {title}流出\n\n"
        + table(["名称", "今日", "5日", "10日", "状态"], outflow_rows)
    )


def render_product_risk_summary(rows: list[dict[str, Any]]) -> str:
    summary_rows = []
    for row in rows:
        profile = row.get("product_profile") or {}
        flags = list(profile.get("risk_flags") or [])
        include = profile.get("is_passive_index") or flags or (profile.get("is_active_equity") and profile.get("fund_size") is None)
        if not include:
            continue
        if profile.get("is_passive_index"):
            category = "被动指数/ETF联接"
            note = "工具型暴露，重点看标的指数、跟踪误差、规模和流动性"
        elif flags:
            category = "主动基金风险"
            note = "规模偏小或换手线索偏高时，净值弹性和风格漂移风险更大"
        else:
            category = "资料待补"
            note = "主动基金规模或换手字段未能采集到"
        summary_rows.append(
            [
                category,
                row.get("rank"),
                row.get("code"),
                row.get("name"),
                product_badge(profile),
                fmt_fund_size(profile),
                fmt_turnover(profile),
                risk_badges(profile),
                note,
            ]
        )
    return table(["类别", "排名", "代码", "基金", "产品属性", "规模", "换手线索", "风险标识", "解读"], summary_rows[:24])


def render_rankings(analysis: dict[str, Any]) -> str:
    rankings = analysis["rankings"]
    comparison = rankings.get("comparison") or {}
    periods = "、".join(rankings.get("available_periods") or []) or "暂无可用周期"
    primary_period = rankings.get("primary_period", "近1月")
    formula = comparison.get("candidate_score_formula") or "综合业绩分 = 0.70 × 近1月收益 + 0.30 × (近3月收益 / 3)"
    text = f"主观察周期：{primary_period}。可用排行周期：{periods}。\n\n"
    text += f"排序算法：{formula}。近3月先折算成月度节奏，用来确认趋势，但单月表现权重更高。\n\n"

    top30_rows = []
    for row in rankings.get("primary_top30", []):
        profile = row.get("product_profile") or {}
        top30_rows.append(
            [
                row.get("rank"),
                row.get("code"),
                row.get("name"),
                product_badge(profile),
                fmt_fund_size(profile),
                risk_badges(profile),
                fmt_pct(row.get("return_1m")),
                fmt_pct(row.get("return_3m")),
                fmt_score(row.get("performance_score")),
                fmt_pct(row.get("return_6m")),
                fmt_pct(row.get("return_ytd")),
                fmt_pct(row.get("return_1y")),
                row.get("fund_type", "-"),
                "、".join(row.get("themes") or []),
                row.get("theme_confidence", "-"),
                row.get("theme_basis", "-"),
                "是" if row.get("held") else "否",
                row.get("note", "-"),
            ]
        )
    text += "### 近1月为主的综合业绩前30\n\n"
    text += table(["排名", "代码", "基金", "产品属性", "规模", "风险标识", "近1月", "近3月", "综合分", "近6月", "今年来", "近1年", "类型", "识别主题", "主题置信度", "主题依据", "当前持有", "备注"], top30_rows)

    feature_rows = [[item["name"], item["count"]] for item in rankings.get("features", [])]
    type_rows = [[item["name"], item["count"]] for item in rankings.get("types", [])]
    period_rows = [
        [item.get("period"), item.get("count"), fmt_pct(item.get("top_return")), "、".join(f"{theme['name']}({theme['count']})" for theme in item.get("themes", []))]
        for item in rankings.get("period_summaries", [])
    ]
    text += "\n### Top 30 主题分布\n\n"
    text += f"近1月Top30共同特征：{rankings.get('summary', '暂无')}。\n\n"
    text += table(["主题", "出现次数"], feature_rows)
    text += "\n### 类型分布\n\n" + table(["类型", "出现次数"], type_rows)
    text += "\n### 产品属性与风险标识\n\n"
    text += render_product_risk_summary(rankings.get("primary_top30", []))
    text += "\n### 其他周期辅助摘要\n\n" + table(["周期", "样本数", "第一名收益", "主要主题"], period_rows)

    current_rows = []
    for row in comparison.get("current_vs_top30", []):
        profile = row.get("product_profile") or {}
        current_rows.append(
            [
                row.get("code"),
                row.get("name"),
                product_badge(profile),
                fmt_fund_size(profile),
                fmt_turnover(profile),
                risk_badges(profile),
                "、".join(row.get("themes") or []),
                row.get("theme_confidence", "-"),
                row.get("theme_basis", "-"),
                "、".join(row.get("top_stocks") or []),
                fmt_pct(row.get("return_1m")),
                fmt_pct(row.get("return_3m")),
                fmt_score(row.get("performance_score")),
                "是" if row.get("in_primary_top30") else "否",
                row.get("same_theme_top30_count"),
                row.get("decision_action"),
                row.get("decision_reason"),
            ]
        )
    gap_rows = [
        ["已覆盖主题", "、".join(comparison.get("covered_themes") or []) or "-"],
        ["缺失机会", "、".join(comparison.get("missing_themes") or []) or "-"],
        ["重复主题", "、".join(f"{item['name']}({item['count']})" for item in comparison.get("duplicated_themes", [])) or "-"],
        ["重复重仓股", "、".join(f"{item['name']}({item['count']})" for item in comparison.get("duplicated_stocks", [])) or "-"],
        ["同质化风险", comparison.get("overlap_risk", "-")],
    ]
    text += "\n### 与当前持仓差异\n\n"
    text += table(["维度", "结论"], gap_rows)
    text += "\n" + table(["代码", "当前基金", "产品属性", "规模", "换手线索", "风险标识", "主题", "主题置信度", "主题依据", "前十大重仓摘要", "近1月", "近3月", "综合分", "进入Top30", "同主题Top30数", "动作", "理由"], current_rows)

    candidate_rows = []
    for row in comparison.get("replacement_candidates", []):
        profile = row.get("product_profile") or {}
        candidate_rows.append(
            [
                row.get("candidate_type"),
                row.get("rank"),
                row.get("code"),
                row.get("name"),
                product_badge(profile),
                fmt_fund_size(profile),
                risk_badges(profile),
                fmt_pct(row.get("return_1m")),
                fmt_pct(row.get("return_3m")),
                fmt_score(row.get("performance_score")),
                "、".join(row.get("themes") or []),
                row.get("candidate_reason"),
            ]
        )
    text += "\n### 是否需要调仓\n\n"
    text += f"{comparison.get('rebalance_decision', '暂无调仓判断。')}\n\n"
    definition_rows = [[item.get("name"), item.get("description")] for item in comparison.get("candidate_type_definitions", [])]
    text += "#### 候选类型说明\n\n" + table(["类型", "含义"], definition_rows)

    recommendation_rows = []
    for row in comparison.get("top_replacement_recommendations", []):
        candidate_profile = row.get("candidate_product_profile") or {}
        replace_profile = row.get("replace_product_profile") or {}
        recommendation_rows.append(
            [
                row.get("candidate_type"),
                row.get("candidate_code"),
                row.get("candidate_name"),
                product_badge(candidate_profile),
                fmt_fund_size(candidate_profile),
                risk_badges(candidate_profile),
                fmt_pct(row.get("candidate_return_1m")),
                fmt_pct(row.get("candidate_return_3m")),
                fmt_score(row.get("candidate_score")),
                row.get("replace_code"),
                row.get("replace_name"),
                product_badge(replace_profile),
                fmt_fund_size(replace_profile),
                risk_badges(replace_profile),
                fmt_pct(row.get("replace_return_1m")),
                fmt_pct(row.get("replace_return_3m")),
                fmt_score(row.get("replace_score")),
                fmt_score(row.get("score_gap")),
                row.get("reason"),
            ]
        )
    text += "\n#### 推荐替换Top3\n\n"
    text += table(["候选类型", "候选代码", "候选基金", "候选属性", "候选规模", "候选风险", "候选近1月", "候选近3月", "候选综合分", "替换代码", "当前基金", "当前属性", "当前规模", "当前风险", "当前近1月", "当前近3月", "当前综合分", "分差", "推荐理由"], recommendation_rows)

    text += "\n#### 候选池排序\n\n"
    text += table(["候选类型", "排名", "代码", "基金", "产品属性", "规模", "风险标识", "近1月", "近3月", "综合分", "主题", "理由"], candidate_rows)
    return text

def render_allocations(analysis: dict[str, Any]) -> str:
    rows = []
    for item in analysis["portfolio"]["allocations"]:
        rows.append(
            [
                item["code"],
                item["name"],
                fmt_weight(item["current_weight"]),
                fmt_weight(item["target_weight"]),
                fmt_weight(item.get("first_step_target_weight")),
                fmt_weight(item["delta"]),
                fmt_weight(item.get("first_step_delta")),
                fmt_amount(item.get("delta_amount")),
                item["action"],
                item["priority"],
                "；".join(item.get("reasons") or []),
            ]
        )
    validation = (analysis.get("portfolio") or {}).get("allocation_validation") or {}
    note = f"配比约束校验：{validation.get('status', 'unknown')}。战略目标满足集中度约束；第一期目标用于执行单期调整上限。\n\n"
    return note + table(["代码", "基金", "当前", "战略目标", "第一期目标", "战略变化", "第一期变化", "战略金额差额", "动作", "优先级", "理由"], rows)


def render(analysis: dict[str, Any]) -> str:
    constraints = analysis.get("constraints", {})
    warnings = analysis.get("warnings") or []
    lines = [
        "# 基金组合轮动分析报告",
        "",
        f"- 数据时间：{analysis.get('as_of', '-')}",
        f"- 数据源：{analysis.get('source', '-')}",
        f"- 组合规模：{fmt_amount(analysis.get('portfolio', {}).get('total_amount'))}",
        f"- 风控约束：单只基金上限 {fmt_weight(constraints.get('single_fund_cap'))}，单主题上限 {fmt_weight(constraints.get('single_theme_cap'))}，高波动主题单期调整上限 {fmt_weight(constraints.get('high_volatility_adjustment_cap'))}",
        "",
        "## 持仓概览与近期表现",
        "",
        render_funds(analysis),
        "",
        "## 大盘与风格判断",
        "",
        render_style(analysis),
        "",
        "## 行业/概念资金流入流出",
        "",
        render_flow_block("行业", analysis["market"]["industry_flow"]),
        "",
        render_flow_block("概念", analysis["market"]["concept_flow"]),
        "",
        "## 基金排行 Top 30 特征分析",
        "",
        render_rankings(analysis),
        "",
        "## 调仓建议与目标配比",
        "",
        render_allocations(analysis),
        "",
        "## 风险提示与数据缺口",
        "",
        f"- {analysis.get('disclaimer')}",
    ]
    if warnings:
        lines.extend(f"- 数据提示：{warning}" for warning in warnings[:12])
    else:
        lines.append("- 暂无数据采集警告。")
    lines.append("- 调仓建议仅为基金级别配置分析，不代表收益承诺，也不应替代个人风险评估。")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    report = render(load_json(args.analysis))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")


if __name__ == "__main__":
    main()
