#!/usr/bin/env python3
"""Analyze collected fund and market data."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean, pstdev
from typing import Any


RETURN_WINDOWS = {
    "1周": 7,
    "1月": 30,
    "3月": 90,
    "6月": 180,
    "1年": 365,
}

RANK_PERIODS = ["近1周", "近1月", "近3月", "近6月", "今年来", "近1年"]
PRIMARY_RANK_PERIOD = "近1月"

THEME_KEYWORDS = {
    "AI光模块": ["光模块", "光通信", "CPO", "新易盛", "中际旭创", "天孚通信", "长芯博创", "源杰科技", "长飞光纤", "亨通光电", "腾景科技", "德科立"],
    "半导体": [
        "半导体", "芯片", "集成电路", "数字芯片", "晶圆", "封测", "中证半导体",
        "富创精密", "华海清科", "中微公司", "北方华创", "拓荆科技", "中科飞测",
        "芯源微", "精测电子", "京仪装备", "盛美上海", "正帆科技", "福晶科技",
        "茂莱光学", "长川科技", "汇成真空", "江化微", "中船特气", "沪硅产业",
        "南大光电", "雅克科技", "江丰电子", "上海新阳", "晶瑞电材", "广立微",
        "鼎龙股份", "安集科技", "联瑞新材", "寒武纪", "海光信息", "华虹公司",
        "普冉股份", "兆易创新", "北京君正", "江波龙", "德明利", "香农芯创",
        "佰维存储", "朗科科技", "聚辰股份", "中芯国际", "华峰测控", "芯原股份",
        "伟测科技", "西测测试", "ST臻镭", "国博电子", "复旦微电", "成都华微",
        "芯碁微装", "杰华特", "燕东微", "先导基电",
    ],
    "PCB/AI服务器链": ["沪电股份", "生益科技", "生益电子", "胜宏科技", "深南电路", "东山精密", "鼎泰高科", "江南新材", "宏和科技", "大族数控"],
    "通信": ["通信", "通信设备", "5G", "6G", "光纤", "光缆", "算力", "数据中心"],
    "AI软件": ["人工智能", "AI", "计算机", "软件", "信创", "云计算", "机器人"],
    "创新药": ["创新药", "生物医药", "医药生物", "化学制药", "医疗服务", "CXO", "医疗研发"],
    "医药": ["医药", "医疗", "生物", "疫苗", "中药"],
    "消费": ["消费", "白酒", "食品", "家电", "旅游", "农业"],
    "新能源": ["新能源", "电池", "光伏", "储能", "电力设备", "汽车"],
    "航空出行": ["春秋航空", "南方航空", "中国东航", "中国国航", "吉祥航空"],
    "能源装备/高端制造": ["中国动力", "东方电气", "杰瑞股份", "应流股份", "思源电气", "阳光电源", "固德威", "万泽股份", "三花智控", "立讯精密", "水晶光电"],
    "军工高端制造": ["军工", "国防", "航天", "船舶", "航空装备", "中航", "航发"],
    "资源周期": ["资源", "有色", "煤炭", "石油", "黄金", "铜", "采矿", "化工"],
    "金融地产": ["银行", "证券", "保险", "地产", "金融"],
    "红利价值": ["红利", "低波", "股息"],
    "可转债": ["可转债", "转债"],
    "港股QDII": ["港股", "恒生", "QDII", "海外", "纳斯达克", "标普"],
    "债券现金": ["债", "货币", "短债", "现金", "同业存单"],
}

NON_MARKET_THEMES = {"主动权益", "混合型", "偏股混合", "质量成长", "LOF", "指数", "ETF"}


def market_themes(themes: list[str]) -> list[str]:
    return [theme for theme in themes if theme not in NON_MARKET_THEMES and theme != "未识别"]


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
        handle.write("\n")


def to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if math.isnan(float(value)):
            return None
        return float(value)
    text = str(value).strip().replace(",", "").replace("%", "")
    if not text or text in {"--", "-"}:
        return None
    multiplier = 1.0
    if "亿" in text:
        multiplier = 100000000.0
    elif "万" in text:
        multiplier = 10000.0
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if match:
        return float(match.group(0)) * multiplier
    try:
        return float(text)
    except ValueError:
        return None


def find_key(row: dict[str, Any], candidates: list[str]) -> str | None:
    keys = list(row.keys())
    for candidate in candidates:
        for key in keys:
            if candidate in key:
                return key
    return None


def parse_date(value: Any) -> dt.date | None:
    if value is None:
        return None
    text = str(value).strip()[:10]
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def extract_series(records: list[dict[str, Any]]) -> list[tuple[dt.date, float]]:
    if not records:
        return []
    sample = records[0]
    date_key = find_key(sample, ["净值日期", "日期", "date", "交易日"])
    value_key = find_key(sample, ["单位净值", "累计净值", "收盘", "close", "最新净值"])
    if not date_key or not value_key:
        numeric_keys = [key for key, value in sample.items() if to_float(value) is not None]
        value_key = numeric_keys[0] if numeric_keys else None
    if not date_key or not value_key:
        return []

    series = []
    for row in records:
        day = parse_date(row.get(date_key))
        value = to_float(row.get(value_key))
        if day and value and value > 0:
            series.append((day, value))
    return sorted(series, key=lambda item: item[0])


def return_since(series: list[tuple[dt.date, float]], days: int) -> float | None:
    if len(series) < 2:
        return None
    latest_day, latest_value = series[-1]
    cutoff = latest_day - dt.timedelta(days=days)
    baseline = series[0][1]
    for day, value in series:
        if day <= cutoff:
            baseline = value
        else:
            break
    return (latest_value / baseline - 1) * 100


def max_drawdown(series: list[tuple[dt.date, float]]) -> float | None:
    if len(series) < 2:
        return None
    peak = series[0][1]
    worst = 0.0
    for _, value in series:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1)
    return worst * 100


def volatility(series: list[tuple[dt.date, float]]) -> float | None:
    if len(series) < 5:
        return None
    returns = []
    for (_, prev), (_, current) in zip(series, series[1:]):
        if prev:
            returns.append(current / prev - 1)
    if len(returns) < 2:
        return None
    return pstdev(returns) * math.sqrt(252) * 100


def pct(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value, 2)


def performance_score(return_1m: Any, return_3m: Any) -> float | None:
    one_month = to_float(return_1m)
    three_month = to_float(return_3m)
    if one_month is None and three_month is None:
        return None
    three_month_pace = three_month / 3 if three_month is not None else one_month
    one_month = one_month if one_month is not None else three_month_pace
    return round(one_month * 0.70 + three_month_pace * 0.30, 2)


def score_text(score: float | None) -> str:
    return f"{score:.2f}" if score is not None else "-"


def infer_themes(*texts: Any) -> list[str]:
    joined = " ".join(str(text) for text in texts if text)
    themes = []
    for theme, words in THEME_KEYWORDS.items():
        if any(word.lower() in joined.lower() for word in words):
            themes.append(theme)
    return themes or ["未识别"]


def infer_theme_counts_from_items(items: list[str]) -> Counter[str]:
    counter: Counter[str] = Counter()
    for item in items:
        for theme in infer_themes(item):
            if theme != "未识别":
                counter[theme] += 1
    return counter


def infer_theme_evidence(name: str, fund_type_text: Any, stocks: list[str], industries: list[str]) -> dict[str, Any]:
    """Infer themes with disclosed holdings taking priority over fund names."""
    stock_theme_counts = infer_theme_counts_from_items(stocks)
    stock_themes = [theme for theme, count in stock_theme_counts.most_common() if count >= 2]
    if not stock_themes and stock_theme_counts:
        theme, count = stock_theme_counts.most_common(1)[0]
        if count >= 1:
            stock_themes = [theme]
    industry_themes = [theme for theme in infer_themes(industries) if theme != "未识别"]
    name_themes = [theme for theme in infer_themes(name, fund_type_text) if theme != "未识别"]

    disclosed_themes = stock_themes or industry_themes
    if disclosed_themes:
        # Fund names often lag actual rotation. Keep name-only themes only when they
        # are also visible in at least two disclosed holdings.
        themes = list(dict.fromkeys(disclosed_themes + [theme for theme in name_themes if theme in stock_themes]))
    else:
        themes = name_themes

    # A fund name can lag the actual portfolio. If disclosed holdings clearly point
    # to semiconductor equipment/materials, do not keep a name-only AI software tag.
    if "半导体" in disclosed_themes and "AI软件" in name_themes and "AI软件" not in stock_themes:
        themes = [theme for theme in themes if theme != "AI软件"]

    if disclosed_themes:
        confidence = "高"
        basis = "前十大持仓/行业配置披露"
    elif name_themes:
        confidence = "低"
        basis = "基金名称/类型关键词，需用持仓确认"
    else:
        confidence = "低"
        basis = "未识别到稳定主题"

    return {
        "themes": themes or ["未识别"],
        "confidence": confidence,
        "basis": basis,
        "stock_theme_counts": dict(stock_theme_counts),
        "industry_themes": industry_themes,
        "name_themes": name_themes,
    }


def infer_themes_from_sources(name: str, fund_type_text: Any, stocks: list[str], industries: list[str]) -> list[str]:
    return infer_theme_evidence(name, fund_type_text, stocks, industries)["themes"]


def top_stock_names(rows: list[dict[str, Any]], limit: int = 10) -> list[str]:
    names = []
    for row in rows or []:
        key = find_key(row, ["股票名称", "证券名称", "名称"])
        if key and row.get(key):
            names.append(str(row[key]))
    return names[:limit]


def display_name(holding: dict[str, Any], metadata: dict[str, Any]) -> str:
    if holding.get("name"):
        return str(holding["name"])
    for key in ["基金简称", "基金名称", "name"]:
        if metadata.get(key):
            return str(metadata[key])
    return holding["code"]


def profile_map(rows: list[dict[str, Any]]) -> dict[str, Any]:
    mapped = {}
    for row in rows or []:
        key = row.get("item") or row.get("字段") or row.get("项目") or row.get("name")
        value = row.get("value") or row.get("值") or row.get("内容")
        if key is not None and value is not None:
            mapped[str(key)] = value
    return mapped


def first_profile_value(*profiles: dict[str, Any], keys: list[str]) -> Any:
    for profile in profiles:
        for key in keys:
            for existing_key, value in profile.items():
                if key in existing_key and value not in {None, "", "<NA>"}:
                    return value
    return None


def infer_product_profile(name: str, metadata: dict[str, Any], detail: dict[str, Any], stock_rows: list[dict[str, Any]]) -> dict[str, Any]:
    basic = profile_map(detail.get("basic_info") or [])
    ths = profile_map(detail.get("ths_info") or [])
    metadata_type = str(metadata.get("基金类型") or "")
    fund_type = str(first_profile_value(basic, ths, metadata, keys=["基金类型"]) or metadata_type or "-")
    investment_type = str(first_profile_value(ths, basic, metadata, keys=["投资类型"]) or "")
    benchmark = str(first_profile_value(basic, ths, keys=["业绩比较基准"]) or "")
    strategy = str(first_profile_value(basic, ths, keys=["投资策略", "投资目标"]) or "")
    joined = " ".join([name, fund_type, investment_type, benchmark, strategy])

    is_index = any(token in joined for token in ["标准指数", "指数型", "ETF联接", "交易型开放式指数", "目标ETF", "跟踪标的指数"])
    is_enhanced = "指数增强" in joined or "增强指数" in joined
    is_passive = is_index and not is_enhanced
    is_qdii = "QDII" in joined or "海外" in joined
    if is_passive:
        management_style = "被动指数"
    elif is_enhanced:
        management_style = "指数增强"
    elif is_qdii:
        management_style = "QDII"
    elif any(token in joined for token in ["混合", "股票"]):
        management_style = "主动权益"
    elif "债" in joined:
        management_style = "债券/固收"
    else:
        management_style = "未识别"

    size_text = first_profile_value(basic, ths, keys=["最新规模", "基金规模", "资产净值"])
    share_size_text = first_profile_value(ths, basic, keys=["份额规模"])
    size_value = to_float(size_text)
    turnover_text = first_profile_value(basic, ths, keys=["换手率", "股票换手率"])
    turnover_value = to_float(turnover_text)
    churn = holding_churn_signal(stock_rows)

    risk_flags = []
    if management_style == "主动权益":
        if size_value is not None and size_value < 200000000:
            risk_flags.append("主动迷你规模")
        elif size_value is not None and size_value < 500000000:
            risk_flags.append("主动小规模")
        if turnover_value is not None and turnover_value >= 300:
            risk_flags.append("换手率高")
        elif churn.get("level") == "高":
            risk_flags.append("披露持仓变化大")
    if management_style == "被动指数":
        risk_flags.append("指数工具")

    return {
        "fund_type_detail": fund_type,
        "investment_type": investment_type or "-",
        "management_style": management_style,
        "is_passive_index": is_passive,
        "is_active_equity": management_style == "主动权益",
        "fund_size": size_value,
        "fund_size_text": str(size_text) if size_text is not None else "-",
        "share_size_text": str(share_size_text) if share_size_text is not None else "-",
        "turnover": turnover_value,
        "turnover_text": str(turnover_text) if turnover_text is not None else "-",
        "holding_churn": churn,
        "risk_flags": risk_flags,
    }


def holding_churn_signal(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_quarter: dict[str, list[str]] = defaultdict(list)
    for row in rows or []:
        quarter_key = find_key(row, ["季度", "报告期", "截止时间"])
        stock_key = find_key(row, ["股票代码", "股票名称", "证券代码", "证券名称", "名称"])
        quarter = str(row.get(quarter_key) or "") if quarter_key else ""
        stock = str(row.get(stock_key) or "") if stock_key else ""
        if quarter and stock:
            by_quarter[quarter].append(stock)
    if len(by_quarter) < 2:
        return {"level": "待确认", "rate": None, "note": "仅有一期披露持仓，无法估算换手线索"}

    def quarter_sort_key(label: str) -> tuple[int, int, str]:
        year_match = re.search(r"(20\d{2})", label)
        quarter_match = re.search(r"([1-4])季度|Q([1-4])", label, flags=re.I)
        year = int(year_match.group(1)) if year_match else 0
        quarter = int(next(group for group in quarter_match.groups() if group)) if quarter_match else 0
        return year, quarter, label

    quarters = sorted(by_quarter, key=quarter_sort_key, reverse=True)
    current = set(by_quarter[quarters[0]][:10])
    previous = set(by_quarter[quarters[1]][:10])
    if not current or not previous:
        return {"level": "待确认", "rate": None, "note": "披露持仓不足，无法估算换手线索"}
    overlap = len(current & previous)
    rate = round((1 - overlap / max(len(current), 1)) * 100, 1)
    if rate >= 60:
        level = "高"
    elif rate >= 35:
        level = "中"
    else:
        level = "低"
    return {"level": level, "rate": rate, "note": f"前十大持仓披露变化约{rate:.1f}%，非正式换手率"}


def derive_weights(holdings: list[dict[str, Any]]) -> dict[str, float]:
    explicit = [to_float(item.get("current_weight")) for item in holdings]
    if all(value is not None and value > 0 for value in explicit):
        normalized = [float(value) / 100 if float(value) > 1 else float(value) for value in explicit]
        total = sum(normalized)
        return {item["code"]: value / total for item, value in zip(holdings, normalized)}
    total_amount = sum(float(item.get("amount") or 0) for item in holdings)
    if total_amount > 0:
        return {item["code"]: float(item.get("amount") or 0) / total_amount for item in holdings}
    equal = 1 / len(holdings) if holdings else 0
    return {item["code"]: equal for item in holdings}


def analyze_funds(holdings: list[dict[str, Any]], market_data: dict[str, Any]) -> list[dict[str, Any]]:
    weights = derive_weights(holdings)
    fund_data = market_data.get("funds", {})
    results = []
    for holding in holdings:
        code = holding["code"]
        data = fund_data.get(code, {})
        metadata = data.get("metadata") or {}
        name = display_name(holding, metadata)
        product_profile = infer_product_profile(name, metadata, data, data.get("stock_holdings") or [])
        series = extract_series(data.get("nav") or [])
        returns = {label: pct(return_since(series, days)) for label, days in RETURN_WINDOWS.items()}
        industries = []
        for row in data.get("industry_allocation") or []:
            key = find_key(row, ["行业类别", "行业", "名称"])
            weight_key = find_key(row, ["占净值比例", "比例", "占比"])
            if key:
                weight = to_float(row.get(weight_key)) if weight_key else None
                if weight is None or weight > 0.5:
                    industries.append({"name": str(row.get(key)), "weight": weight})
        top_industry_names = [item["name"] for item in industries[:5]]
        stocks = top_stock_names(data.get("stock_holdings") or [])
        user_tags = holding.get("tags") or []
        theme_evidence = infer_theme_evidence(name, metadata.get("基金类型"), stocks, top_industry_names)
        inferred_themes = theme_evidence["themes"]
        if user_tags and inferred_themes == ["未识别"]:
            inferred_themes = []
            theme_evidence = {**theme_evidence, "confidence": "中", "basis": "用户标签"}
        themes = sorted(set(user_tags + inferred_themes))

        momentum_values = [value for value in returns.values() if value is not None]
        momentum = mean(momentum_values) if momentum_values else 0
        drawdown = max_drawdown(series)
        vol = volatility(series)
        score = 50 + momentum * 1.4
        if drawdown is not None:
            score += max(drawdown, -35) * 0.55
        if vol is not None:
            score -= min(vol, 45) * 0.25
        if holding.get("is_core"):
            score += 4
        score = max(0, min(100, score))

        results.append(
            {
                "code": code,
                "name": name,
                "current_weight": round(weights.get(code, 0), 4),
                "amount": holding.get("amount"),
                "cost": holding.get("cost"),
                "is_core": bool(holding.get("is_core")),
                "themes": themes,
                "theme_confidence": theme_evidence["confidence"],
                "theme_basis": theme_evidence["basis"],
                "returns": returns,
                "max_drawdown": pct(drawdown),
                "volatility": pct(vol),
                "industries": industries[:8],
                "top_stocks": stocks,
                "product_profile": product_profile,
                "score": round(score, 1),
                "confidence": "高" if len(series) >= 60 else ("中" if len(series) >= 10 else "低"),
            }
        )
    return results


def analyze_style(market_data: dict[str, Any]) -> list[dict[str, Any]]:
    indexes = (market_data.get("market") or {}).get("style_indexes") or {}
    rows = []
    for name, records in indexes.items():
        series = extract_series(records)
        one_month = return_since(series, 30)
        three_month = return_since(series, 90)
        if one_month is None and three_month is None:
            status = "数据不足"
        elif (one_month or 0) > 0 and (three_month or 0) > 0:
            status = "强势"
        elif (one_month or 0) > 0 and (three_month or 0) <= 0:
            status = "修复"
        elif (one_month or 0) < 0 and (three_month or 0) < 0:
            status = "弱势"
        else:
            status = "震荡"
        rows.append({"name": name, "return_1m": pct(one_month), "return_3m": pct(three_month), "status": status})
    rows.sort(key=lambda row: (row["return_1m"] is not None, row["return_1m"] or -999), reverse=True)
    return rows


def row_name(row: dict[str, Any]) -> str:
    key = find_key(row, ["基金简称", "基金名称", "名称", "板块", "行业", "概念"])
    if key:
        return str(row.get(key))
    return str(next(iter(row.values()), ""))


def row_flow(row: dict[str, Any]) -> float | None:
    amount_tokens = ["净流入-净额", "主力净流入-净额", "资金净流入-净额", "净额"]
    ratio_tokens = ["净占比", "净比", "占比", "涨跌幅", "%"]
    for token in amount_tokens:
        for key in row:
            if token in key and not any(ratio_token in key for ratio_token in ratio_tokens):
                value = to_float(row.get(key))
                if value is not None:
                    return value
    ignored_key_tokens = ["日期", "时间", "序号", "排名", "代码"]
    values = [
        to_float(value)
        for key, value in row.items()
        if not any(token in key for token in ignored_key_tokens)
    ]
    values = [value for value in values if value is not None]
    return values[-1] if values else None


def analyze_flows(market_data: dict[str, Any], key: str) -> dict[str, Any]:
    flow_sets = ((market_data.get("market") or {}).get(key) or {})
    by_name: dict[str, dict[str, float]] = defaultdict(dict)
    for period, rows in flow_sets.items():
        for row in rows or []:
            name = row_name(row)
            value = row_flow(row)
            if name and value is not None:
                by_name[name][period] = value

    summaries = []
    for name, flows in by_name.items():
        today = flows.get("今日")
        five = flows.get("5日")
        ten = flows.get("10日")
        positives = sum(1 for value in [today, five, ten] if value is not None and value > 0)
        negatives = sum(1 for value in [today, five, ten] if value is not None and value < 0)
        if positives >= 2 and (today or 0) > 0:
            status = "持续流入"
        elif (today or 0) > 0:
            status = "短线脉冲"
        elif negatives >= 2 and (today or 0) < 0:
            status = "持续流出"
        else:
            status = "分歧"
        summaries.append({"name": name, "today": today, "five_day": five, "ten_day": ten, "status": status})
    summaries.sort(key=lambda item: item.get("today") or 0, reverse=True)
    inflow = [item for item in summaries if (item.get("today") or 0) > 0]
    outflow = sorted([item for item in summaries if (item.get("today") or 0) < 0], key=lambda item: item.get("today") or 0)
    return {"inflow": inflow[:10], "outflow": outflow[:10], "all": summaries}


def fund_code(row: dict[str, Any]) -> str:
    key = find_key(row, ["基金代码", "代码"])
    return str(row.get(key, "")).zfill(6) if key and row.get(key) else ""


def fund_name(row: dict[str, Any]) -> str:
    key = find_key(row, ["基金简称", "基金名称", "名称"])
    return str(row.get(key)) if key and row.get(key) else row_name(row)


def fund_type(row: dict[str, Any]) -> str:
    key = find_key(row, ["基金类型", "类型"])
    return str(row.get(key)) if key and row.get(key) else "-"


def product_key(name: str) -> str:
    cleaned = re.sub(r"\s+", "", name)
    cleaned = re.sub(r"\(.*?\)|（.*?）", "", cleaned)
    cleaned = re.sub(r"(人民币|美元|港币)?[A-Z]$", "", cleaned, flags=re.I)
    cleaned = re.sub(r"(联接|发起式)?[A-Z]$", "", cleaned, flags=re.I)
    return cleaned


def share_preference(name: str, fee: Any) -> tuple[int, float]:
    fee_value = to_float(fee)
    if name.endswith("A") or "人民币A" in name:
        class_rank = 0
    elif re.search(r"[CE]$", name) or "人民币C" in name:
        class_rank = 2
    else:
        class_rank = 1
    return class_rank, fee_value if fee_value is not None else 999.0


def top30_note(row: dict[str, Any], period: str) -> str:
    one_week = to_float(row.get("近1周"))
    one_month = to_float(row.get(period))
    three_month = to_float(row.get("近3月"))
    one_year = to_float(row.get("近1年"))
    notes = []
    if one_month is not None and one_month >= 30 and (one_week is not None and one_week < 0):
        notes.append("月度强但短线回撤")
    if three_month is not None and one_month is not None and three_month >= one_month * 2:
        notes.append("趋势延续")
    if one_year is not None and one_year >= 100:
        notes.append("高弹性高波动")
    return "；".join(notes) or "强势候选"


def ranking_row(
    row: dict[str, Any],
    rank: int,
    period: str,
    held_codes: set[str],
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    detail = detail or {}
    stocks = top_stock_names(detail.get("stock_holdings") or [], limit=8)
    industries = []
    for industry_row in detail.get("industry_allocation") or []:
        key = find_key(industry_row, ["行业类别", "行业", "名称"])
        weight_key = find_key(industry_row, ["占净值比例", "比例", "占比"])
        weight = to_float(industry_row.get(weight_key)) if weight_key else None
        if key and industry_row.get(key) and (weight is None or weight > 0.5):
            industries.append(str(industry_row[key]))
    name = fund_name(row)
    theme_evidence = infer_theme_evidence(name, fund_type(row), stocks, industries)
    themes = theme_evidence["themes"]
    score = performance_score(row.get("近1月"), row.get("近3月"))
    product_profile = infer_product_profile(name, {"基金类型": fund_type(row)}, detail, detail.get("stock_holdings") or [])
    return {
        "rank": rank,
        "code": fund_code(row),
        "name": name,
        "performance_score": score,
        "return_1w": pct(to_float(row.get("近1周"))),
        "return_1m": pct(to_float(row.get("近1月"))),
        "return_3m": pct(to_float(row.get("近3月"))),
        "return_6m": pct(to_float(row.get("近6月"))),
        "return_ytd": pct(to_float(row.get("今年来"))),
        "return_1y": pct(to_float(row.get("近1年"))),
        "fund_type": fund_type(row),
        "product_profile": product_profile,
        "themes": [theme for theme in themes if theme != "未识别"] or ["未识别"],
        "held": fund_code(row) in held_codes,
        "note": top30_note(row, period),
        "top_stocks": stocks,
        "industries": industries[:5],
        "theme_confidence": theme_evidence["confidence"],
        "theme_basis": theme_evidence["basis"],
    }


def rank_sort_value(row: dict[str, Any], period: str) -> float | None:
    if period == PRIMARY_RANK_PERIOD:
        return performance_score(row.get("近1月"), row.get("近3月"))
    return to_float(row.get(period))


def dedupe_rank_rows(rows: list[dict[str, Any]], period: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        value = rank_sort_value(row, period)
        if value is None:
            continue
        grouped[product_key(fund_name(row))].append(row)

    representatives = []
    for candidates in grouped.values():
        candidates.sort(
            key=lambda row: (
                share_preference(fund_name(row), row.get("手续费")),
                0 if fund_type(row) != "-" else 1,
                -(rank_sort_value(row, period) or -999),
            )
        )
        representatives.append(candidates[0])
    representatives.sort(key=lambda row: rank_sort_value(row, period) or -999, reverse=True)
    return representatives


def analyze_rankings(market_data: dict[str, Any], funds: list[dict[str, Any]]) -> dict[str, Any]:
    rankings = market_data.get("rankings") or {}
    held_codes = {fund["code"] for fund in funds}
    details = market_data.get("ranking_fund_details") or {}
    all_rows = []
    for group_name, rows in rankings.items():
        for row in rows or []:
            enriched = dict(row)
            if group_name != "全部" and not enriched.get("基金类型"):
                enriched["基金类型"] = group_name
            all_rows.append(enriched)
    if not all_rows:
        return {
            "primary_period": PRIMARY_RANK_PERIOD,
            "primary_top30": [],
            "periods": {},
            "period_summaries": [],
            "features": [],
            "summary": "基金排行数据不足",
            "comparison": {},
        }

    period_results = {}
    period_summaries = []
    for period in RANK_PERIODS:
        top_rows = dedupe_rank_rows(all_rows, period)[:30]
        period_results[period] = top_rows
        theme_counter: Counter[str] = Counter()
        for row in top_rows:
            code = fund_code(row)
            detail = details.get(code, {})
            for theme in ranking_row(row, 0, period, held_codes, detail).get("themes", []):
                if theme != "未识别":
                    theme_counter[theme] += 1
        period_summaries.append(
            {
                "period": period,
                "count": len(top_rows),
                "top_return": pct(to_float(top_rows[0].get(period))) if top_rows else None,
                "themes": [{"name": key, "count": value} for key, value in theme_counter.most_common(5)],
            }
        )

    primary_rows = period_results.get(PRIMARY_RANK_PERIOD, [])[:30]
    primary_top30 = [
        ranking_row(row, index + 1, PRIMARY_RANK_PERIOD, held_codes, details.get(fund_code(row), {}))
        for index, row in enumerate(primary_rows)
    ]
    feature_counter: Counter[str] = Counter()
    type_counter: Counter[str] = Counter()
    for row in primary_top30:
        for theme in row.get("themes", []):
            if theme != "未识别":
                feature_counter[theme] += 1
        if row.get("fund_type") and row["fund_type"] != "-":
            type_counter[row["fund_type"]] += 1
    features = [{"name": key, "count": value} for key, value in feature_counter.most_common(10)]
    types = [{"name": key, "count": value} for key, value in type_counter.most_common(8)]
    available_periods = [period for period, rows in period_results.items() if rows]
    summary = "、".join(item["name"] for item in features[:5]) or "主题特征不明显"
    comparison = compare_current_with_top30(funds, primary_top30)
    return {
        "primary_period": PRIMARY_RANK_PERIOD,
        "primary_top30": primary_top30,
        "periods": period_results,
        "period_summaries": period_summaries,
        "available_periods": available_periods,
        "features": features,
        "types": types,
        "summary": summary,
        "comparison": comparison,
    }


def build_top_replacement_recommendations(
    current_rows: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    current_themes: set[str],
    current_stocks: set[str],
) -> list[dict[str, Any]]:
    eligible = [row for row in candidates if row.get("candidate_type") in {"可替换候选", "补充候选"}]
    def selection_score(row: dict[str, Any]) -> float:
        value = row.get("performance_score") if row.get("performance_score") is not None else -999
        flags = set((row.get("product_profile") or {}).get("risk_flags") or [])
        if "主动迷你规模" in flags:
            value -= 5
        elif "主动小规模" in flags:
            value -= 2.5
        return value

    eligible.sort(key=selection_score, reverse=True)
    used_current: set[str] = set()
    recommendations = []
    current_pool = [row for row in current_rows if row.get("decision_action") in {"替换候选", "减配去重", "观察", "保留但不加"}]

    for candidate in eligible:
        candidate_themes = set(market_themes(candidate.get("themes", [])))
        best_current = None
        best_key = None
        for current in current_pool:
            if current["code"] in used_current:
                continue
            current_themes_for_row = set(market_themes(current.get("themes", [])))
            shared = candidate_themes & current_themes_for_row
            current_score = current.get("performance_score")
            score_gap = None
            if candidate.get("performance_score") is not None and current_score is not None:
                score_gap = round(candidate["performance_score"] - current_score, 2)
            cross_theme_allowed = score_gap is not None and score_gap >= 15
            if candidate.get("candidate_type") == "可替换候选" and not shared and not cross_theme_allowed:
                continue
            replace_priority = {"替换候选": 0, "减配去重": 1, "观察": 2, "保留但不加": 3, "保留核心": 4}.get(current.get("decision_action"), 5)
            key = (replace_priority, -(score_gap if score_gap is not None else -999), 0 if shared else 1, current_score if current_score is not None else 999)
            if best_key is None or key < best_key:
                best_key = key
                best_current = current
        if best_current is None:
            continue
        used_current.add(best_current["code"])
        score_gap = None
        if candidate.get("performance_score") is not None and best_current.get("performance_score") is not None:
            score_gap = round(candidate["performance_score"] - best_current["performance_score"], 2)
        shared_themes = sorted(candidate_themes & set(market_themes(best_current.get("themes", []))))
        missing_hits = candidate.get("missing_themes_hit", [])
        overlap_stocks = candidate.get("overlap_stocks", [])
        if candidate.get("candidate_type") == "补充候选":
            reason = "补当前缺失强势主题"
            if missing_hits:
                reason += f"（{'、'.join(missing_hits)}）"
        elif missing_hits:
            reason = f"同主题更强，同时补缺失主题（{'、'.join(missing_hits)}）"
        elif shared_themes:
            reason = "同主题更强，替换弱势/重复持仓"
        else:
            reason = "跨主题替换弱势持仓，提升Top30强势主题暴露"
        if score_gap is not None:
            reason += f"；综合分高出当前持仓 {score_gap:.2f}"
        if shared_themes:
            reason += f"；共同主题：{'、'.join(shared_themes)}"
        if overlap_stocks:
            reason += f"；与现组合重仓股重叠{len(overlap_stocks)}只，建议替换而不是新增"
        risk_flags = (candidate.get("product_profile") or {}).get("risk_flags") or []
        if risk_flags:
            reason += f"；候选风险：{'、'.join(risk_flags)}"
        recommendations.append(
            {
                "candidate_type": candidate.get("candidate_type"),
                "candidate_code": candidate.get("code"),
                "candidate_name": candidate.get("name"),
                "candidate_score": candidate.get("performance_score"),
                "candidate_return_1m": candidate.get("return_1m"),
                "candidate_return_3m": candidate.get("return_3m"),
                "candidate_themes": candidate.get("themes", []),
                "candidate_product_profile": candidate.get("product_profile", {}),
                "replace_code": best_current.get("code"),
                "replace_name": best_current.get("name"),
                "replace_score": best_current.get("performance_score"),
                "replace_return_1m": best_current.get("return_1m"),
                "replace_return_3m": best_current.get("return_3m"),
                "replace_product_profile": best_current.get("product_profile", {}),
                "replace_action": best_current.get("decision_action"),
                "score_gap": score_gap,
                "reason": reason,
            }
        )
        if len(recommendations) >= 3:
            break
    return recommendations


def compare_current_with_top30(funds: list[dict[str, Any]], primary_top30: list[dict[str, Any]]) -> dict[str, Any]:
    current_theme_counter: Counter[str] = Counter()
    top_theme_counter: Counter[str] = Counter()
    stock_counter: Counter[str] = Counter()
    for fund in funds:
        for theme in market_themes(fund.get("themes", [])):
            current_theme_counter[theme] += 1
        for stock in fund.get("top_stocks", []):
            stock_counter[stock] += 1
    for row in primary_top30:
        for theme in row.get("themes", []):
            if theme != "未识别":
                top_theme_counter[theme] += 1

    current_themes = set(current_theme_counter)
    top_themes = set(top_theme_counter)
    covered = sorted(top_themes & current_themes, key=lambda theme: top_theme_counter[theme], reverse=True)
    missing = sorted(top_themes - current_themes, key=lambda theme: top_theme_counter[theme], reverse=True)
    duplicated_themes = [
        {"name": theme, "count": count}
        for theme, count in current_theme_counter.most_common()
        if count >= max(3, math.ceil(len(funds) * 0.45))
    ]
    duplicated_stocks = [
        {"name": stock, "count": count}
        for stock, count in stock_counter.most_common(10)
        if count >= 3
    ]

    top_by_theme: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in primary_top30:
        for theme in row.get("themes", []):
            top_by_theme[theme].append(row)

    current_rows = []
    for fund in funds:
        themes = market_themes(fund.get("themes", []))
        in_top30 = any(row["code"] == fund["code"] for row in primary_top30)
        same_theme_top = []
        for theme in themes:
            same_theme_top.extend(top_by_theme.get(theme, []))
        same_theme_top = {row["code"]: row for row in same_theme_top}.values()
        same_theme_best = max((row.get("return_1m") for row in same_theme_top if row.get("return_1m") is not None), default=None)
        one_month = fund.get("returns", {}).get("1月")
        three_month = fund.get("returns", {}).get("3月")
        current_score = performance_score(one_month, three_month)
        if in_top30:
            action = "保留核心"
            reason = "已进入近1月Top30，继续观察拥挤度和回撤"
        elif one_month is not None and one_month < 0:
            action = "替换候选"
            reason = "近1月为负，优先作为换弱对象"
        elif same_theme_best is not None and one_month is not None and one_month < same_theme_best - 15:
            action = "替换候选"
            reason = "同主题明显落后近1月Top30强势基金"
        elif any(theme in covered for theme in themes) and duplicated_themes:
            action = "减配去重"
            reason = "主题已覆盖且组合内重复暴露较高"
        else:
            action = "保留但不加"
            reason = "方向仍顺势，但新增资金可能只是同赛道加仓"
        current_rows.append(
            {
                "code": fund["code"],
                "name": fund["name"],
                "themes": themes,
                "top_stocks": fund.get("top_stocks", [])[:5],
                "product_profile": fund.get("product_profile", {}),
                "return_1m": one_month,
                "return_3m": three_month,
                "performance_score": current_score,
                "in_primary_top30": in_top30,
                "same_theme_top30_count": len(same_theme_top),
                "decision_action": action,
                "decision_reason": reason,
            }
        )

    candidates = []
    held_codes = {fund["code"] for fund in funds}
    current_stocks = set(stock_counter)
    for row in primary_top30:
        if row["code"] in held_codes:
            continue
        row_themes = set(market_themes(row.get("themes", [])))
        theme_overlap = row_themes & current_themes
        stock_overlap = set(row.get("top_stocks", [])) & current_stocks
        missing_overlap = row_themes & set(missing)
        high_stock_overlap = len(stock_overlap) >= 3
        if missing_overlap and theme_overlap:
            bucket = "可替换候选"
            reason = f"包含当前缺失主题（{'、'.join(sorted(missing_overlap))}），但也与现有持仓部分重叠，适合替换弱势持仓而非新增加仓"
        elif missing_overlap:
            bucket = "补充候选"
            reason = f"覆盖当前组合缺失的强势主题（{'、'.join(sorted(missing_overlap))}），适合小仓位补齐暴露"
        elif theme_overlap and high_stock_overlap:
            bucket = "不建议追高"
            reason = "与现有持仓主题和重仓股高度重叠，新增会放大同质化风险"
        elif theme_overlap:
            bucket = "可替换候选"
            reason = "与现有持仓同主题但综合业绩分更强，适合替换弱势持仓"
        else:
            bucket = "观察候选"
            reason = "排名靠前但与当前组合关系不强，先观察持续性"
        candidates.append({
            **row,
            "candidate_type": bucket,
            "candidate_reason": reason,
            "overlap_themes": sorted(theme_overlap),
            "missing_themes_hit": sorted(missing_overlap),
            "overlap_stocks": sorted(stock_overlap),
        })
    candidates.sort(key=lambda row: row.get("performance_score") if row.get("performance_score") is not None else -999, reverse=True)

    top_replacements = build_top_replacement_recommendations(current_rows, candidates, current_themes, current_stocks)

    concentration = "高" if duplicated_themes or duplicated_stocks else "中" if covered else "低"
    if concentration == "高":
        decision = "需要调仓，但目标是降低同质化风险和换弱留强，不是继续堆同一赛道。"
    elif missing:
        decision = "可以小幅调仓，优先补充当前组合缺失且持续强势的主题。"
    else:
        decision = "当前组合已覆盖主要强势主题，暂不建议追高新增Top30同类基金。"

    return {
        "current_theme_distribution": [{"name": key, "count": value} for key, value in current_theme_counter.most_common()],
        "top30_theme_distribution": [{"name": key, "count": value} for key, value in top_theme_counter.most_common()],
        "covered_themes": covered,
        "missing_themes": missing,
        "duplicated_themes": duplicated_themes,
        "duplicated_stocks": duplicated_stocks,
        "current_vs_top30": current_rows,
        "candidate_score_formula": "综合业绩分 = 0.70 × 近1月收益 + 0.30 × (近3月收益 / 3)",
        "candidate_type_definitions": [
            {"name": "补充候选", "description": "当前组合缺失的强势主题，适合小仓位补齐暴露"},
            {"name": "可替换候选", "description": "与现有持仓同主题但综合业绩分更强，适合替换弱势或重复持仓"},
            {"name": "不建议追高", "description": "与现有持仓主题和重仓股高度重叠，买入更多只是加大集中度"},
        ],
        "replacement_candidates": candidates[:12],
        "top_replacement_recommendations": top_replacements,
        "rebalance_decision": decision,
        "overlap_risk": concentration,
    }


def target_allocations(
    funds: list[dict[str, Any]],
    flow_analysis: dict[str, Any],
    ranking_analysis: dict[str, Any],
    total_amount: float,
) -> list[dict[str, Any]]:
    hot_themes = {item["name"] for item in ranking_analysis.get("features", [])[:4]}
    strong_flow_names = {item["name"] for item in flow_analysis.get("inflow", [])[:8] if item.get("status") == "持续流入"}
    weak_flow_names = {item["name"] for item in flow_analysis.get("outflow", [])[:8] if item.get("status") == "持续流出"}

    raw_targets = []
    for fund in funds:
        score = fund["score"]
        theme_bonus = 0
        if any(theme in hot_themes for theme in fund.get("themes", [])):
            theme_bonus += 4
        industry_names = {item["name"] for item in fund.get("industries", [])}
        if industry_names & strong_flow_names:
            theme_bonus += 5
        if industry_names & weak_flow_names:
            theme_bonus -= 6
        adjusted = max(0, min(100, score + theme_bonus))
        current = fund["current_weight"]
        target = current + (adjusted - 50) / 100 * 0.18
        reasons = []
        if adjusted >= 65:
            reasons.append("综合得分较强")
        elif adjusted < 45:
            reasons.append("表现或风险信号偏弱")
        if fund.get("is_core"):
            target = max(target, current * 0.5)
            reasons.append("核心持仓保留底仓")
        target = max(0.0, min(0.25, target))
        if target == 0.25 and current > target:
            reasons.append("当前占比超过单只基金上限")
        raw_targets.append([fund, target, adjusted, reasons])

    def reduction_floor(item: list[Any]) -> float:
        fund = item[0]
        current = fund["current_weight"]
        floor = current * 0.5 if fund.get("is_core") else 0.0
        return max(0.0, floor)

    # Enforce the conservative theme cap. A multi-theme fund counts fully toward
    # every identified theme; reduce low-score non-core funds first.
    for _ in range(20):
        theme_totals: dict[str, float] = defaultdict(float)
        for fund, target, _, _ in raw_targets:
            for theme in market_themes(fund.get("themes", [])):
                theme_totals[theme] += target
        breaches = [(theme, total - 0.40) for theme, total in theme_totals.items() if total > 0.400001]
        if not breaches:
            break
        changed = False
        for theme, excess in breaches:
            candidates = sorted(
                [item for item in raw_targets if theme in market_themes(item[0].get("themes", [])) and item[1] > 0],
                key=lambda item: (bool(item[0].get("is_core")), item[2]),
            )
            for item in candidates:
                if excess <= 0:
                    break
                reduction = min(max(0.0, item[1] - reduction_floor(item)), excess)
                if reduction <= 0:
                    continue
                item[1] -= reduction
                excess -= reduction
                item[3].append(f"受{theme}主题40%上限约束")
                changed = True
        if not changed:
            break

    # Never renormalize constrained targets upward. If the sum is above 100%,
    # reduce the weakest positions and leave any shortfall as cash.
    total = sum(item[1] for item in raw_targets)
    if total > 1:
        excess = total - 1
        for item in sorted(raw_targets, key=lambda value: (bool(value[0].get("is_core")), value[2])):
            if excess <= 0:
                break
            reduction = min(max(0.0, item[1] - reduction_floor(item)), excess)
            if reduction <= 0:
                continue
            item[1] -= reduction
            excess -= reduction
            item[3].append("组合总仓位约束")
    normalized = raw_targets

    rows = []
    for fund, target, adjusted, reasons in normalized:
        current = fund["current_weight"]
        delta = target - current
        high_volatility = bool(set(market_themes(fund.get("themes", []))) & {"半导体", "AI光模块", "通信", "AI软件", "新能源", "PCB/AI服务器链"})
        first_step_delta = max(-0.10, min(0.10, delta)) if high_volatility else delta
        first_step_target = current + first_step_delta
        if delta >= 0.05:
            action = "增配"
        elif delta >= 0.02:
            action = "小幅增配"
        elif delta <= -0.08 or (adjusted < 25 and target <= 0.005 and not fund.get("is_core")):
            action = "清仓候选"
        elif delta <= -0.02:
            action = "减配"
        else:
            action = "观察"
        if action in {"减配", "清仓候选"} and adjusted >= 60 and current > target:
            reasons.append("减配主要来自仓位约束而非基本面弱化")
        if action in {"增配", "小幅增配"}:
            reasons.append("目标仓位高于当前占比")
        if action == "观察" and delta > 0:
            reasons.append("增配幅度未达执行阈值")
        if action == "观察" and delta < 0:
            reasons.append("减配幅度未达执行阈值")
        if not reasons:
            reasons.append("维持组合平衡")
        rows.append(
            {
                "code": fund["code"],
                "name": fund["name"],
                "current_weight": round(current, 4),
                "target_weight": round(target, 4),
                "delta": round(delta, 4),
                "delta_amount": round(delta * total_amount, 2) if total_amount else None,
                "first_step_target_weight": round(first_step_target, 4),
                "first_step_delta": round(first_step_delta, 4),
                "first_step_delta_amount": round(first_step_delta * total_amount, 2) if total_amount else None,
                "action": action,
                "priority": "高" if action in {"增配", "清仓候选"} else ("中" if action in {"小幅增配", "减配"} else "低"),
                "score": fund["score"],
                "adjusted_score": round(adjusted, 1),
                "reasons": reasons,
            }
        )
    cash_weight = round(max(0.0, 1 - sum(row["target_weight"] for row in rows)), 4)
    first_step_cash = round(max(0.0, 1 - sum(row.get("first_step_target_weight", row["target_weight"]) for row in rows)), 4)
    if cash_weight >= 0.005:
        rows.append(
            {
                "code": "CASH",
                "name": "现金/待配置",
                "current_weight": 0.0,
                "target_weight": cash_weight,
                "delta": cash_weight,
                "delta_amount": round(cash_weight * total_amount, 2) if total_amount else None,
                "first_step_target_weight": first_step_cash,
                "first_step_delta": first_step_cash,
                "first_step_delta_amount": round(first_step_cash * total_amount, 2) if total_amount else None,
                "action": "观察",
                "priority": "低",
                "score": None,
                "adjusted_score": None,
                "reasons": ["受单只基金上限约束，保留待配置仓位"],
            }
        )
    return sorted(rows, key=lambda row: {"高": 0, "中": 1, "低": 2}[row["priority"]])


def align_long_term_scores(
    funds: list[dict[str, Any]],
    flow_analysis: dict[str, Any],
    ranking_analysis: dict[str, Any],
) -> None:
    """Align long-term scores with the documented 35/20/20/15/10 model."""
    hot_themes = {item["name"] for item in ranking_analysis.get("features", [])[:6]}
    top_codes = {row.get("code") for row in ranking_analysis.get("primary_top30", [])}
    strong_flow_names = {row["name"] for row in flow_analysis.get("inflow", []) if row.get("status") == "持续流入"}
    theme_counts: Counter[str] = Counter(theme for fund in funds for theme in market_themes(fund.get("themes", [])))
    momentum_values = []
    for fund in funds:
        values = [value for value in (fund.get("returns") or {}).values() if value is not None]
        momentum_values.append(mean(values) if values else None)
    valid_momentum = sorted(value for value in momentum_values if value is not None)

    def percentile(value: float | None) -> float | None:
        if value is None or not valid_momentum:
            return None
        if len(valid_momentum) == 1:
            return 50.0
        rank = sum(candidate <= value for candidate in valid_momentum) - 1
        return rank / (len(valid_momentum) - 1) * 100

    for fund, momentum in zip(funds, momentum_values):
        momentum_score = percentile(momentum)
        drawdown = abs(float(fund.get("max_drawdown") or 0))
        vol = float(fund.get("volatility") or 0)
        risk_score = max(0.0, 100 - min(70, drawdown * 2) - min(30, vol * 0.6))
        themes = set(market_themes(fund.get("themes", [])))
        industry_names = {item["name"] for item in fund.get("industries", [])}
        sector_score = 100.0 if industry_names & strong_flow_names else 75.0 if themes & hot_themes else 40.0
        ranking_score = 100.0 if fund.get("code") in top_codes else 70.0 if themes & hot_themes else 20.0
        maximum_overlap = max([theme_counts.get(theme, 0) for theme in themes] + [0])
        fit_score = 100.0 if maximum_overlap <= 1 else 60.0 if maximum_overlap == 2 else 20.0
        components = {
            "momentum": momentum_score,
            "risk": risk_score,
            "sector_style": sector_score,
            "ranking_confirmation": ranking_score,
            "portfolio_fit": fit_score,
        }
        if momentum_score is None:
            fund["score"] = 50.0
            fund["score_confidence"] = "低"
        else:
            fund["score"] = round(momentum_score * 0.35 + risk_score * 0.20 + sector_score * 0.20 + ranking_score * 0.15 + fit_score * 0.10, 1)
            fund["score_confidence"] = "高" if fund.get("confidence") == "高" else "中"
        fund["score_components"] = components


def validate_allocations(rows: list[dict[str, Any]], funds: list[dict[str, Any]]) -> list[str]:
    errors = []
    by_code = {fund["code"]: fund for fund in funds}
    total = sum(row.get("target_weight", 0) for row in rows)
    if abs(total - 1) > 0.001:
        errors.append(f"target weights sum to {total:.6f}, expected 1")
    first_step_total = sum(row.get("first_step_target_weight", row.get("target_weight", 0)) for row in rows)
    if abs(first_step_total - 1) > 0.001:
        errors.append(f"first-step weights sum to {first_step_total:.6f}, expected 1")
    for row in rows:
        if row.get("target_weight", 0) < -1e-9:
            errors.append(f"{row.get('code')} has negative target weight")
        if row.get("code") != "CASH" and row.get("target_weight", 0) > 0.250001:
            errors.append(f"{row.get('code')} exceeds single-fund cap")
        fund_for_row = by_code.get(row.get("code"))
        if fund_for_row and set(market_themes(fund_for_row.get("themes", []))) & {"半导体", "AI光模块", "通信", "AI软件", "新能源", "PCB/AI服务器链"} and abs(row.get("first_step_delta", 0)) > 0.100001:
            errors.append(f"{row.get('code')} exceeds first-step adjustment cap")
    theme_totals: dict[str, float] = defaultdict(float)
    for row in rows:
        fund = by_code.get(row.get("code"))
        if not fund:
            continue
        for theme in market_themes(fund.get("themes", [])):
            theme_totals[theme] += row.get("target_weight", 0)
    for theme, weight in theme_totals.items():
        if weight > 0.400001:
            errors.append(f"{theme} target {weight:.6f} exceeds theme cap")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--holdings", required=True, type=Path)
    parser.add_argument("--market-data", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    holdings_payload = load_json(args.holdings)
    holdings = holdings_payload.get("holdings", holdings_payload) if isinstance(holdings_payload, dict) else holdings_payload
    market_data = load_json(args.market_data)

    funds = analyze_funds(holdings, market_data)
    style = analyze_style(market_data)
    industry_flow = analyze_flows(market_data, "industry_flow")
    concept_flow = analyze_flows(market_data, "concept_flow")
    ranking = analyze_rankings(market_data, funds)
    align_long_term_scores(funds, industry_flow, ranking)
    total_amount = sum(float(to_float(item.get("amount")) or 0) for item in holdings)
    allocations = target_allocations(funds, industry_flow, ranking, total_amount)
    allocation_errors = validate_allocations(allocations, funds)

    payload = {
        "schema_version": 2,
        "as_of": market_data.get("as_of") or dt.datetime.now().isoformat(timespec="seconds"),
        "source": market_data.get("source", "akshare"),
        "warnings": market_data.get("warnings", []),
        "portfolio": {
            "total_amount": total_amount,
            "funds": funds,
            "allocations": allocations,
            "allocation_validation": {"status": "ok" if not allocation_errors else "failed", "errors": allocation_errors},
        },
        "market": {
            "style": style,
            "industry_flow": industry_flow,
            "concept_flow": concept_flow,
        },
        "rankings": ranking,
        "data_quality": market_data.get("source_status", []),
        "constraints": {
            "single_fund_cap": 0.25,
            "single_theme_cap": 0.40,
            "high_volatility_adjustment_cap": 0.10,
        },
        "disclaimer": "本报告仅基于公开数据生成基金级别分析，不构成投资建议或交易指令。",
    }
    write_json(args.output, payload)


if __name__ == "__main__":
    main()
