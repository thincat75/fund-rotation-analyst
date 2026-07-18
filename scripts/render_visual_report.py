#!/usr/bin/env python3
"""Render a visual HTML dashboard for fund rotation analysis."""

from __future__ import annotations

import argparse
import html
import json
from collections import Counter
from pathlib import Path
from typing import Any


COLORS = {
    "active": "#059669",
    "passive": "#2563eb",
    "enhanced": "#7c3aed",
    "risk": "#ea580c",
    "negative": "#dc2626",
    "muted": "#64748b",
    "ink": "#0f172a",
    "line": "#e2e8f0",
    "soft": "#f8fafc",
}


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def pct(value: Any, digits: int = 1) -> str:
    if value is None:
        return "-"
    return f"{float(value):.{digits}f}%"


def score(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):.2f}"


def money(value: Any) -> str:
    if value is None:
        return "-"
    value = float(value)
    if abs(value) >= 100000000:
        return f"{value / 100000000:.2f}亿"
    if abs(value) >= 10000:
        return f"{value / 10000:.2f}万"
    return f"{value:.0f}"


def weight(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value) * 100:.1f}%"


def profile(row: dict[str, Any], key: str = "product_profile") -> dict[str, Any]:
    return row.get(key) or {}


def style_class(product_profile: dict[str, Any]) -> str:
    style = product_profile.get("management_style")
    if style == "被动指数":
        return "passive"
    if style == "指数增强":
        return "enhanced"
    if style == "主动权益":
        return "active"
    return "neutral"


def badge(label: str, kind: str = "neutral") -> str:
    return f'<span class="badge {esc(kind)}">{esc(label)}</span>'


def product_badge(product_profile: dict[str, Any]) -> str:
    label = product_profile.get("management_style") or "未识别"
    return badge(str(label), style_class(product_profile))


def risk_badges(product_profile: dict[str, Any]) -> str:
    flags = list(product_profile.get("risk_flags") or [])
    if product_profile.get("is_active_equity") and product_profile.get("fund_size") is None:
        flags.append("规模待补")
    if not flags:
        return '<span class="muted">无明显产品风险标识</span>'
    return " ".join(badge(str(flag), "risk" if flag != "指数工具" else "passive") for flag in flags)


def fund_size(product_profile: dict[str, Any]) -> str:
    text = product_profile.get("fund_size_text") or "-"
    share = product_profile.get("share_size_text")
    if share and share != "-":
        return f"{esc(text)}<small>份额 {esc(share)}</small>"
    return esc(text)


def bar_width(value: float, max_value: float) -> str:
    if max_value <= 0:
        return "0%"
    return f"{max(2, min(100, abs(value) / max_value * 100)):.1f}%"


def performance_bars(funds: list[dict[str, Any]]) -> str:
    values = [abs(float((fund.get("returns") or {}).get("1月") or 0)) for fund in funds]
    max_value = max(values + [1])
    rows = []
    for fund in sorted(funds, key=lambda item: (item.get("returns") or {}).get("1月") or -999, reverse=True):
        one_month = float((fund.get("returns") or {}).get("1月") or 0)
        three_month = (fund.get("returns") or {}).get("3月")
        cls = "negative" if one_month < 0 else style_class(profile(fund))
        rows.append(
            f"""
            <div class="bar-row">
              <div class="bar-label"><b>{esc(fund.get('code'))}</b><span>{esc(fund.get('name'))}</span></div>
              <div class="bar-track"><div class="bar {cls}" style="width:{bar_width(one_month, max_value)}"></div></div>
              <div class="bar-value">{pct(one_month)}<small>3月 {pct(three_month)}</small></div>
            </div>
            """
        )
    return "\n".join(rows)


def style_bars(style_rows: list[dict[str, Any]]) -> str:
    max_value = max([abs(float(row.get("return_1m") or 0)) for row in style_rows] + [1])
    rows = []
    for row in style_rows:
        value = float(row.get("return_1m") or 0)
        cls = "negative" if value < 0 else "passive"
        rows.append(
            f"""
            <div class="bar-row compact">
              <div class="bar-label"><b>{esc(row.get('name'))}</b><span>{esc(row.get('status'))}</span></div>
              <div class="bar-track"><div class="bar {cls}" style="width:{bar_width(value, max_value)}"></div></div>
              <div class="bar-value">{pct(row.get('return_1m'))}<small>3月 {pct(row.get('return_3m'))}</small></div>
            </div>
            """
        )
    return "\n".join(rows)


def top30_score_bars(rows: list[dict[str, Any]], limit: int = 15) -> str:
    selected = rows[:limit]
    max_value = max([float(row.get("performance_score") or 0) for row in selected] + [1])
    output = []
    for row in selected:
        product_profile = profile(row)
        risk = " risk-outline" if product_profile.get("risk_flags") else ""
        output.append(
            f"""
            <div class="rank-row{risk}">
              <div class="rank-num">{esc(row.get('rank'))}</div>
              <div class="rank-name"><b>{esc(row.get('name'))}</b><span>{esc(row.get('code'))} · {'、'.join(esc(t) for t in row.get('themes') or [])}</span></div>
              <div class="rank-bar"><div class="bar {style_class(product_profile)}" style="width:{bar_width(float(row.get('performance_score') or 0), max_value)}"></div></div>
              <div class="rank-score">{score(row.get('performance_score'))}</div>
              <div>{product_badge(product_profile)}</div>
            </div>
            """
        )
    return "\n".join(output)


def theme_compare(comparison: dict[str, Any]) -> str:
    current = {row["name"]: row["count"] for row in comparison.get("current_theme_distribution", [])}
    top30 = {row["name"]: row["count"] for row in comparison.get("top30_theme_distribution", [])}
    themes = sorted(set(current) | set(top30), key=lambda theme: top30.get(theme, 0), reverse=True)[:10]
    max_value = max([current.get(theme, 0) for theme in themes] + [top30.get(theme, 0) for theme in themes] + [1])
    rows = []
    for theme in themes:
        rows.append(
            f"""
            <div class="theme-row">
              <div class="theme-label">{esc(theme)}</div>
              <div class="dual-bars">
                <div class="mini-line"><span>Top30</span><div class="mini-track"><i class="top" style="width:{bar_width(top30.get(theme, 0), max_value)}"></i></div><b>{top30.get(theme, 0)}</b></div>
                <div class="mini-line"><span>持仓</span><div class="mini-track"><i class="current" style="width:{bar_width(current.get(theme, 0), max_value)}"></i></div><b>{current.get(theme, 0)}</b></div>
              </div>
            </div>
            """
        )
    return "\n".join(rows)


def product_mix(rows: list[dict[str, Any]]) -> str:
    counts = Counter((profile(row).get("management_style") or "未识别") for row in rows)
    total = sum(counts.values()) or 1
    order = ["被动指数", "指数增强", "主动权益", "QDII", "债券/固收", "未识别"]
    segments = []
    legend = []
    for label in order:
        count = counts.get(label, 0)
        if not count:
            continue
        cls = style_class({"management_style": label})
        segments.append(f'<span class="{cls}" style="width:{count / total * 100:.1f}%"></span>')
        legend.append(f'<li>{product_badge({"management_style": label})}<b>{count}</b></li>')
    return f'<div class="stacked">{"".join(segments)}</div><ul class="legend">{"".join(legend)}</ul>'


def replacement_cards(recommendations: list[dict[str, Any]], total_amount: float, cap: float) -> str:
    if not recommendations:
        return '<p class="muted">暂无替换建议。</p>'
    step = min(cap or 0.10, 0.10) / max(1, len(recommendations))
    cards = []
    for index, rec in enumerate(recommendations, 1):
        candidate_profile = rec.get("candidate_product_profile") or {}
        replace_profile = rec.get("replace_product_profile") or {}
        cards.append(
            f"""
            <article class="flow-card">
              <div class="flow-index">#{index}</div>
              <div class="flow-side out">
                <span>调出</span>
                <b>{esc(rec.get('replace_name'))}</b>
                <small>{esc(rec.get('replace_code'))} · {product_badge(replace_profile)} · {fund_size(replace_profile)}</small>
              </div>
              <div class="arrow">→</div>
              <div class="flow-side in">
                <span>候选</span>
                <b>{esc(rec.get('candidate_name'))}</b>
                <small>{esc(rec.get('candidate_code'))} · {product_badge(candidate_profile)} · {fund_size(candidate_profile)}</small>
              </div>
              <div class="flow-metrics">
                <strong>分差 {score(rec.get('score_gap'))}</strong>
                <strong>第一期 {weight(step)} / {money(step * total_amount)}</strong>
              </div>
              <p>{esc(rec.get('reason'))}</p>
            </article>
            """
        )
    return "\n".join(cards)


def allocation_after_cards(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return '<p class="muted">暂无配比数据。</p>'
    max_value = max([max(float(row.get("current_weight") or 0), float(row.get("target_weight") or 0), float(row.get("first_step_target_weight") or 0)) for row in rows] + [0.01])
    output = []
    for row in rows:
        code, name = row.get("code"), row.get("name")
        current = float(row.get("current_weight") or 0)
        target = float(row.get("target_weight") or 0)
        first_step = float(row.get("first_step_target_weight") or target)
        action = row.get("action") or "观察"
        cls = "in" if target > current else "out" if target < current else "muted-line"
        output.append(
            f"""
            <div class="alloc-row {cls}">
              <div><b>{esc(code)}</b><span>{esc(name)}</span></div>
              <div class="alloc-bars">
                <div><span>当前</span><i style="width:{bar_width(current, max_value)}"></i><b>{weight(current)}</b></div>
                <div><span>战略目标</span><i style="width:{bar_width(target, max_value)}"></i><b>{weight(target)}</b></div>
                <div><span>第一期</span><i style="width:{bar_width(first_step, max_value)}"></i><b>{weight(first_step)}</b></div>
              </div>
              <em>{esc(action)}</em>
            </div>
            """
        )
    return "\n".join(output)


def flow_bars(block: dict[str, Any], label: str) -> str:
    rows = (block.get("inflow") or [])[:6]
    if not rows:
        return '<p class="muted">暂无可用数据。</p>'
    max_value = max([abs(float(row.get("today") or 0)) for row in rows] + [1])
    output = [f"<h3>{esc(label)}</h3>"]
    for row in rows:
        value = float(row.get("today") or 0)
        output.append(
            f"""
            <div class="bar-row compact">
              <div class="bar-label"><b>{esc(row.get('name'))}</b><span>{esc(row.get('status'))}</span></div>
              <div class="bar-track"><div class="bar active" style="width:{bar_width(value, max_value)}"></div></div>
              <div class="bar-value">{money(value)}<small>5日 {money(row.get('five_day'))}</small></div>
            </div>
            """
        )
    return "\n".join(output)


def top30_cards(rows: list[dict[str, Any]]) -> str:
    cards = []
    for row in rows:
        product_profile = profile(row)
        cards.append(
            f"""
            <article class="fund-card">
              <div><b>#{esc(row.get('rank'))} {esc(row.get('name'))}</b><span>{esc(row.get('code'))}</span></div>
              <div>{product_badge(product_profile)} {risk_badges(product_profile)}</div>
              <div>{badge(str(row.get('theme_confidence') or '低') + '置信主题', 'neutral')}</div>
              <ul>
                <li>综合分 <strong>{score(row.get('performance_score'))}</strong></li>
                <li>近1月 <strong>{pct(row.get('return_1m'))}</strong></li>
                <li>近3月 <strong>{pct(row.get('return_3m'))}</strong></li>
                <li>规模 <strong>{fund_size(product_profile)}</strong></li>
              </ul>
              <small>{esc(row.get('theme_basis') or '')}</small>
            </article>
            """
        )
    return "\n".join(cards)


def holding_audit_cards(funds: list[dict[str, Any]]) -> str:
    cards = []
    for fund in funds:
        product_profile = profile(fund)
        themes = [theme for theme in fund.get("themes", []) if theme not in {"主动权益", "混合型", "偏股混合", "质量成长", "LOF"}]
        stocks = fund.get("top_stocks") or []
        cards.append(
            f"""
            <article class="audit-card">
              <div class="audit-head">
                <div><b>{esc(fund.get('name'))}</b><span>{esc(fund.get('code'))}</span></div>
                {product_badge(product_profile)}
              </div>
              <div class="theme-tags">{''.join(badge(str(theme), 'neutral') for theme in themes) or '<span class="muted">主题待确认</span>'}</div>
              <p>{esc('、'.join(stocks[:10]))}</p>
              <small>主题{esc(fund.get('theme_confidence') or '低')}置信 · {esc(fund.get('theme_basis') or '依据待确认')}</small>
              <small>近1月 {pct((fund.get('returns') or {}).get('1月'))} · 近3月 {pct((fund.get('returns') or {}).get('3月'))} · 规模 {fund_size(product_profile)}</small>
            </article>
            """
        )
    return "\n".join(cards)


def render(analysis: dict[str, Any]) -> str:
    portfolio = analysis.get("portfolio") or {}
    funds = portfolio.get("funds") or []
    rankings = analysis.get("rankings") or {}
    comparison = rankings.get("comparison") or {}
    top30 = rankings.get("primary_top30") or []
    recommendations = comparison.get("top_replacement_recommendations") or []
    constraints = analysis.get("constraints") or {}
    total_amount = float(portfolio.get("total_amount") or 0)
    decision = comparison.get("rebalance_decision") or "暂无调仓判断。"
    warnings = analysis.get("warnings") or []
    covered = "、".join(comparison.get("covered_themes") or []) or "-"
    missing = "、".join(comparison.get("missing_themes") or []) or "-"

    best_holding = max(funds, key=lambda fund: (fund.get("returns") or {}).get("1月") or -999) if funds else {}
    worst_holding = min(funds, key=lambda fund: (fund.get("returns") or {}).get("1月") or 999) if funds else {}
    passive_count = sum(1 for row in top30 if profile(row).get("is_passive_index"))
    active_risk_count = sum(1 for row in top30 if profile(row).get("risk_flags"))

    html_doc = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>基金组合轮动可视化报告</title>
  <style>
    :root {{
      --ink:{COLORS['ink']}; --muted:{COLORS['muted']}; --line:{COLORS['line']}; --soft:{COLORS['soft']};
      --active:{COLORS['active']}; --passive:{COLORS['passive']}; --enhanced:{COLORS['enhanced']};
      --risk:{COLORS['risk']}; --negative:{COLORS['negative']};
    }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:#f6f8fb; color:var(--ink); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",Arial,sans-serif; }}
    main {{ max-width:1280px; margin:0 auto; padding:28px; }}
    header {{ display:flex; justify-content:space-between; gap:24px; align-items:flex-end; padding:8px 0 24px; border-bottom:1px solid var(--line); }}
    h1 {{ margin:0; font-size:30px; letter-spacing:0; }}
    h2 {{ margin:0 0 16px; font-size:20px; }}
    h3 {{ margin:14px 0 10px; font-size:15px; color:var(--muted); }}
    p {{ line-height:1.65; }}
    small, .muted {{ color:var(--muted); font-size:12px; }}
    section {{ margin-top:22px; }}
    .grid {{ display:grid; grid-template-columns:repeat(12,1fr); gap:16px; }}
    .card {{ background:#fff; border:1px solid var(--line); border-radius:10px; padding:18px; box-shadow:0 10px 28px rgba(15,23,42,.04); }}
    .span-3 {{ grid-column:span 3; }} .span-4 {{ grid-column:span 4; }} .span-5 {{ grid-column:span 5; }} .span-6 {{ grid-column:span 6; }} .span-7 {{ grid-column:span 7; }} .span-8 {{ grid-column:span 8; }} .span-12 {{ grid-column:span 12; }}
    .kpi b {{ display:block; font-size:28px; margin-top:8px; }} .kpi span {{ color:var(--muted); font-size:13px; }}
    .decision {{ border-left:5px solid var(--risk); background:#fff7ed; }}
    .badge {{ display:inline-flex; align-items:center; padding:3px 7px; border-radius:5px; font-size:12px; font-weight:700; white-space:nowrap; }}
    .badge.active {{ background:#ecfdf5; color:#047857; }} .badge.passive {{ background:#e8f1ff; color:#1d4ed8; }}
    .badge.enhanced {{ background:#eef2ff; color:#4f46e5; }} .badge.risk {{ background:#fff7ed; color:#c2410c; }} .badge.neutral {{ background:#f1f5f9; color:#475569; }}
    .bar-row {{ display:grid; grid-template-columns:minmax(180px,2.2fr) minmax(160px,4fr) 92px; gap:12px; align-items:center; padding:9px 0; border-bottom:1px solid #f1f5f9; }}
    .bar-row.compact {{ grid-template-columns:minmax(130px,1.8fr) minmax(140px,4fr) 90px; }}
    .bar-label b, .rank-name b {{ display:block; font-size:13px; }} .bar-label span, .rank-name span {{ display:block; color:var(--muted); font-size:12px; margin-top:2px; }}
    .bar-track, .rank-bar {{ height:12px; background:#eef2f7; border-radius:999px; overflow:hidden; }}
    .bar {{ display:block; height:100%; border-radius:999px; }}
    .bar.active, .active {{ background:var(--active); }} .bar.passive, .passive {{ background:var(--passive); }} .bar.enhanced, .enhanced {{ background:var(--enhanced); }} .bar.negative {{ background:var(--negative); }}
    .bar-value {{ font-weight:800; text-align:right; font-size:13px; }} .bar-value small {{ display:block; font-weight:500; margin-top:2px; }}
    .rank-row {{ display:grid; grid-template-columns:34px minmax(220px,2fr) minmax(160px,3fr) 54px 86px; align-items:center; gap:12px; padding:9px 0; border-bottom:1px solid #f1f5f9; }}
    .rank-num {{ width:28px; height:28px; border-radius:50%; background:#f1f5f9; display:grid; place-items:center; font-weight:800; font-size:12px; }}
    .rank-score {{ text-align:right; font-weight:900; }} .risk-outline {{ outline:1px solid #fed7aa; outline-offset:-3px; border-radius:7px; }}
    .theme-row {{ display:grid; grid-template-columns:90px 1fr; gap:12px; padding:8px 0; border-bottom:1px solid #f1f5f9; }}
    .theme-label {{ font-weight:800; }} .mini-line {{ display:grid; grid-template-columns:46px 1fr 24px; gap:8px; align-items:center; margin:2px 0; font-size:12px; color:var(--muted); }}
    .mini-track {{ height:8px; background:#eef2f7; border-radius:999px; overflow:hidden; }} .mini-track i {{ display:block; height:100%; border-radius:999px; }} .mini-track .top {{ background:var(--passive); }} .mini-track .current {{ background:var(--active); }}
    .stacked {{ height:24px; background:#eef2f7; border-radius:999px; overflow:hidden; display:flex; }} .stacked span {{ height:100%; }}
    .legend {{ list-style:none; padding:0; margin:14px 0 0; display:flex; flex-wrap:wrap; gap:10px; }} .legend li {{ display:flex; gap:6px; align-items:center; }}
    .flow-card {{ background:#fff; border:1px solid var(--line); border-radius:10px; padding:16px; display:grid; grid-template-columns:40px 1fr 28px 1fr; gap:12px; align-items:center; margin-bottom:12px; }}
    .flow-index {{ font-weight:900; color:var(--risk); }} .flow-side span {{ color:var(--muted); font-size:12px; }} .flow-side b {{ display:block; margin:4px 0; }} .flow-side small {{ display:block; line-height:1.5; }} .arrow {{ font-size:22px; color:var(--muted); }}
    .flow-metrics {{ grid-column:2 / 5; display:flex; gap:10px; flex-wrap:wrap; }} .flow-metrics strong {{ background:#f8fafc; border:1px solid var(--line); border-radius:8px; padding:6px 8px; }}
    .flow-card p {{ grid-column:2 / 5; margin:0; color:#334155; }}
    .alloc-row {{ display:grid; grid-template-columns:minmax(180px,2fr) minmax(220px,4fr) 70px; gap:12px; align-items:center; padding:8px 0; border-bottom:1px solid #f1f5f9; }}
    .alloc-row div:first-child b, .fund-card b {{ display:block; }} .alloc-row div:first-child span, .fund-card span {{ display:block; color:var(--muted); font-size:12px; }}
    .alloc-bars div {{ display:grid; grid-template-columns:38px 1fr 48px; gap:8px; align-items:center; margin:3px 0; font-size:12px; }} .alloc-bars i {{ height:8px; background:var(--passive); border-radius:999px; }} .alloc-row.in .alloc-bars i {{ background:var(--active); }} .alloc-row.out .alloc-bars i {{ background:var(--risk); }} .alloc-row em {{ font-style:normal; color:var(--muted); font-size:12px; }}
    details summary {{ cursor:pointer; font-weight:800; margin-bottom:12px; }} .fund-grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); gap:12px; }}
    .fund-card {{ border:1px solid var(--line); border-radius:10px; padding:12px; background:#fff; }} .fund-card ul {{ margin:10px 0 0; padding:0; list-style:none; display:grid; gap:5px; }} .fund-card li {{ display:flex; justify-content:space-between; gap:8px; font-size:12px; }}
    .audit-grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(300px,1fr)); gap:12px; }}
    .audit-card {{ border:1px solid var(--line); border-radius:10px; padding:14px; background:#fff; }}
    .audit-head {{ display:flex; justify-content:space-between; gap:10px; align-items:flex-start; }}
    .audit-head b {{ display:block; }} .audit-head span {{ display:block; color:var(--muted); font-size:12px; margin-top:2px; }}
    .theme-tags {{ display:flex; flex-wrap:wrap; gap:6px; margin:10px 0; }}
    .audit-card p {{ margin:0 0 10px; color:#334155; font-size:13px; line-height:1.55; }}
    .footer {{ color:var(--muted); font-size:12px; }}
    @media (max-width:900px) {{
      main {{ padding:16px; }} header {{ display:block; }} .grid {{ grid-template-columns:1fr; }} .span-3,.span-4,.span-5,.span-6,.span-7,.span-8,.span-12 {{ grid-column:span 1; }}
      .bar-row,.bar-row.compact,.rank-row,.theme-row,.alloc-row,.flow-card {{ grid-template-columns:1fr; }}
      .flow-metrics,.flow-card p {{ grid-column:auto; }} .bar-value {{ text-align:left; }}
    }}
  </style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>基金组合轮动可视化报告</h1>
      <p class="muted">数据时间 {esc(analysis.get('as_of'))} · 数据源 {esc(analysis.get('source'))} · 综合分 = 0.70 × 近1月 + 0.30 × 近3月月均</p>
    </div>
    <div>{badge("基金级别分析", "neutral")} {badge("非交易指令", "risk")}</div>
  </header>

  <section class="grid">
    <article class="card span-3 kpi"><span>组合规模</span><b>{money(total_amount)}</b><small>当前按等权持仓试算</small></article>
    <article class="card span-3 kpi"><span>最佳持仓近1月</span><b>{pct((best_holding.get('returns') or {{}}).get('1月'))}</b><small>{esc(best_holding.get('name'))}</small></article>
    <article class="card span-3 kpi"><span>Top30 被动指数数</span><b>{passive_count}</b><small>半导体指数工具明显增多</small></article>
    <article class="card span-3 kpi"><span>Top30 产品风险数</span><b>{active_risk_count}</b><small>小规模主动或指数工具标识</small></article>
    <article class="card span-12 decision"><h2>调仓结论</h2><p>{esc(decision)}</p><p><b>已覆盖：</b>{esc(covered)}　<b>缺失机会：</b>{esc(missing)}　<b>最弱持仓近1月：</b>{esc(worst_holding.get('name'))} {pct((worst_holding.get('returns') or {{}}).get('1月'))}</p></article>
  </section>

  <section class="grid">
    <article class="card span-7"><h2>当前持仓表现</h2>{performance_bars(funds)}</article>
    <article class="card span-5"><h2>大盘与风格强弱</h2>{style_bars((analysis.get('market') or {{}}).get('style') or [])}</article>
  </section>

  <section class="card">
    <h2>当前持仓内容审计</h2>
    <div class="audit-grid">{holding_audit_cards(funds)}</div>
  </section>

  <section class="grid">
    <article class="card span-8"><h2>近1月为主 Top30 综合分排行</h2>{top30_score_bars(top30)}</article>
    <article class="card span-4"><h2>Top30 产品属性结构</h2>{product_mix(top30)}<h3>主题差异</h3>{theme_compare(comparison)}</article>
  </section>

  <section class="grid">
    <article class="card span-12"><h2>Top3 替换流向</h2>{replacement_cards(recommendations, total_amount, float(constraints.get('high_volatility_adjustment_cap') or 0.10))}</article>
    <article class="card span-12"><h2>战略目标与第一期配比</h2>{allocation_after_cards(portfolio.get('allocations') or [])}</article>
  </section>

  <section class="grid">
    <article class="card span-6">{flow_bars(((analysis.get('market') or {{}}).get('industry_flow') or {{}}), '行业资金流入')}</article>
    <article class="card span-6">{flow_bars(((analysis.get('market') or {{}}).get('concept_flow') or {{}}), '概念资金流入')}</article>
  </section>

  <section class="card">
    <details>
      <summary>展开查看 Top30 明细卡片</summary>
      <div class="fund-grid">{top30_cards(top30)}</div>
    </details>
  </section>

  <section class="card footer">
    <p>{esc(analysis.get('disclaimer'))}</p>
    <p>主题判断优先使用前十大持仓和行业配置；只有名称或类型命中关键词时，会标为低置信，不作为单独调仓依据。</p>
    <p>换手线索来自可采集字段或披露持仓变化；若显示“待确认”，表示 AkShare 当前接口未提供足够披露期或真实换手率字段。</p>
    <p>{'；'.join(esc(warning) for warning in warnings[:8]) if warnings else '暂无数据采集警告。'}</p>
  </section>
</main>
</body>
</html>
"""
    return html_doc


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(load_json(args.analysis)), encoding="utf-8")


if __name__ == "__main__":
    main()
