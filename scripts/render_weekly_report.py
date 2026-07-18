#!/usr/bin/env python3
"""Render weekly fund analysis as a Chinese Markdown report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from three_week_analysis import select_rotation_rows
from report_contract import REPORT_FORMAT_VERSION


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def pct(value: Any, digits: int = 2) -> str:
    return "-" if value is None else f"{float(value):.{digits}f}%"


def money(value: Any) -> str:
    if value is None:
        return "-"
    value = float(value)
    if abs(value) >= 100000000:
        return f"{value / 100000000:.2f}亿"
    if abs(value) >= 10000:
        return f"{value / 10000:.2f}万"
    return f"{value:.0f}"


def flow_money(value: Any) -> str:
    if value is None:
        return "-"
    value = float(value)
    return f"{'+' if value > 0 else ''}{value / 100000000:.2f}亿元"


def money_yi(value: Any) -> str:
    return "数据不足" if value is None else f"{float(value) / 100000000:,.2f}亿元"


def score(value: Any) -> str:
    return "未评分" if value is None else f"{float(value):.1f}分"


def status_text(value: Any) -> str:
    return {
        "ok": "正常", "complete": "数据完整", "partial": "部分数据可用",
        "degraded": "降级展示", "insufficient_data": "数据不足",
        "insufficient_sample": "样本不足",
        "explicit": "指定截止日", "current": "当前周", "completed": "完整周",
        "optional_unavailable": "可选数据暂不可用", "not_required": "本次无需采集",
        "fallback_used": "已使用备用数据源", "stale_source": "数据过期，已拒绝",
        "failed": "采集失败",
    }.get(str(value), str(value or "数据不足"))


def table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return "_暂无可用数据。_\n"
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join("" if item is None else str(item).replace("|", "\\|") for item in row) + " |")
    return "\n".join(lines) + "\n"


def sector_rows(rows: list[dict[str, Any]]) -> list[list[Any]]:
    return [
        [
            row.get("name"),
            row.get("theme"),
            pct(row.get("week_return")),
            flow_money(row.get("today_flow")),
            flow_money(row.get("five_day_flow")),
            flow_money(row.get("ten_day_flow")),
            row.get("flow_status"),
            row.get("flow_status_reason"),
            pct((row.get("coverage_weight") or 0) * 100),
            f"{row.get('classification_basis') or '待补充'} / {row.get('classification_confidence') or '低'}",
            row.get("return_basis"),
            row.get("source_date"),
            row.get("cache_age_days"),
            row.get("flow_basis"),
            row.get("universe_scope"),
            "、".join(row.get("candidate_etfs") or []),
        ]
        for row in rows[:10]
    ]


def render(data: dict[str, Any]) -> str:
    week = data.get("week") or {}
    portfolio = data.get("portfolio") or {}
    market = data.get("market") or {}
    margin = market.get("margin_leverage") or {}
    sectors = market.get("sector_top10") or {}
    comparison = data.get("comparison") or {}
    three = data.get("three_week_analysis") or {}
    periods = three.get("periods") or []
    three_portfolio = three.get("portfolio") or {}
    industry_display = select_rotation_rows(three.get("industries") or [], periods)
    concept_display = select_rotation_rows(three.get("concepts") or [], periods)
    synthesis = data.get("llm_synthesis") or {}
    conclusion = comparison.get("weekly_conclusion") or {}
    conclusion_lines = [
        conclusion.get("market_summary"), conclusion.get("flow_summary"), conclusion.get("coverage_summary"),
        conclusion.get("overlap_summary"), conclusion.get("decision_summary"),
    ]
    lines = [
        "# 三周基金轮动复盘",
        "",
        f"- 数据时间：{data.get('as_of')}",
        f"- 数据修订：{data.get('data_revision') or 'legacy'}",
        f"- 报告格式：{data.get('report_format_version') or REPORT_FORMAT_VERSION}",
        f"- 数据来源策略：{data.get('provider_policy') or 'akshare-only'}；本次选用：{(data.get('provider_route') or {}).get('selected_provider') or 'AkShare及公开备用源'}",
        f"- 周期：{week.get('baseline_date')} -> {week.get('end_date')}（{'完整周' if week.get('completeness') == 'complete' else '进行中周'}）",
        f"- 权重口径：{portfolio.get('weight_basis_display') or portfolio.get('weight_basis')}",
        f"- 权重说明：{portfolio.get('weight_assumption') or '按输入权重分析'}；报告占比不代表券商账户实时仓位。",
        f"- 净值覆盖率：{pct((portfolio.get('nav_coverage_weight') or 0) * 100)}",
        f"- 组合本周收益：{pct(portfolio.get('weekly_return'))}",
        f"- 截至当前复合收益：{pct(three_portfolio.get('three_week_compound_return'))}（{three_portfolio.get('compound_basis') or '口径待确认'}）",
        f"- 完整周复合收益：{pct(three_portfolio.get('completed_weeks_compound_return'))}（正式动作仅使用完整周）",
        f"- 本周监测风格：{(three.get('style_regime') or {}).get('current_regime') or '数据不足'}；动作依据风格：{(three.get('style_regime') or {}).get('action_regime') or '数据不足'}",
        f"- 部分估算：{pct(portfolio.get('partial_weekly_return'))}（仅在覆盖率不足时参考）",
        "",
        "## 三周综合判断",
        "",
        f"- 市场状态：{synthesis.get('market_regime') or '证据不足'}",
        f"- 持续/增强方向：{'、'.join(synthesis.get('persistent_leaders') or []) or '未形成'}",
        f"- 新出现方向：{'、'.join(synthesis.get('emerging_sectors') or []) or '未确认'}",
        f"- 退潮方向：{'、'.join(synthesis.get('fading_sectors') or []) or '未确认'}",
        f"- 置信度：{synthesis.get('confidence') or '低'}；进行中周只用于监测。",
        "",
        "## 三周组合收益轨迹",
        "",
        table(
            ["基金"] + [f"{period.get('label')}（{status_text(period.get('completeness'))}）" for period in periods],
            [["当前组合"] + [pct((three_portfolio.get('weekly_returns') or {}).get(period.get('period_id'))) for period in periods]] + [
                [f"{row.get('name')}（{row.get('trajectory_state') or '证据不足'}）"] + [pct((row.get("periods") or {}).get(period.get("period_id"), {}).get("return")) for period in periods]
                for row in three_portfolio.get("funds") or []
            ],
        ),
        "## 三周风格变化",
        "",
        table(
            ["风格"] + [period.get("label") for period in periods],
            [[row.get("name")] + [f"{pct((row.get('periods') or {}).get(period.get('period_id'), {}).get('return'))} / #{(row.get('periods') or {}).get(period.get('period_id'), {}).get('rank') or '-'}" for period in periods] for row in three.get("styles") or []],
        ),
        "## A股杠杆温度",
        "",
        "> 长期比较主体为沪深市场，北交所单列。总两融余额用于展示，融资余额用于评分；本模块只解释市场环境，不改变基金评分、Top3、目标配比或调仓动作。",
        "",
        table(
            ["指标", "当前值", "历史位置/含义"],
            [
                ["两融余额", money_yi((margin.get("current") or {}).get("margin_balance")), f"距全历史峰值 {pct((margin.get('history_position') or {}).get('peak_gap_pct'))}"],
                ["融资余额", money_yi((margin.get("current") or {}).get("financing_balance")), f"近20日 {pct((margin.get('trends') or {}).get('change_20d_pct'))}"],
                ["融资杠杆密度", pct((margin.get("normalization") or {}).get("financing_to_float_cap")), f"融资余额/沪深A股流通市值；近5年窗口分位 {pct((margin.get('history_position') or {}).get('financing_density_5y_percentile'))}"],
                ["融资交易强度", pct((margin.get("normalization") or {}).get("financing_buy_to_turnover")), f"融资买入额/沪深A股成交额；近5年窗口分位 {pct((margin.get('history_position') or {}).get('financing_intensity_5y_percentile'))}"],
                ["杠杆热度", f"{score((margin.get('heat') or {}).get('score'))} / {(margin.get('heat') or {}).get('label') or '数据不足'}", "不是越高越好；低分是水位低，高分是杠杆参与深，需与压力同看"],
                ["去杠杆压力", f"{score((margin.get('deleveraging_pressure') or {}).get('score'))} / {(margin.get('deleveraging_pressure') or {}).get('label') or '数据不足'}", "通常越低越平稳、越高风险越大，但不能单独预测涨跌"],
            ],
        ),
        "- 融资杠杆密度偏高：杠杆参与更深，上涨弹性可能更强，回撤时负反馈也可能更大；偏低只表示杠杆参与较少。",
        "- 融资交易强度偏高：当日成交中的融资买盘更活跃，也可能更拥挤；偏低可能是现金主导，也可能是风险偏好较弱。",
        "- 杠杆热度没有单独的好坏方向；去杠杆压力则通常低更平稳、高更危险。两个指标必须一起看。",
        f"二维状态：{(margin.get('regime') or {}).get('label') or '数据不足'}。{(margin.get('regime') or {}).get('explanation') or '缺少同口径历史，暂不判断。'}",
        "",
        "### 最近三周两融变化",
        "",
        table(
            ["周期", "周末融资余额", "本周变化", "平均融资交易强度", "杠杆热度", "去杠杆压力", "状态"],
            [[row.get("period_id"), money_yi(row.get("end_financing_balance")), pct(row.get("financing_balance_change")), pct(row.get("average_financing_intensity")), f"{score(row.get('heat_score'))} / {row.get('heat_label') or '数据不足'}", f"{score(row.get('deleveraging_pressure_score'))} / {row.get('deleveraging_pressure_label') or '数据不足'}", status_text(row.get("data_status"))] for row in ((three.get("margin_leverage") or {}).get("periods") or [])],
        ),
        "### 历史阶段同口径比较",
        "",
        table(
            ["阶段", "余额峰值", "峰值日", "当前距峰值", "杠杆密度峰值", "交易强度峰值", "20日最快扩张", "峰后20日最大回撤", "峰后60日最大回撤"],
            [[row.get("label"), money_yi(row.get("peak_margin_balance")), row.get("peak_date"), pct(row.get("current_vs_peak_pct")), f"{pct(row.get('peak_financing_to_float_cap'))}（{row.get('peak_financing_to_float_cap_date') or '日期不足'}）", f"{pct(row.get('peak_financing_buy_to_turnover'))}（{row.get('peak_financing_buy_to_turnover_date') or '日期不足'}）", pct(row.get("fastest_20d_financing_growth")), pct(row.get("post_peak_20d_max_drawdown")), pct(row.get("post_peak_60d_max_drawdown"))] for row in margin.get("historical_comparisons") or []],
        ),
        "### 走步法历史校准",
        "",
        table(
            ["热度区间", "样本数", "样本状态", "未来20日回撤中位数", "20日风险事件率", "未来60日回撤中位数", "60日风险事件率"],
            [[row.get("band"), row.get("sample_count"), status_text(row.get("status")), pct(row.get("median_20d_max_drawdown")), pct(row.get("event_20d_le_5pct_rate") * 100) if row.get("event_20d_le_5pct_rate") is not None else "样本不足", pct(row.get("median_60d_max_drawdown")), pct(row.get("event_60d_le_10pct_rate") * 100) if row.get("event_60d_le_10pct_rate") is not None else "样本不足"] for row in ((margin.get("calibration") or {}).get("heat_bands") or [])],
        ),
        "低杠杆不代表上涨空间必然较大；高杠杆也不代表市场立即见顶。需要同时观察价格、盈利、成交和去杠杆压力。",
        *[f"- 数据限制：{item}" for item in margin.get("data_quality") or []],
        "",
        "## 三周行业轮动",
        "",
        table(
            ["行业", "轮动状态", "持仓覆盖", "依据"] + [period.get("label") for period in periods],
            [[row.get("name"), f"{row.get('rotation_state')} / {row.get('monitor_state')}", f"{row.get('portfolio_coverage')} {pct((row.get('coverage_weight') or 0) * 100)}", row.get("rotation_reason")] + [f"收益 {pct((row.get('periods') or {}).get(period.get('period_id'), {}).get('return'))} / 资金 {flow_money((row.get('periods') or {}).get(period.get('period_id'), {}).get('weekly_net_flow'))}" for period in periods] for row in industry_display],
        ),
        "## 三周概念轮动",
        "",
        table(
            ["概念", "轮动状态", "持仓覆盖", "依据"] + [period.get("label") for period in periods],
            [[row.get("name"), f"{row.get('rotation_state')} / {row.get('monitor_state')}", f"{row.get('portfolio_coverage')} {pct((row.get('coverage_weight') or 0) * 100)}", row.get("rotation_reason")] + [f"收益 {pct((row.get('periods') or {}).get(period.get('period_id'), {}).get('return'))} / 资金 {flow_money((row.get('periods') or {}).get(period.get('period_id'), {}).get('weekly_net_flow'))}" for period in periods] for row in concept_display],
        ),
        "## 本周结论",
        "",
        *[f"- {item}" for item in conclusion_lines if item],
        f"- 结论口径：{conclusion.get('confidence_note') or '只使用取得真实周期证据的数据。'}",
        "",
        "## 持仓本周表现",
        "",
        "> 当前组合占比来自上述权重口径。近1年最大回撤表示过去一年从阶段高点到低点的最大跌幅，负值越大风险越高；未评分时会列出缺失分项。",
        "",
        table(
            ["代码", "基金", "产品类型", "画像状态", "披露期", "规模", "换手", "当前组合占比", "主题", "本周收益", "近1月收益", "近3月收益", "近1年最大回撤", "周内振幅", "周度综合分", "建议动作", "产品风险", "理由/缺失分项"],
            [
                [
                    row.get("code"),
                    row.get("name"),
                    row.get("product_type"),
                    row.get("profile_status"),
                    row.get("disclosure_period"),
                    money(row.get("fund_size")),
                    pct(row.get("turnover")),
                    pct((row.get("current_weight") or 0) * 100),
                    "、".join(row.get("themes") or []),
                    pct(row.get("week_return")),
                    pct(row.get("one_month")),
                    pct(row.get("three_month")),
                    pct(row.get("max_drawdown_1y")),
                    pct(row.get("week_range")),
                    f"{row.get('weekly_score')}分" if row.get("weekly_score") is not None else "未评分",
                    row.get("decision_action", "-"),
                    "、".join(row.get("quality_flags") or []),
                    "；".join(filter(None, [row.get("decision_reason"), row.get("score_unavailable_reason")])),
                ]
                for row in (comparison.get("current_vs_weekly") or portfolio.get("funds") or [])
            ],
        ),
        "## 大盘与风格",
        "",
        table(
            ["指数/风格", "本周收益", "收益口径", "最终数据源", "起始日", "结束日", "数据状态"],
            [
                [row.get("name"), pct(row.get("week_return")), row.get("return_basis"), row.get("resolved_source"), row.get("baseline_date"), row.get("source_latest_date") or row.get("latest_date"), row.get("data_status_display")]
                for row in market.get("style_indexes", [])
            ],
        ),
        "## 板块 Top10",
        "",
        "### 行业近5个交易日收益 Top10",
        "",
        "_滚动5个交易日口径；自然周收益以三周行业矩阵为准。_",
        "",
        table(["板块", "行业分类", "收益", "单日资金", "5日资金", "10日资金", "资金状态", "判定依据", "组合相关主题估算占比", "分类依据/置信度", "收益口径", "截止日", "缓存天数", "资金口径", "样本范围", "候选ETF"], sector_rows(sectors.get("industry_return") or [])),
        "### 概念近5个交易日收益 Top10",
        "",
        "_滚动5个交易日口径；自然周收益以三周概念矩阵为准。_",
        "",
        table(["板块", "行业分类", "收益", "单日资金", "5日资金", "10日资金", "资金状态", "判定依据", "组合相关主题估算占比", "分类依据/置信度", "收益口径", "截止日", "缓存天数", "资金口径", "样本范围", "候选ETF"], sector_rows(sectors.get("concept_return") or [])),
        "### 今日涨跌榜（非周收益）",
        "",
        table(
            ["类型", "板块", "今日涨跌", "口径"],
            [[row.get("type"), row.get("name"), pct(row.get("today_return")), row.get("return_basis")] for row in (sectors.get("industry_today") or [])[:5] + (sectors.get("concept_today") or [])[:5]],
        ),
        f"### 报告期后当日资金观察（{week.get('collection_trade_date')}，不参与上周结论）",
        "",
        table(
            ["方向", "板块", "今日涨跌", "单日主力净额", "资金判断", "说明"],
            [
                [direction, row.get("name"), pct(row.get("today_return")), flow_money(row.get("today_flow")), row.get("flow_status_display"), row.get("flow_status_reason")]
                for direction, rows in [
                    ("流入", (sectors.get("industry_today_inflow") or [])[:5] + (sectors.get("concept_today_inflow") or [])[:5]),
                    ("流出", (sectors.get("industry_today_outflow") or [])[:5] + (sectors.get("concept_today_outflow") or [])[:5]),
                ]
                for row in rows
            ],
        ),
        f"### 报告期5日行业资金流入 Top10（截至 {week.get('end_date')}）",
        "",
        table(["板块", "行业分类", "收益", "单日资金", "5日资金", "10日资金", "资金状态", "判定依据", "组合相关主题估算占比", "分类依据/置信度", "收益口径", "截止日", "缓存天数", "资金口径", "样本范围", "候选ETF"], sector_rows(sectors.get("industry_inflow") or [])),
        f"### 报告期5日概念资金流入 Top10（截至 {week.get('end_date')}）",
        "",
        table(["板块", "行业分类", "收益", "单日资金", "5日资金", "10日资金", "资金状态", "判定依据", "组合相关主题估算占比", "分类依据/置信度", "收益口径", "截止日", "缓存天数", "资金口径", "样本范围", "候选ETF"], sector_rows(sectors.get("concept_inflow") or [])),
        f"### 报告期5日资金流出 Top10（截至 {week.get('end_date')}）",
        "",
        table(["板块", "行业分类", "收益", "单日资金", "5日资金", "10日资金", "资金状态", "判定依据", "组合相关主题估算占比", "分类依据/置信度", "收益口径", "截止日", "缓存天数", "资金口径", "样本范围", "候选ETF"], sector_rows((sectors.get("industry_outflow") or [])[:5] + (sectors.get("concept_outflow") or [])[:5])),
        "### 基金主题热度代理（非板块收益）",
        "",
        table(
            ["主题", "基金样本", "平均近1周", "口径", "当前覆盖"],
            [[row.get("name"), row.get("sample_size"), pct(row.get("average_fund_week_return")), row.get("return_basis"), row.get("current_coverage")] for row in sectors.get("theme_signal_proxy") or []],
        ),
        "## 近1周基金排行信号",
        "",
        table(
            ["排名", "代码", "基金", "产品类型", "披露期", "规模", "换手", "近1周", "近1月", "主题", "风险"],
            [
                [idx + 1, row.get("code"), row.get("name"), row.get("product_type"), row.get("disclosure_period"), money(row.get("fund_size")), pct(row.get("turnover")), pct(row.get("week_return")), pct(row.get("one_month")), "、".join(row.get("themes") or []), "、".join(row.get("quality_flags") or [])]
                for idx, row in enumerate(market.get("weekly_top_funds", [])[:20])
            ],
        ),
        "## 候选 ETF 交易质量",
        "",
        table(
            ["代码", "名称", "本周", "收益口径/置信度", "截止日收盘", "截止日溢价", "截止日成交额", "实时价/IOPV", "实时溢价", "执行状态", "风险/份额事件"],
            [
                [
                    row.get("code"),
                    row.get("name"),
                    pct(row.get("week_return")),
                    f"{row.get('return_basis')} / {row.get('return_confidence')}",
                    (row.get("eod_quality") or {}).get("close"),
                    pct((row.get("eod_quality") or {}).get("premium_rate")),
                    money((row.get("eod_quality") or {}).get("turnover")),
                    f"{(row.get('live_snapshot') or {}).get('price')} / {(row.get('live_snapshot') or {}).get('iopv')}",
                    pct((row.get("live_snapshot") or {}).get("premium_rate")),
                    row.get("execution_note"),
                    "、".join((row.get("quality_flags") or []) + (row.get("corporate_actions") or [])),
                ]
                for row in data.get("candidate_etfs", [])
            ],
        ),
        "## Top3 替换观察",
        "",
        table(
            ["调出", "候选", "双方得分", "分差", "动作", "第一阶段比例", "证据", "风险", "理由"],
            [
                [
                    f"{row.get('replace_code')} {row.get('replace_name')}",
                    f"{row.get('candidate_code')} {row.get('candidate_name')}",
                    f"{row.get('replace_score')} -> {row.get('candidate_score')}",
                    row.get("score_gap"),
                    row.get("action"),
                    pct(row.get("suggested_first_step_weight") * 100) if row.get("suggested_first_step_weight") is not None else "执行前复核",
                    "；".join(row.get("evidence") or []),
                    "、".join(row.get("risk_flags") or []),
                    row.get("reason"),
                ]
                for row in comparison.get("replacement_top3", [])
            ],
        ),
        f"替换状态：{comparison.get('replacement_status_display') or '证据不足，暂不生成替换建议'}。{comparison.get('replacement_note') or ''}",
        *[f"- 阻断原因：{item}" for item in comparison.get("replacement_blockers") or []],
        "",
        "## 未解决的必需数据",
        "",
    ]
    warnings = data.get("warnings") or []
    lines.extend([f"- {warning}" for warning in warnings] or ["- 暂无数据缺口。"])
    optional_unavailable = [row for row in data.get("data_quality") or [] if row.get("requirement") == "optional" and row.get("status") in {"failed", "partial", "optional_unavailable"}]
    lines.extend(["", "## 可选实时数据", ""])
    lines.extend([f"- {row.get('dataset')}：{row.get('reason') or row.get('basis')}" for row in optional_unavailable] or ["- 暂无可选实时数据缺口。"])
    lines.extend(["", "## 分析说明", ""])
    lines.extend([f"- {note}" for note in data.get("analysis_notes") or []] or ["- 暂无额外分析说明。"])
    lines.extend(["", "## 已自动恢复的数据源", ""])
    recovered = [row for row in data.get("data_quality") or [] if row.get("status") == "fallback_used"]
    lines.append(table(["数据集", "恢复来源", "口径"], [[row.get("dataset"), row.get("resolved_by"), row.get("basis")] for row in recovered]))
    lines.extend(["", "## 逻辑数据集状态", ""])
    lines.append(
        table(
            ["数据集", "状态", "提供方", "最终接口", "记录数", "口径/原因"],
            [[row.get("dataset"), status_text(row.get("status")), row.get("provider"), row.get("resolved_by"), row.get("record_count"), row.get("basis") or row.get("reason")] for row in data.get("data_quality") or []],
        )
    )
    lines.extend(["", data.get("disclaimer", "")])
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(load_json(args.analysis)), encoding="utf-8")


if __name__ == "__main__":
    main()
