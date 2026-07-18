#!/usr/bin/env python3
"""Render the schema-v2 weekly analysis as a responsive HTML decision report."""

from __future__ import annotations

import argparse
import html
from pathlib import Path
from typing import Any

from data_access import load_json
from report_contract import NAV_ITEMS, REPORT_FORMAT_VERSION, REPORT_TITLE
from three_week_analysis import select_rotation_rows


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def pct(value: Any, digits: int = 2) -> str:
    return "-" if value is None else f"{float(value):.{digits}f}%"


def money(value: Any) -> str:
    if value is None:
        return "-"
    number = float(value)
    if abs(number) >= 100_000_000:
        return f"{number / 100_000_000:.2f}亿"
    if abs(number) >= 10_000:
        return f"{number / 10_000:.2f}万"
    return f"{number:.0f}"


def flow_money(value: Any) -> str:
    if value is None:
        return "-"
    number = float(value)
    return f"{'+' if number > 0 else ''}{number / 100_000_000:.2f}亿元"


def money_yi(value: Any) -> str:
    return "数据不足" if value is None else f"{float(value) / 100_000_000:,.2f}亿元"


def score_text(value: Any) -> str:
    return "未评分" if value is None else f"{float(value):.1f}分"


def tone(value: Any) -> str:
    if value is None:
        return "neutral"
    return "positive" if float(value) >= 0 else "negative"


def metric(label: str, value: str, note: str, style: str = "neutral") -> str:
    return f'<article class="card metric span-3 {style}"><span>{esc(label)}</span><b>{esc(value)}</b><small>{esc(note)}</small></article>'


STATUS_DISPLAY = {
    "ok": "正常",
    "fallback_used": "已使用备用数据源",
    "partial": "部分数据可用",
    "failed": "全部来源失败",
    "stale_source": "数据过期，已拒绝",
    "partial_profile": "部分画像",
    "stale_profile": "画像已过期",
    "stale_basic_info": "基本资料待刷新",
    "crosscheck_ok": "交叉核验一致",
    "crosscheck_conflict": "交叉核验冲突",
    "insufficient_data": "数据不足",
    "insufficient_evidence": "证据不足，暂不生成替换建议",
    "complete": "数据完整",
    "degraded": "降级展示",
    "display_only": "仅作展示",
    "insufficient_sample": "样本不足",
    "explicit": "指定截止日",
    "current": "当前周",
    "completed": "完整周",
    "optional_unavailable": "可选数据暂不可用",
    "deterministic_fallback": "程序规则综合",
    "not_required": "本次无需采集",
    "insufficient_profile": "画像证据不足",
}


def status_display(value: Any) -> str:
    return STATUS_DISPLAY.get(str(value), str(value or "数据不足"))


def labeled_value(label: str, value: str, style: str = "") -> str:
    return f'<span class="holding-field {style}"><small>{esc(label)}</small><b>{esc(value)}</b></span>'


def holding_rows(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return '<p class="empty">暂无持仓数据。</p>'
    output = [
        '<div class="holding-head"><span>基金与画像</span><span>当前组合占比</span><span>本周收益</span><span>近1月收益</span><span>近3月收益</span><span>近1年最大回撤</span><span>周度综合分</span><span>建议动作</span></div>'
    ]
    for row in sorted(rows, key=lambda item: item.get("week_return") if item.get("week_return") is not None else -999, reverse=True):
        score = f"{row['weekly_score']:.2f}分" if row.get("weekly_score") is not None else "未评分"
        score_note = row.get("score_unavailable_reason") if row.get("weekly_score") is None else f"评分覆盖率 {pct((row.get('score_coverage') or 0) * 100)}"
        output.append(
            f'''<div class="data-row holding-row" data-row="holding">
              <div class="holding-name"><b>{esc(row.get('name'))}</b><small>{esc(row.get('code'))} · {esc(row.get('product_type') or '类型待确认')} · {'、'.join(esc(theme) for theme in row.get('themes') or []) or '主题待确认'}</small><small>画像 {esc(status_display(row.get('profile_status')))} · 披露期 {esc(row.get('disclosure_period') or '待补充')} · 规模 {money(row.get('fund_size'))} · 换手 {pct(row.get('turnover'))} · {esc('、'.join(row.get('quality_flags') or []) or '无产品风险标记')}</small></div>
              {labeled_value('当前组合占比', pct((row.get('current_weight') or 0) * 100))}
              {labeled_value('本周收益', pct(row.get('week_return')), tone(row.get('week_return')))}
              {labeled_value('近1月收益', pct(row.get('one_month')), tone(row.get('one_month')))}
              {labeled_value('近3月收益', pct(row.get('three_month')), tone(row.get('three_month')))}
              {labeled_value('近1年最大回撤', pct(row.get('max_drawdown_1y')), 'negative' if row.get('max_drawdown_1y') is not None else 'neutral')}
              {labeled_value('周度综合分', score)}
              <span class="holding-action"><small>建议动作</small><b class="badge">{esc(row.get('decision_action') or '观察')}</b></span>
              <small class="holding-reason">{esc(row.get('decision_reason') or status_display(row.get('data_status')))} {esc(score_note or '')}</small>
            </div>'''
        )
    return "".join(output)


def style_rows(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return '<p class="empty">暂无风格指数数据。</p>'
    output = []
    maximum = max([abs(float(row.get("week_return") or 0)) for row in rows] + [1])
    for row in rows:
        value = row.get("week_return")
        width = max(2, abs(float(value or 0)) / maximum * 100)
        output.append(
            f'''<div class="style-row" data-row="style"><b>{esc(row.get('name'))}</b><div class="track"><i class="{tone(value)}" style="width:{width:.1f}%"></i></div><strong class="{tone(value)}">{pct(value)}</strong><small>{esc(row.get('return_basis') or '周收益不可确认')} · {esc(row.get('resolved_source') or '无有效来源')} · 最新 {esc(row.get('source_latest_date') or row.get('latest_date') or '日期不足')} · {esc(row.get('data_status_display') or status_display(row.get('data_status')))}</small></div>'''
        )
    return "".join(output)


def sector_rows(rows: list[dict[str, Any]], value_key: str = "week_return", return_label: str | None = None) -> str:
    if not rows:
        return '<p class="empty">暂无可用数据，未使用代理数据补齐。</p>'
    output = []
    for row in rows[:10]:
        value = row.get(value_key)
        output.append(
            f'''<div class="data-row sector-row" data-row="sector" data-return-basis="{esc(row.get('return_basis'))}">
              <div class="sector-title"><b>{esc(row.get('name'))}</b><small>{esc(row.get('theme') or '待分类')} · {esc(row.get('classification_basis') or '尚无分类依据')} · 置信度 {esc(row.get('classification_confidence') or '低')}</small></div>
              <span><small>{esc(return_label or ('近5日收益' if value_key == 'week_return' else '今日涨跌'))}</small><b class="{tone(value)}">{pct(value)}</b></span>
              <span><small>单日主力净额</small><b class="{tone(row.get('today_flow'))}">{flow_money(row.get('today_flow'))}</b></span>
              <span><small>5日主力净额</small><b class="{tone(row.get('five_day_flow'))}">{flow_money(row.get('five_day_flow'))}</b></span>
              <span><small>10日主力净额</small><b class="{tone(row.get('ten_day_flow'))}">{flow_money(row.get('ten_day_flow'))}</b></span>
              <span><small>资金判断</small><b class="badge">{esc(row.get('flow_status_display') or row.get('flow_status'))}</b></span>
              <span><small>组合相关主题估算占比</small><b>{pct((row.get('coverage_weight') or 0) * 100)}</b></span>
              <small class="sector-evidence">{esc(row.get('flow_status_reason') or '资金周期证据不足')} · 收益口径：{esc(row.get('return_basis') or '不可确认')} · 资金口径：{esc(row.get('flow_basis') or '不可确认')} · 截止 {esc(row.get('source_date') or '日期不足')} · 缓存 {esc(row.get('cache_age_days') or 0)} 天 · {esc(row.get('universe_scope') or '全市场')}</small>
            </div>'''
        )
    return "".join(output)


def proxy_rows(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return '<p class="empty">暂无基金主题代理。</p>'
    return "".join(
        f'''<div class="data-row proxy-row" data-row="proxy"><b>{esc(row.get('name'))}</b><strong>{pct(row.get('average_fund_week_return'))}</strong><span>样本 {esc(row.get('sample_size'))}</span><small>{esc(row.get('return_basis'))}</small></div>'''
        for row in rows
    )


def top_funds(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return '<p class="empty">暂无基金排行数据。</p>'
    return "".join(
        f'''<div class="data-row rank-row" data-row="fund-rank"><span>#{index}</span><div><b>{esc(row.get('name'))}</b><small>{esc(row.get('code'))} · {esc(row.get('product_type') or '类型待确认')} · {'、'.join(esc('主题待确认' if theme == '未识别' else theme) for theme in row.get('themes') or []) or '主题待确认'}</small><small>披露期 {esc(row.get('disclosure_period') or '待补充')} · 规模 {money(row.get('fund_size'))} · 换手 {pct(row.get('turnover'))} · {esc('、'.join(row.get('quality_flags') or []) or '无产品风险标记')}</small></div><strong class="{tone(row.get('week_return'))}">{pct(row.get('week_return'))}</strong><span>1月 {pct(row.get('one_month'))}</span></div>'''
        for index, row in enumerate(rows[:20], 1)
    )


def etf_cards(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return '<p class="empty">暂无 ETF 数据。</p>'
    output = []
    for row in rows:
        flags = row.get("quality_flags") or []
        actions = row.get("corporate_actions") or []
        eod = row.get("eod_quality") or {}
        live = row.get("live_snapshot") or {}
        card_tone = "risk" if "追高风险" in flags else "warn" if flags else "ok"
        output.append(
            f'''<article class="etf-card {card_tone}" data-row="etf" data-return-basis="{esc(row.get('return_basis'))}">
              <header><div><b>{esc(row.get('name'))}</b><small>{esc(row.get('code'))} · {esc(row.get('channel'))}</small></div><strong>{esc(f"{row['weekly_score']:.2f}分" if row.get('weekly_score') is not None else '未评分')}</strong></header>
              <div class="quote"><b>{esc(eod.get('close') if eod.get('close') is not None else '不可确认')}</b><span>报告截止日 {esc(eod.get('as_of') or '-')}</span></div>
              <h3>报告截止日</h3><dl><div><dt>本周 / 近1月</dt><dd>{pct(row.get('week_return'))} / {pct(row.get('one_month'))}</dd></div><div><dt>收盘溢价</dt><dd>{pct(eod.get('premium_rate'))}</dd></div><div><dt>溢价口径</dt><dd>{esc(eod.get('premium_basis') or '-')}</dd></div><div><dt>成交额</dt><dd>{money(eod.get('turnover'))}</dd></div><div><dt>收益口径</dt><dd>{esc(row.get('return_basis'))} · 置信度{esc(row.get('return_confidence') or '-')}</dd></div></dl>
              <h3>当前交易快照</h3><dl><div><dt>价格 / IOPV</dt><dd>{esc(live.get('price') if live.get('price') is not None else '不可确认')} / {esc(live.get('iopv') if live.get('iopv') is not None else '不可确认')}</dd></div><div><dt>实时溢价</dt><dd>{pct(live.get('premium_rate'))}</dd></div><div><dt>时间</dt><dd>{esc(live.get('trade_time') or '-')} · {esc('有5分钟内' if live.get('fresh_within_5m') else '需重新复核')}</dd></div></dl>
              <p>{esc('、'.join(flags + actions) or '收盘交易质量门槛通过')} · {esc(row.get('execution_note'))}</p>
            </article>'''
        )
    return "".join(output)


def replacements(comparison: dict[str, Any]) -> str:
    rows = comparison.get("replacement_top3") or []
    if not rows:
        blockers = "".join(f"<li>{esc(item)}</li>" for item in comparison.get("replacement_blockers") or [])
        return f'<div class="empty decision-gap"><b>{esc(comparison.get("replacement_status_display") or "证据不足，暂不生成替换建议")}</b><p>{esc(comparison.get("replacement_note") or "未使用低质量候选补足数量。")}</p><ul>{blockers}</ul></div>'
    output = []
    for index, row in enumerate(rows, 1):
        components = row.get("candidate_score_components") or {}
        output.append(
            f'''<article class="replacement" data-row="replacement">
              <span class="index">#{index}</span><div><small>调出观察</small><b>{esc(row.get('replace_name'))}</b><strong>{esc(row.get('replace_score'))}分</strong></div>
              <div><small>候选</small><b>{esc(row.get('candidate_name'))}</b><strong>{esc(row.get('candidate_score'))}分</strong></div>
              <div><b>分差 +{esc(row.get('score_gap'))}</b><small>{esc(f"首期 {pct(row.get('suggested_first_step_weight') * 100)}" if row.get('suggested_first_step_weight') is not None else '执行前复核实时溢价')}</small></div>
              <p>{esc(row.get('reason'))}<br>{esc('；'.join(row.get('evidence') or []))}<br>风险：{esc('、'.join(row.get('risk_flags') or []) or '门槛内')} · 分项：{esc(components)}</p>
            </article>'''
        )
    return "".join(output)


def quality_rows(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return '<p class="empty">暂无接口状态；可能来自旧缓存结构。</p>'
    return "".join(
        f'''<div class="quality-row" data-row="quality"><b>{esc(row.get('dataset') or row.get('label'))}</b><span class="badge">{esc(status_display(row.get('status')))}</span><span>{esc(row.get('provider') or '来源未标注')} · {esc(row.get('resolved_by') or row.get('function') or '无有效来源')}</span><span>记录 {esc(row.get('record_count') if row.get('record_count') is not None else '未知')}</span><small>{esc(row.get('basis') or row.get('reason') or '')}</small></div>'''
        for row in rows
    )


def conclusion_html(comparison: dict[str, Any]) -> str:
    conclusion = comparison.get("weekly_conclusion") or {}
    paragraphs = [
        conclusion.get("market_summary"),
        conclusion.get("flow_summary"),
        conclusion.get("coverage_summary"),
        conclusion.get("overlap_summary"),
        conclusion.get("decision_summary"),
    ]
    body = "".join(f"<p>{esc(item)}</p>" for item in paragraphs if item)
    confidence = conclusion.get("confidence_note") or "结论仅使用已取得真实周期证据的数据。"
    return body + f'<small class="confidence-note">结论口径：{esc(confidence)}</small>'


def three_week_synthesis(data: dict[str, Any]) -> str:
    synthesis = data.get("llm_synthesis") or {}
    three = data.get("three_week_analysis") or {}
    evidence = three.get("evidence_index") or {}
    refs = []
    for ref in synthesis.get("evidence_refs") or []:
        row = evidence.get(ref)
        if not row:
            continue
        value = flow_money(row.get("value")) if row.get("unit") == "元" else pct(row.get("value")) if row.get("unit") == "%" else row.get("value")
        refs.append(f'<span class="evidence-chip">{esc(row.get("entity_name"))} · {esc(row.get("period"))} · {esc(value)}</span>')
    rotation = "".join(
        f'<div class="rotation-note"><b>{esc(row.get("entity"))}</b><span class="badge">{esc(row.get("state"))}</span><small>{esc(row.get("reason"))}</small></div>'
        for row in synthesis.get("rotation_path") or []
    )
    synthesis_mode = status_display(synthesis.get("status") or "确定性结论")
    return f'''<div class="synthesis-grid"><div><p><b>市场状态：</b>{esc(synthesis.get('market_regime') or '证据不足')}</p><p><b>持续/增强：</b>{esc('、'.join(synthesis.get('persistent_leaders') or []) or '未形成')}</p><p><b>新出现：</b>{esc('、'.join(synthesis.get('emerging_sectors') or []) or '未确认')}</p><p><b>退潮：</b>{esc('、'.join(synthesis.get('fading_sectors') or []) or '未确认')}</p><p><b>置信度：</b>{esc(synthesis.get('confidence') or '低')} · {esc(synthesis_mode)}</p></div><div>{rotation or '<p class="empty">没有满足两周证据门槛的轮动路径。</p>'}</div></div><div class="evidence-list">{''.join(refs) or '<span class="muted">暂无跨周证据引用。</span>'}</div>'''


def three_week_portfolio(three: dict[str, Any]) -> str:
    periods = three.get("periods") or []
    portfolio = three.get("portfolio") or {}
    headers = "".join(f'<span>{esc(period.get("label"))}<small>{esc(period.get("start_date"))}至{esc(period.get("end_date"))} · {"完整" if period.get("completeness") == "complete" else "进行中"}</small></span>' for period in periods)
    rows = []
    for fund in portfolio.get("funds") or []:
        cells = "".join(f'<span class="{tone((fund.get("periods") or {}).get(period.get("period_id"), {}).get("return"))}">{pct((fund.get("periods") or {}).get(period.get("period_id"), {}).get("return"))}<small>贡献 {pct((fund.get("periods") or {}).get(period.get("period_id"), {}).get("contribution"))}</small></span>' for period in periods)
        rows.append(f'<div class="three-row" data-row="three-week-fund"><b>{esc(fund.get("name"))}<small>轨迹：{esc(fund.get("trajectory_state") or "证据不足")}</small></b>{cells}</div>')
    summary_cells = "".join(f'<span class="{tone((portfolio.get("weekly_returns") or {}).get(period.get("period_id")))}">{pct((portfolio.get("weekly_returns") or {}).get(period.get("period_id")))}<small>覆盖 {pct((portfolio.get("coverage") or {}).get(period.get("period_id"), 0) * 100)}</small></span>' for period in periods)
    return f'<div class="three-head"><b>组合/基金</b>{headers}</div><div class="three-row portfolio-total"><b>当前组合</b>{summary_cells}</div>{"".join(rows)}'


def three_week_styles(three: dict[str, Any]) -> str:
    periods = three.get("periods") or []
    rows = []
    for style in three.get("styles") or []:
        cells = "".join(f'<span class="heat {tone((style.get("periods") or {}).get(period.get("period_id"), {}).get("return"))}">{pct((style.get("periods") or {}).get(period.get("period_id"), {}).get("return"))}<small>#{esc((style.get("periods") or {}).get(period.get("period_id"), {}).get("rank") or "-")}</small></span>' for period in periods)
        rows.append(f'<div class="three-row" data-row="three-week-style"><b>{esc(style.get("name"))}</b>{cells}</div>')
    return "".join(rows) or '<p class="empty">风格指数不足，无法比较三周。</p>'


def _sparkline(rows: list[dict[str, Any]], field: str, label: str, value_kind: str = "money") -> str:
    points = [(row.get("trade_date"), row.get(field)) for row in rows if row.get(field) is not None]
    if len(points) < 2:
        return f'<p class="empty">{esc(label)}历史样本不足，暂不绘制趋势。</p>'
    values = [float(value) for _day, value in points]
    low, high = min(values), max(values)
    span = high - low or 1.0
    coords = []
    for index, (_day, value) in enumerate(points):
        x = index / max(1, len(points) - 1) * 1000
        y = 118 - (float(value) - low) / span * 100
        coords.append(f"{x:.1f},{y:.1f}")
    def value_text(value: float) -> str:
        if value_kind == "pct":
            return pct(value)
        if value_kind == "index":
            return f"{value:,.2f}"
        return money_yi(value)

    return (
        f'<figure class="spark"><svg viewBox="0 0 1000 136" role="img" aria-label="{esc(label)}轨迹">'
        f'<polyline points="{esc(" ".join(coords))}" fill="none" stroke="currentColor" stroke-width="5" vector-effect="non-scaling-stroke"/>'
        f'</svg><figcaption>{esc(label)} · {esc(points[0][0])} 至 {esc(points[-1][0])} · 区间 {esc(value_text(low))} 至 {esc(value_text(high))}</figcaption></figure>'
    )


def margin_leverage_html(margin: dict[str, Any], three: dict[str, Any]) -> str:
    current = margin.get("current") or {}
    norm = margin.get("normalization") or {}
    history = margin.get("history_position") or {}
    heat = margin.get("heat") or {}
    pressure = margin.get("deleveraging_pressure") or {}
    regime = margin.get("regime") or {}
    guide = margin.get("metric_guide") or {}
    status = margin.get("status") or "degraded"
    broad_index_name = margin.get("broad_index_name") or "中证全指"
    ratio_observations = history.get("ratio_history_observations")
    ratio_window = (
        f"样本 {ratio_observations}日 · {history.get('ratio_history_start')} 至 {history.get('ratio_history_end')}"
        if ratio_observations else "历史样本待补"
    )
    full_history_note = "全历史已覆盖" if history.get("full_ratio_history_available") else "全历史尚未覆盖2014起点"
    cards = "".join([
        f'<div><small>当前两融余额</small><b>{esc(money_yi(current.get("margin_balance")))}</b><span>融资 {esc(money_yi(current.get("financing_balance")))} · 融券 {esc(money_yi(current.get("lending_balance")))}</span></div>',
        f'<div><small>距离全历史峰值</small><b>{esc(pct(history.get("peak_gap_pct")))}</b><span>峰值恢复度 {esc(pct(history.get("peak_recovery_pct")))} · 峰值日 {esc(history.get("peak_date") or "数据不足")}</span></div>',
        f'<div><small>融资杠杆密度</small><b>{esc(pct(norm.get("financing_to_float_cap")))}</b><span>滚动5年分位（近5年窗口） {esc(pct(history.get("financing_density_5y_percentile")))} · {esc(ratio_window)}</span><span>{esc(full_history_note)}</span></div>',
        f'<div><small>融资交易强度</small><b>{esc(pct(norm.get("financing_buy_to_turnover")))}</b><span>滚动5年分位（近5年窗口） {esc(pct(history.get("financing_intensity_5y_percentile")))} · {esc(ratio_window)}</span><span>{esc(full_history_note)}</span></div>',
    ])
    gauges = "".join([
        f'<div class="gauge"><span><b>杠杆热度</b><strong>{esc(score_text(heat.get("score")))} · {esc(heat.get("label") or "数据不足")}</strong></span><div class="gauge-track"><i style="width:{max(0,min(100,float(heat.get("score") or 0))):.1f}%"></i></div><small>评分覆盖率 {esc(pct((heat.get("coverage") or 0) * 100))}；反映水位，不等同顶部判断。</small></div>',
        f'<div class="gauge pressure"><span><b>去杠杆压力</b><strong>{esc(score_text(pressure.get("score")))} · {esc(pressure.get("label") or "数据不足")}</strong></span><div class="gauge-track"><i style="width:{max(0,min(100,float(pressure.get("score") or 0))):.1f}%"></i></div><small>评分覆盖率 {esc(pct((pressure.get("coverage") or 0) * 100))}；反映负反馈压力，不等同必然下跌。</small></div>',
    ])
    three_rows = []
    period_labels = {row.get("period_id"): row for row in (three.get("periods") or [])}
    for row in ((three.get("margin_leverage") or {}).get("periods") or []):
        period = period_labels.get(row.get("period_id"), {})
        three_rows.append(
            f'<div class="margin-week"><b>{esc(period.get("label") or row.get("period_id"))}</b><span><small>周末融资余额</small>{esc(money_yi(row.get("end_financing_balance")))}</span><span><small>本周变化</small>{esc(pct(row.get("financing_balance_change")))}</span><span><small>平均融资交易强度</small>{esc(pct(row.get("average_financing_intensity")))}</span><span><small>杠杆热度</small>{esc(score_text(row.get("heat_score")))} · {esc(row.get("heat_label") or "数据不足")}</span><span><small>去杠杆压力</small>{esc(score_text(row.get("deleveraging_pressure_score")))} · {esc(row.get("deleveraging_pressure_label") or "数据不足")}</span><span><small>状态</small>{esc(status_display(row.get("data_status")))}</span></div>'
        )
    comparisons = "".join(
        f'<tr><th>{esc(row.get("label"))}</th><td>{esc(money_yi(row.get("peak_margin_balance")))}</td><td>{esc(row.get("peak_date") or "-")}</td><td>{esc(pct(row.get("current_vs_peak_pct")))}</td><td>{esc(pct(row.get("peak_financing_to_float_cap")))}<small>{esc(row.get("peak_financing_to_float_cap_date") or "")}</small></td><td>{esc(pct(row.get("peak_financing_buy_to_turnover")))}<small>{esc(row.get("peak_financing_buy_to_turnover_date") or "")}</small></td><td>{esc(pct(row.get("fastest_20d_financing_growth")))}</td><td>{esc(pct(row.get("post_peak_20d_max_drawdown")))}</td><td>{esc(pct(row.get("post_peak_60d_max_drawdown")))}</td></tr>'
        for row in margin.get("historical_comparisons") or []
    )
    calibration = margin.get("calibration") or {}
    calibration_rows = "".join(
        f'<tr><th>{esc(row.get("band"))}</th><td>{esc(row.get("sample_count"))}</td><td>{esc(status_display(row.get("status")))}</td><td>{esc(pct(row.get("median_20d_max_drawdown")))}</td><td>{esc(pct(row.get("event_20d_le_5pct_rate") * 100) if row.get("event_20d_le_5pct_rate") is not None else "样本不足")}</td><td>{esc(pct(row.get("median_60d_max_drawdown")))}</td><td>{esc(pct(row.get("event_60d_le_10pct_rate") * 100) if row.get("event_60d_le_10pct_rate") is not None else "样本不足")}</td></tr>'
        for row in calibration.get("heat_bands") or []
    )
    policies = "".join(
        f'<li><b>{esc(row.get("date"))} · {esc(row.get("label"))}</b><span>{esc(row.get("description"))}</span></li>'
        for row in margin.get("policy_events") or []
    )
    quality = "".join(f"<li>{esc(item)}</li>" for item in margin.get("data_quality") or []) or "<li>两融与同日市场规模数据通过基础校验。</li>"
    density_guide = guide.get("financing_leverage_density") or {}
    intensity_guide = guide.get("financing_trading_intensity") or {}
    heat_guide = guide.get("leverage_heat") or {}
    pressure_guide = guide.get("deleveraging_pressure") or {}
    explanations = f'''<div class="metric-guide">
        <div><b>融资杠杆密度是什么？</b><p>{esc(density_guide.get('definition') or '融资余额 / 沪深A股流通市值')}。{esc(density_guide.get('meaning') or '反映存量市值中的杠杆参与程度。')}</p><strong>高低含义：{esc(density_guide.get('direction') or '不是越高越好；偏高意味着杠杆参与更深，偏低只表示杠杆参与较少。')}</strong></div>
        <div><b>融资交易强度是什么？</b><p>{esc(intensity_guide.get('definition') or '融资买入额 / 沪深A股成交额')}。{esc(intensity_guide.get('meaning') or '反映当天成交中的融资买盘参与度。')}</p><strong>高低含义：{esc(intensity_guide.get('direction') or '不是越高越好；偏高表示融资更活跃，也可能更拥挤。')}</strong></div>
        <div><b>杠杆热度怎么读？</b><p>{esc(heat_guide.get('direction') or '低分表示杠杆水位低，高分表示杠杆水位高；没有单独的好坏方向。')}</p><span>{esc(heat_guide.get('bands') or '0–20低杠杆，20–70正常，70以上逐步升温。')}</span></div>
        <div><b>去杠杆压力怎么读？</b><p>{esc(pressure_guide.get('direction') or '通常越低越平稳、越高风险越大，但不能单独预测涨跌。')}</p><span>{esc(pressure_guide.get('bands') or '0–30平稳，30–60观察，60以上逐步进入降温或去杠杆。')}</span></div>
      </div>'''
    return f'''<div class="margin-kpis">{cards}</div>{explanations}<div class="dual-gauge">{gauges}</div>
      <div class="margin-regime"><b>二维状态：{esc(regime.get('label') or '数据不足')}</b><p>{esc(regime.get('explanation') or '缺少同口径历史，暂不判断。')}</p><small>截至 {esc(margin.get('as_of') or '-')} · {esc(status_display(status))} · 仅作市场环境展示，不改变基金评分、Top3、目标配比或调仓动作。</small></div>
      <h3>近60日杠杆与市场轨迹</h3><div class="margin-trends">
        {_sparkline(margin.get('series') or [], 'margin_balance', '近60日两融余额')}
        {_sparkline(margin.get('series') or [], 'financing_to_float_cap', '近60日融资杠杆密度', 'pct')}
        {_sparkline(margin.get('broad_index_series') or [], 'close', f'近60日宽基代表（{broad_index_name}）', 'index')}
      </div>
      <h3>最近三周</h3><div class="margin-weeks">{''.join(three_rows) or '<p class="empty">三周两融轨迹不足。</p>'}</div>
      <h3>历史阶段同口径比较</h3><div class="table-scroll"><table class="margin-table"><thead><tr><th>阶段</th><th>余额峰值</th><th>峰值日</th><th>当前距峰值</th><th>杠杆密度峰值</th><th>交易强度峰值</th><th>20日最快扩张</th><th>峰后20日最大回撤</th><th>峰后60日最大回撤</th></tr></thead><tbody>{comparisons or '<tr><td colspan="9">历史样本不足，暂不比较。</td></tr>'}</tbody></table></div>
      <h3>走步法历史校准</h3>{f'<p class="muted">校准截止 {esc(calibration.get("end_date"))} · 样本 {esc(calibration.get("observation_count"))} · 每个历史日只使用此前最多5年数据。</p><div class="table-scroll"><table class="margin-table"><thead><tr><th>热度区间</th><th>样本数</th><th>样本状态</th><th>未来20日回撤中位数</th><th>20日风险事件率</th><th>未来60日回撤中位数</th><th>60日风险事件率</th></tr></thead><tbody>{calibration_rows}</tbody></table></div>' if calibration_rows else '<p class="empty">尚未生成同口径走步校准；不发布历史风险概率。</p>'}
      <div class="margin-notes"><div><h3>政策与口径节点</h3><ul>{policies}</ul></div><div><h3>数据限制</h3><ul>{quality}</ul><p>低杠杆不代表上涨空间必然较大；高杠杆也不代表市场立即见顶。需要同时观察价格、盈利、成交和去杠杆压力。</p></div></div>'''


def rotation_matrix(rows: list[dict[str, Any]], periods: list[dict[str, Any]], row_type: str) -> str:
    if not rows:
        return '<p class="empty">逐日历史覆盖不足，无法形成三周矩阵。</p>'
    selected = select_rotation_rows(rows, periods)
    output = []
    for row in selected:
        cells = []
        for period in periods:
            values = (row.get("periods") or {}).get(period.get("period_id"), {})
            cells.append(f'<span class="heat"><b class="{tone(values.get("return"))}">{pct(values.get("return"))}</b><small class="{tone(values.get("weekly_net_flow"))}">{flow_money(values.get("weekly_net_flow"))}</small></span>')
        coverage = row.get("portfolio_coverage") or "覆盖待核验"
        coverage_detail = f"{coverage} {pct((row.get('coverage_weight') or 0) * 100)}"
        output.append(f'<div class="three-row" data-row="{esc(row_type)}"><b>{esc(row.get("name"))}<small>{esc(row.get("rotation_state"))} · {esc(row.get("monitor_state"))}</small><small>{esc(coverage_detail)}</small></b>{"".join(cells)}</div>')
    return "".join(output)


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
    cache_stats = ((data.get("cache") or {}).get("stats") or {})
    current = comparison.get("current_vs_weekly") or portfolio.get("funds") or []
    styles = market.get("style_indexes") or []
    valid_funds = [row for row in current if row.get("week_return") is not None]
    valid_styles = [row for row in styles if row.get("week_return") is not None]
    best = max(valid_funds, key=lambda row: row["week_return"], default={})
    worst = min(valid_funds, key=lambda row: row["week_return"], default={})
    best_style = max(valid_styles, key=lambda row: row["week_return"], default={})
    high_premium = [row for row in data.get("candidate_etfs") or [] if row.get("premium_rate") is not None and row["premium_rate"] >= 2]
    style_regime = three.get("style_regime") or {}
    current_risk_count = sum(
        row.get("monitor_state") == "进行中转弱预警"
        or (row.get("rotation_state") in {"退潮", "持续流出"} and row.get("monitor_state") != "进行中修复观察")
        for row in three.get("industries") or []
    )
    covered = "、".join(comparison.get("covered_themes") or []) or "未发现与本周领涨方向一致的可验证覆盖"
    missing = "、".join(comparison.get("missing_themes") or []) or "未识别出明确缺失方向"
    overlap = "、".join(comparison.get("overlap_risk") or []) or "暂未识别出三只及以上基金共同暴露的主题"
    warnings = data.get("warnings") or []
    warning_html = "".join(f"<li>{esc(item)}</li>" for item in warnings) or "<li>暂无未解决的数据缺口。</li>"
    note_html = "".join(f"<li>{esc(item)}</li>" for item in data.get("analysis_notes") or []) or "<li>暂无额外分析说明。</li>"
    quality = data.get("data_quality") or []
    recovered = [row for row in quality if row.get("status") == "fallback_used"]
    unresolved = [row for row in quality if row.get("requirement", "required") == "required" and row.get("status") in {"failed", "partial"}]
    optional_unavailable = [row for row in quality if row.get("requirement") == "optional" and row.get("status") in {"failed", "partial", "optional_unavailable"}]
    provider_route = data.get("provider_route") or {}
    provider_note = "；".join(filter(None, [
        f"策略：{data.get('provider_policy') or 'akshare-only'}",
        f"本次选用：{provider_route.get('selected_provider')}" if provider_route.get("selected_provider") else None,
        f"已晋级数据集：{'、'.join(provider_route.get('promoted_datasets') or [])}" if provider_route.get("promoted_datasets") else "本次没有代理数据集参与报告结论",
        provider_route.get("credential_risk"),
    ]))
    report_format = data.get("report_format_version") or REPORT_FORMAT_VERSION
    holdings_fingerprint = str(data.get("holdings_hash") or "未记录")[:12]
    evidence_fingerprint = str(data.get("llm_evidence_hash") or "未记录")[:12]
    nav_html = "".join(f'<a href="#{esc(anchor)}">{esc(label)}</a>' for anchor, label in NAV_ITEMS)

    document = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="fund-report-format" content="{esc(report_format)}"><meta name="color-scheme" content="light"><title>{esc(REPORT_TITLE)}</title>
<style>
:root{{--bg:#f4f6f8;--card:#fff;--ink:#172033;--muted:#687386;--line:#dfe4ea;--pos:#087f5b;--neg:#c92a2a;--blue:#2457a6;--warn:#b76b00;--risk:#a61e4d}}*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;letter-spacing:0}}main{{max-width:1320px;margin:auto;padding:24px}}h1{{font-size:28px;margin:0 0 6px}}h2{{font-size:18px;margin:0 0 14px;overflow-wrap:anywhere}}h3{{font-size:15px;margin:18px 0 10px}}small,.muted{{color:var(--muted)}}.grid{{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:14px;margin:14px 0}}.span-2{{grid-column:span 2}}.span-4{{grid-column:span 4}}.span-5{{grid-column:span 5}}.span-6{{grid-column:span 6}}.span-7{{grid-column:span 7}}.span-12{{grid-column:span 12}}.card,.etf-card,.replacement{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:16px;min-width:0}}.metric span,.metric small{{display:block}}.metric b{{display:block;font-size:24px;margin:7px 0}}.positive{{color:var(--pos)}}.negative{{color:var(--neg)}}.neutral{{color:var(--muted)}}.summary{{border-left:4px solid var(--blue)}}.summary p{{margin:7px 0;line-height:1.65}}.confidence-note{{display:block;margin-top:10px;padding-top:10px;border-top:1px solid var(--line)}}.data-row,.style-row,.quality-row{{display:grid;align-items:center;gap:8px;padding:10px 0;border-bottom:1px solid var(--line);min-width:0}}.data-row>*,.style-row>*,.quality-row>*{{min-width:0;overflow-wrap:anywhere}}.data-row:last-child,.style-row:last-child,.quality-row:last-child{{border:0}}.holding-head,.holding-row{{display:grid;grid-template-columns:minmax(230px,1.6fr) repeat(6,minmax(72px,.55fr)) minmax(88px,.65fr);gap:8px;align-items:center}}.holding-head{{padding:9px 0;border-bottom:2px solid var(--line);font-size:12px;color:var(--muted);font-weight:700}}.holding-name small,.holding-field small,.holding-action small{{display:block}}.holding-field b{{display:block;margin-top:3px}}.holding-action .badge{{margin-top:4px}}.holding-reason{{grid-column:2/-1;padding:3px 0 7px;line-height:1.5}}.sector-row{{grid-template-columns:minmax(170px,1.35fr) repeat(6,minmax(72px,.62fr))}}.sector-row span small,.sector-row span b{{display:block}}.sector-evidence{{grid-column:2/-1;line-height:1.5}}.rank-row{{grid-template-columns:38px 1fr 70px 90px}}.proxy-row{{grid-template-columns:1fr 80px 80px minmax(220px,1.4fr)}}.style-row{{grid-template-columns:100px 1fr 70px minmax(280px,1.4fr)}}.track{{height:10px;background:#edf0f3;border-radius:5px;overflow:hidden}}.track i{{display:block;height:100%;background:var(--pos)}}.track i.negative{{background:var(--neg)}}.badge{{display:inline-flex;justify-content:center;padding:4px 7px;border-radius:6px;background:#eef3fb;color:var(--blue);font-size:12px}}.etf-grid{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}}.etf-card.ok{{border-color:#9bd8c5}}.etf-card.warn{{border-color:#f0c36a}}.etf-card.risk{{border-color:#e39aad}}.etf-card header{{display:flex;justify-content:space-between;gap:8px}}.etf-card header small{{display:block;margin-top:3px}}.quote{{margin:14px 0}}.quote b{{font-size:25px;margin-right:10px}}dl{{margin:0;display:grid;gap:7px}}dl div{{display:flex;justify-content:space-between;gap:10px}}dt{{color:var(--muted)}}dd{{margin:0;font-weight:650;text-align:right;overflow-wrap:anywhere}}.replacement{{display:grid;grid-template-columns:34px 1fr 1fr 140px;gap:12px;align-items:center;margin:10px 0}}.replacement div>*{{display:block}}.replacement p{{grid-column:2/-1;margin:0;color:var(--muted);line-height:1.55}}.index{{font-weight:800}}.difference{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}}.difference div{{padding:12px;background:#f7f9fb;border:1px solid var(--line);border-radius:6px}}.empty{{padding:14px;background:#f8fafc;border:1px dashed var(--line);color:var(--muted)}}.decision-gap{{border-color:#d7a94b;color:#765100}}.quality-row{{grid-template-columns:minmax(220px,1fr) 150px 110px 90px minmax(200px,1fr)}}details{{border:1px solid var(--line);border-radius:6px;padding:10px 12px}}summary{{cursor:pointer;font-weight:700;color:var(--blue)}}ul{{line-height:1.6}}.three-head,.three-row{{display:grid;grid-template-columns:minmax(180px,1.35fr) repeat(3,minmax(110px,1fr));gap:8px;align-items:center;padding:10px 0;border-bottom:1px solid var(--line)}}.three-head{{font-size:12px;color:var(--muted);font-weight:700}}.three-head span small,.three-row span small,.three-row b small{{display:block;margin-top:3px}}.portfolio-total{{background:#f7f9fb;padding:10px;border-radius:6px;font-size:16px}}.heat{{padding:8px;border-left:3px solid #dfe4ea;background:#fafbfc}}.heat.positive{{border-color:var(--pos)}}.heat.negative{{border-color:var(--neg)}}.synthesis-grid{{display:grid;grid-template-columns:1fr 1.4fr;gap:16px}}.rotation-note{{display:grid;grid-template-columns:minmax(100px,1fr) auto 2fr;gap:8px;padding:8px 0;border-bottom:1px solid var(--line)}}.evidence-list{{display:flex;gap:7px;flex-wrap:wrap;margin-top:12px}}.evidence-chip{{padding:5px 8px;background:#eef3fb;color:var(--blue);border-radius:5px;font-size:12px}}
p{{overflow-wrap:anywhere}}
.sector-row{{grid-template-columns:minmax(140px,1.25fr) repeat(6,minmax(54px,.55fr))}}
.span-3{{grid-column:span 3}}
.skip-link{{position:fixed;left:12px;top:-60px;z-index:10;background:#fff;color:var(--blue);padding:10px;border:2px solid var(--blue);border-radius:6px}}.skip-link:focus{{top:12px}}.report-nav{{display:flex;gap:6px;overflow-x:auto;padding:10px 0 4px;position:sticky;top:0;z-index:5;background:rgba(244,246,248,.96);border-bottom:1px solid var(--line)}}.report-nav a{{color:var(--blue);text-decoration:none;white-space:nowrap;padding:7px 9px;border-radius:5px;font-size:13px}}.report-nav a:hover,.report-nav a:focus{{background:#e7edf7;outline:2px solid transparent}}.report-meta{{display:flex;flex-wrap:wrap;gap:7px;margin-top:10px}}.report-meta span{{padding:4px 7px;border:1px solid var(--line);border-radius:5px;color:var(--muted);font-size:12px}}.grid{{scroll-margin-top:58px}}
.margin-kpis{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}}.margin-kpis>div{{padding:13px;background:#f7f9fb;border:1px solid var(--line);border-radius:6px}}.margin-kpis small,.margin-kpis b,.margin-kpis span{{display:block}}.margin-kpis b{{font-size:21px;margin:6px 0}}.metric-guide{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin:14px 0}}.metric-guide>div{{padding:13px;border:1px solid var(--line);border-radius:6px;background:#fff}}.metric-guide p{{margin:7px 0;line-height:1.55;color:var(--muted)}}.metric-guide strong,.metric-guide span{{display:block;font-size:13px;line-height:1.55}}.dual-gauge{{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin:16px 0}}.gauge{{padding:14px;border:1px solid var(--line);border-radius:6px}}.gauge>span{{display:flex;justify-content:space-between;gap:10px}}.gauge-track{{height:12px;background:#edf0f3;border-radius:6px;overflow:hidden;margin:10px 0}}.gauge-track i{{display:block;height:100%;background:var(--warn)}}.gauge.pressure .gauge-track i{{background:var(--risk)}}.margin-regime{{padding:14px;border-left:4px solid var(--blue);background:#f7f9fb}}.margin-regime p{{margin:6px 0;line-height:1.6}}.margin-trends{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}}.spark{{margin:8px 0;color:var(--blue);min-width:0}}.spark svg{{display:block;width:100%;height:138px;background:#fafbfc;border:1px solid var(--line);border-radius:6px}}.spark figcaption{{margin-top:5px;color:var(--muted);font-size:12px;overflow-wrap:anywhere}}.margin-weeks{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}}.margin-week{{padding:12px;border:1px solid var(--line);border-radius:6px}}.margin-week>span{{display:flex;justify-content:space-between;gap:8px;padding-top:7px}}.margin-week small{{display:block}}.table-scroll{{overflow-x:auto}}.margin-table{{width:100%;border-collapse:collapse;min-width:780px}}.margin-table th,.margin-table td{{padding:9px;text-align:left;border-bottom:1px solid var(--line);font-size:13px}}.margin-table td small{{display:block;margin-top:2px}}.margin-notes{{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:12px}}.margin-notes li span{{display:block;color:var(--muted);margin:2px 0 7px}}.margin-notes p{{line-height:1.6;color:var(--muted)}}
@media(max-width:900px){{main{{padding:14px}}h1{{font-size:24px}}h2{{font-size:15px;word-break:break-all;overflow-wrap:anywhere;white-space:normal;width:100%;max-width:100%;min-width:0}}.report-nav{{margin:0 -14px;padding:8px 14px}}.grid{{grid-template-columns:1fr}}.span-2,.span-3,.span-4,.span-5,.span-6,.span-7,.span-12{{grid-column:span 1}}.holding-head{{display:none}}.holding-row,.sector-row,.rank-row,.proxy-row,.style-row,.quality-row,.replacement{{grid-template-columns:1fr}}.holding-field,.holding-action{{display:flex;justify-content:space-between;align-items:center;padding:4px 0}}.holding-field small,.holding-action small{{display:block}}.holding-field b,.holding-action .badge{{margin:0}}.holding-reason,.sector-evidence{{grid-column:auto}}.etf-grid,.difference,.synthesis-grid,.margin-kpis,.metric-guide,.dual-gauge,.margin-trends,.margin-weeks,.margin-notes{{grid-template-columns:1fr}}.replacement p{{grid-column:auto}}.data-row{{padding:13px 0}}.three-head{{display:none}}.three-row{{grid-template-columns:1fr}}.three-row>span{{display:flex;justify-content:space-between;align-items:center}}.rotation-note{{grid-template-columns:1fr auto}}.rotation-note small{{grid-column:1/-1}}.gauge>span{{align-items:flex-start;flex-direction:column}}}}
@media print{{@page{{size:A4;margin:12mm}}body{{background:#fff;color:#000}}main{{max-width:none;padding:0}}.skip-link,.report-nav{{display:none}}.grid{{gap:8px;margin:8px 0}}.card,.etf-card,.replacement{{box-shadow:none;break-inside:avoid;border-color:#aaa}}details{{display:none}}a{{color:inherit;text-decoration:none}}}}
</style></head><body data-report-format="{esc(report_format)}"><a class="skip-link" href="#report-main">跳到报告正文</a><main id="report-main">
<header><h1>{esc(REPORT_TITLE)}</h1><p class="muted">Schema v{esc(data.get('schema_version'))} / 数据修订 {esc(data.get('data_revision') or 'legacy')} · 三周窗口 {esc((periods[0] if periods else {}).get('start_date') or week.get('start_date'))} → {esc(week.get('end_date'))} · {esc(status_display(week.get('period_mode')))}/{esc('完整周' if week.get('completeness') == 'complete' else '进行中周')} · {esc(portfolio.get('weight_basis_display') or portfolio.get('weight_basis'))}</p><div class="report-meta"><span>格式 {esc(report_format)}</span><span>持仓快照 {esc(holdings_fingerprint)}</span><span>证据 {esc(evidence_fingerprint)}</span><span>生成时间 {esc(data.get('as_of') or '-')}</span></div></header>
<nav class="report-nav" aria-label="报告导航">{nav_html}</nav>
<section class="grid" data-section="kpi">{metric('组合本周',pct(portfolio.get('weekly_return')),'进行中周仅作监测',tone(portfolio.get('weekly_return')))}{metric('截至当前复合',pct(three_portfolio.get('three_week_compound_return')),f"含进行中周；完整周复合 {pct(three_portfolio.get('completed_weeks_compound_return'))}",tone(three_portfolio.get('three_week_compound_return')))}{metric('净值覆盖率',pct((portfolio.get('nav_coverage_weight') or 0)*100),status_display(portfolio.get('return_status')),tone((portfolio.get('nav_coverage_weight') or 0)-.9))}{metric('本周监测风格',str(style_regime.get('current_regime') or '数据不足'),f"动作依据：{style_regime.get('action_regime') or '数据不足'}")}{metric('持续主线',str(sum(row.get('rotation_state') in {'持续主线','加速','新启动'} and row.get('monitor_state') in {'进行中延续','无进行中周'} for row in three.get('industries') or [])),'完整周确认且本周未转弱')}{metric('当前转弱/退潮',f"{current_risk_count}/{len(three.get('industries') or [])}",'行业全量；已排除进行中修复')}{metric('历史缓存命中',pct((cache_stats.get('historical_hit_rate') if cache_stats.get('historical_hit_rate') is not None else cache_stats.get('hit_rate') or 0)*100),'实时行情不计入分母')}{metric('真实缺口',str(len(unresolved)),'全部来源失败','negative' if unresolved else 'positive')}</section>
<section class="grid" data-section="llm-synthesis"><article class="card span-12 summary"><h2>三周综合判断</h2>{three_week_synthesis(data)}</article></section>
<section class="grid" data-section="three-week-portfolio"><article class="card span-12"><h2>三周组合与持仓轨迹</h2><p class="muted">W0进行中时只用于监测，正式调仓依据来自两个完整周。“截至当前复合”包含进行中周，“完整周复合”才是动作证据；两者都要求对应周净值覆盖率达到90%。</p>{three_week_portfolio(three)}</article></section>
<section class="grid" data-section="three-week-style"><article class="card span-12"><h2>风格三周收益与排名</h2>{three_week_styles(three)}</article></section>
<section class="grid" data-section="margin-leverage"><article class="card span-12"><h2>A股杠杆温度</h2><p class="muted">长期比较主体为沪深市场，北交所单列；总两融余额用于展示，融资余额用于评分。该模块只作市场环境展示，不参与基金评分或调仓。</p>{margin_leverage_html(margin,three)}</article></section>
<section class="grid"><article class="card span-6" data-section="three-week-industry"><h2>行业三周收益/资金热力图</h2><p class="muted">每格上方为周收益，下方为该自然交易周主力净流入；展示三周收益Top5、资金Top5与当前持仓相关行业的并集，最多15个。</p>{rotation_matrix(three.get('industries') or [],periods,'three-week-industry')}</article><article class="card span-6" data-section="three-week-concept"><h2>概念三周收益/资金热力图</h2><p class="muted">同样采用三周并集；缺少完整交易日的周保持空白，不用今日快照或基金热度补齐。</p>{rotation_matrix(three.get('concepts') or [],periods,'three-week-concept')}</article></section>
<section class="grid"><article class="card span-12 summary"><h2>本周结论</h2>{conclusion_html(comparison)}</article></section>
<section class="grid" data-section="holdings"><article class="card span-12"><h2>持仓本周表现</h2><p class="muted">{esc(str(portfolio.get('weight_assumption') or portfolio.get('weight_basis_display') or '按输入权重分析').rstrip('。'))}。当前组合占比是本报告的分析口径，不代表券商账户实时仓位。近1年最大回撤表示过去一年从阶段高点到低点的最大跌幅，负值越大风险越高。</p>{holding_rows(current)}</article></section>
<section class="grid" data-section="style"><article class="card span-12"><h2>大盘与风格</h2>{style_rows(styles)}</article></section>
<section class="grid" data-section="sector-week"><article class="card span-6"><h2>板块Top10：行业近5个交易日收益</h2><p class="muted">这是截止报告日向前5个交易日的滚动观察；自然周收益请以三周行业矩阵为准。</p>{sector_rows(sectors.get('industry_return') or [])}</article><article class="card span-6"><h2>板块Top10：概念近5个交易日收益</h2><p class="muted">这是截止报告日向前5个交易日的滚动观察；自然周收益请以三周概念矩阵为准。</p>{sector_rows(sectors.get('concept_return') or [])}</article></section>
<section class="grid" data-section="sector-today"><article class="card span-6"><h2>今日涨跌：行业（非周收益）</h2>{sector_rows(sectors.get('industry_today') or [],'today_return')}</article><article class="card span-6"><h2>{esc('最近有效收盘概念行情' if sectors.get('concept_snapshot_kind') == 'latest_close' else '今日涨跌：概念（非周收益）')}</h2><p class="muted">{esc(sectors.get('concept_snapshot_date') or week.get('collection_trade_date') or '-')} · 不参与周收益计算</p>{sector_rows(sectors.get('concept_today') or [],'today_return')}</article></section>
<section class="grid" data-section="sector-today-flow"><article class="card span-6"><h2>报告期后当日资金流入（{esc(week.get('collection_trade_date'))}，不参与上周结论）</h2>{sector_rows((sectors.get('industry_today_inflow') or [])[:5]+(sectors.get('concept_today_inflow') or [])[:5],'today_return')}</article><article class="card span-6"><h2>报告期后当日资金流出（{esc(week.get('collection_trade_date'))}，不参与上周结论）</h2>{sector_rows((sectors.get('industry_today_outflow') or [])[:5]+(sectors.get('concept_today_outflow') or [])[:5],'today_return')}</article></section>
<section class="grid" data-section="flows"><article class="card span-6"><h2>报告期5日资金流入（截至 {esc(week.get('end_date'))}）</h2>{sector_rows((sectors.get('industry_inflow') or [])[:5]+(sectors.get('concept_inflow') or [])[:5])}</article><article class="card span-6"><h2>报告期5日资金流出（截至 {esc(week.get('end_date'))}）</h2>{sector_rows((sectors.get('industry_outflow') or [])[:5]+(sectors.get('concept_outflow') or [])[:5])}</article></section>
<section class="grid" data-section="difference"><article class="card span-12"><h2>板块与持仓差异</h2><div class="difference"><div><b>已覆盖</b><p>{esc(covered)}</p></div><div><b>未覆盖方向（非买入建议）</b><p>{esc(missing)}</p></div><div><b>重复/拥挤</b><p>{esc(overlap)}</p></div></div></article></section>
<section class="grid" data-section="proxy"><article class="card span-5"><h2>基金主题热度代理</h2>{proxy_rows(sectors.get('theme_signal_proxy') or [])}</article><article class="card span-7"><h2>近1周基金排行</h2>{top_funds(market.get('weekly_top_funds') or [])}</article></section>
<section class="grid" data-section="etf"><article class="card span-12"><h2>ETF交易质量</h2><div class="etf-grid">{etf_cards(data.get('candidate_etfs') or [])}</div></article></section>
<section class="grid" data-section="replacement"><article class="card span-12"><h2>Top3替换观察</h2>{replacements(comparison)}</article></section>
<section class="grid" data-section="cache-audit"><article class="card span-12"><h2>增量缓存</h2><p>数据库：{esc((data.get('cache') or {}).get('database') or '本次未启用共享缓存')}</p><p class="muted">历史逻辑数据集 {esc(cache_stats.get('historical_logical_datasets') or 0)} · 命中 {esc(cache_stats.get('historical_logical_hits') or 0)} · 命中率 {pct((cache_stats.get('historical_hit_rate') or 0)*100)}；实时行情不计入该分母。底层调用命中率 {pct((cache_stats.get('hit_rate') or 0)*100)}。历史时序、实时快照与分析模型版本分离保存。</p></article></section>
<section class="grid" data-section="quality"><article class="card span-12"><h2>未解决的必需数据</h2><ul>{warning_html}</ul><h3>可选实时数据</h3>{quality_rows(optional_unavailable)}<h3>数据来源路由</h3><p class="muted">{esc(provider_note)}</p><h3>分析说明</h3><ul>{note_html}</ul><h3>已自动恢复的数据源</h3>{quality_rows(recovered)}<h3>逻辑数据集状态</h3>{quality_rows(quality)}<details><summary>查看全部 {len(data.get('source_audit') or [])} 条接口审计记录</summary>{quality_rows(data.get('source_audit') or [])}</details><p class="muted">{esc(data.get('disclaimer'))}</p></article></section>
</main></body></html>'''
    document = document.replace(
        '<section class="grid"><article class="card span-12 summary"><h2>本周结论</h2>',
        '<section class="grid" data-section="weekly-conclusion"><article class="card span-12 summary"><h2>本周结论</h2>',
        1,
    )
    anchors = {
        "kpi": "overview",
        "three-week-portfolio": "portfolio",
        "three-week-style": "rotation",
        "margin-leverage": "margin-leverage",
        "holdings": "holdings",
        "sector-week": "sectors",
        "etf": "etf-quality",
        "replacement": "decision",
        "quality": "data-quality",
    }
    for section, anchor in anchors.items():
        marker = f'<section class="grid" data-section="{section}"'
        document = document.replace(marker, f'<section id="{anchor}" class="grid" data-section="{section}"', 1)
    footer = (
        f'<footer class="muted">报告格式 {esc(report_format)} · 数据修订 {esc(data.get("data_revision") or "legacy")} · '
        f'持仓快照 {esc(holdings_fingerprint)} · 证据 {esc(evidence_fingerprint)}<br>{esc(data.get("disclaimer") or "")}</footer>'
    )
    document = document.replace("</main>", footer + "</main>", 1)
    return document.replace("insufficient_evidence", "证据不足，暂不生成替换建议").replace("insufficient_data", "数据不足")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weekly-data", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(load_json(args.weekly_data)), encoding="utf-8")


if __name__ == "__main__":
    main()
