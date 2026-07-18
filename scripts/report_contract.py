"""Stable presentation contract shared by weekly report producers and validators."""

from __future__ import annotations


REPORT_FORMAT_VERSION = "weekly-visual-v2"
REPORT_TITLE = "三周基金轮动复盘"

# Order is part of the product contract. Additions require a format-version change.
MANDATORY_SECTION_ORDER = [
    "kpi",
    "llm-synthesis",
    "three-week-portfolio",
    "three-week-style",
    "margin-leverage",
    "three-week-industry",
    "three-week-concept",
    "weekly-conclusion",
    "holdings",
    "style",
    "sector-week",
    "sector-today",
    "sector-today-flow",
    "flows",
    "difference",
    "proxy",
    "etf",
    "replacement",
    "cache-audit",
    "quality",
]

NAV_ITEMS = [
    ("overview", "综合判断"),
    ("portfolio", "组合轨迹"),
    ("rotation", "市场轮动"),
    ("margin-leverage", "杠杆温度"),
    ("holdings", "持仓表现"),
    ("sectors", "板块证据"),
    ("etf-quality", "ETF质量"),
    ("decision", "调仓观察"),
    ("data-quality", "数据审计"),
]
