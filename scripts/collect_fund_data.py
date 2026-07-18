#!/usr/bin/env python3
"""Collect AkShare data for fund rotation analysis."""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import json
import math
import os
import re
from pathlib import Path
from typing import Any, Callable

from data_access import AkshareClient
from tushare_proxy import (
    PROVIDER as TUSHARE_PROVIDER,
    TushareProxyClient,
    aggregate_sector_flow,
    collect_fund_master,
    create_pro,
    health_source_matches,
    load_health,
    normalize_fund_nav,
    normalize_fund_portfolio,
    promotion_eligible,
    resolve_fund_ts_code,
)


STYLE_INDEXES = {
    "沪深300": "000300",
    "上证50": "000016",
    "中证500": "000905",
    "中证1000": "000852",
    "创业板指": "399006",
    "科创50": "000688",
    "国证成长": "399370",
    "国证价值": "399371",
    "中证红利": "000922",
}

STYLE_FALLBACKS = {
    "000300": "sh000300", "000016": "sh000016", "000905": "sh000905", "000852": "sh000852",
    "399006": "sz399006", "000688": "sh000688", "399370": "sz399370", "399371": "sz399371", "000922": "sh000922",
}
_ACTIVE_CLIENT: AkshareClient | None = None


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
        handle.write("\n")


def clean_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def df_to_records(df: Any, limit: int | None = None) -> list[dict[str, Any]]:
    if df is None:
        return []
    if hasattr(df, "empty") and df.empty:
        return []
    if limit is not None:
        df = df.head(limit)
    records = []
    for row in df.to_dict(orient="records"):
        records.append({str(k): clean_value(v) for k, v in row.items()})
    return records


def normalize_holdings(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict) and "holdings" in raw:
        raw = raw["holdings"]
    if not isinstance(raw, list):
        raise ValueError("holdings JSON must be a list or an object with a holdings list")

    holdings = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each holding must be an object")
        code = str(item.get("code", "")).strip()
        if not code:
            raise ValueError("each holding must include code")
        holding = dict(item)
        holding["code"] = code.zfill(6)
        holding["amount"] = parse_number(item.get("amount")) or 0
        if item.get("cost") is not None:
            holding["cost"] = parse_number(item["cost"]) or 0
        if item.get("current_weight") is not None:
            weight = parse_number(item["current_weight"]) or 0
            holding["current_weight"] = weight / 100 if weight > 1 else weight
        holding["is_core"] = bool(item.get("is_core", False))
        holding["tags"] = item.get("tags") or []
        holdings.append(holding)
    return holdings


def parse_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if math.isnan(float(value)):
            return None
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text or text in {"--", "-"}:
        return None
    multiplier = 1.0
    if "亿" in text:
        multiplier = 100000000.0
    elif "万" in text:
        multiplier = 10000.0
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return None
    return float(match.group(0)) * multiplier


def import_akshare() -> tuple[Any | None, str | None]:
    try:
        import akshare as ak  # type: ignore

        return ak, None
    except Exception as exc:  # pragma: no cover - depends on local environment
        return None, f"akshare unavailable: {exc}"


def safe_call(
    warnings: list[str],
    label: str,
    func: Callable[..., Any] | None,
    variants: list[dict[str, Any]],
    limit: int | None = None,
) -> list[dict[str, Any]]:
    if _ACTIVE_CLIENT is not None:
        return _ACTIVE_CLIENT.call(label, getattr(func, "__name__", label), variants, limit=limit)
    if func is None:
        warnings.append(f"{label}: AkShare function not found")
        return []
    errors = []
    for kwargs in variants:
        try:
            df = func(**kwargs)
            records = df_to_records(df, limit=limit)
            if records:
                return records
            errors.append(f"{kwargs}: empty")
        except Exception as exc:  # pragma: no cover - endpoint dependent
            errors.append(f"{kwargs}: {exc}")
    warnings.append(f"{label}: all variants failed or empty ({'; '.join(errors[:3])})")
    return []


def find_fund_name(records: list[dict[str, Any]], code: str) -> dict[str, Any]:
    for row in records:
        values = {str(v) for v in row.values() if v is not None}
        if code in values:
            return row
    return {}


def enrich_rankings_with_metadata(rankings: dict[str, list[dict[str, Any]]], fund_names: list[dict[str, Any]]) -> None:
    lookup = {}
    for row in fund_names:
        code = row_fund_code(row)
        if code:
            lookup[code] = row
    for rows in rankings.values():
        for row in rows or []:
            code = row_fund_code(row)
            metadata = lookup.get(code, {})
            if metadata.get("基金类型") and not row.get("基金类型"):
                row["基金类型"] = metadata["基金类型"]


def row_fund_code(row: dict[str, Any]) -> str:
    for key in ["基金代码", "代码"]:
        if row.get(key):
            return str(row[key]).zfill(6)
    return ""


def row_fund_name(row: dict[str, Any]) -> str:
    for key in ["基金简称", "基金名称", "名称"]:
        if row.get(key):
            return str(row[key])
    return ""


def ranking_value(row: dict[str, Any], period: str) -> float | None:
    value = row.get(period)
    return parse_number(value)


def product_key(name: str) -> str:
    cleaned = re.sub(r"\s+", "", name)
    cleaned = re.sub(r"\(.*?\)|（.*?）", "", cleaned)
    cleaned = re.sub(r"(人民币|美元|港币)?[A-Z]$", "", cleaned, flags=re.I)
    cleaned = re.sub(r"(联接|发起式)?[A-Z]$", "", cleaned, flags=re.I)
    return cleaned


def share_preference(name: str, fee: Any) -> tuple[int, float]:
    fee_value = parse_number(fee)
    if name.endswith("A") or "人民币A" in name:
        class_rank = 0
    elif re.search(r"[CE]$", name) or "人民币C" in name:
        class_rank = 2
    else:
        class_rank = 1
    return class_rank, fee_value if fee_value is not None else 999.0


def primary_ranking_codes(rankings: dict[str, list[dict[str, Any]]], period: str = "近1月", limit: int = 30) -> list[str]:
    rows = []
    for group_rows in rankings.values():
        rows.extend(group_rows or [])
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        value = ranking_value(row, period)
        code = row_fund_code(row)
        name = row_fund_name(row)
        if value is None or not code or not name:
            continue
        grouped.setdefault(product_key(name), []).append(row)

    representatives = []
    for candidates in grouped.values():
        candidates.sort(
            key=lambda row: (
                share_preference(row_fund_name(row), row.get("手续费")),
                -(ranking_value(row, period) or -999),
            )
        )
        representatives.append(candidates[0])
    representatives.sort(key=lambda row: ranking_value(row, period) or -999, reverse=True)
    return [row_fund_code(row) for row in representatives[:limit]]


def collect_ranking_details(ak: Any, warnings: list[str], rankings: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    details = {}
    today = dt.date.today()
    for code in primary_ranking_codes(rankings):
        details[code] = {
            **collect_fund_profile(ak, warnings, code, f"{code} top30"),
            "stock_holdings": safe_call(
                warnings,
                f"{code} top30 portfolio holdings",
                getattr(ak, "fund_portfolio_hold_em", None),
                [
                    {"symbol": code, "date": str(today.year)},
                    {"symbol": code, "date": str(today.year - 1)},
                    {"symbol": code},
                ],
                limit=20,
            ),
            "industry_allocation": safe_call(
                warnings,
                f"{code} top30 industry allocation",
                getattr(ak, "fund_portfolio_industry_allocation_em", None),
                [
                    {"symbol": code, "date": str(today.year)},
                    {"symbol": code, "date": str(today.year - 1)},
                    {"symbol": code},
                ],
                limit=20,
            ),
        }
    return details


def collect_fund_profile(ak: Any, warnings: list[str], code: str, label: str) -> dict[str, Any]:
    return {
        "basic_info": safe_call(
            warnings,
            f"{label} basic info",
            getattr(ak, "fund_individual_basic_info_xq", None),
            [{"symbol": code}],
            limit=None,
        ),
        "ths_info": safe_call(
            warnings,
            f"{label} ths info",
            getattr(ak, "fund_info_ths", None),
            [{"symbol": code}],
            limit=None,
        ),
        "asset_allocation": safe_call(
            warnings,
            f"{label} asset allocation",
            getattr(ak, "fund_individual_detail_hold_xq", None),
            [{"symbol": code}],
            limit=None,
        ),
    }


def collect_live(holdings: list[dict[str, Any]], args: argparse.Namespace) -> dict[str, Any]:
    global _ACTIVE_CLIENT
    ak, import_warning = import_akshare()
    warnings: list[str] = []
    if import_warning:
        warnings.append(import_warning)
        return {"warnings": warnings, "funds": {}, "market": {}, "rankings": {}}

    today = dt.date.today()
    start = today - dt.timedelta(days=430)
    start_str = start.strftime("%Y%m%d")
    end_str = today.strftime("%Y%m%d")
    _ACTIVE_CLIENT = AkshareClient(
        ak,
        Path(args.cache_dir) / today.strftime("%Y%m%d"),
        timeout=args.timeout,
        retries=args.retries,
        refresh=args.refresh,
        context={"mode": args.mode, "start": start_str, "end": end_str, "holdings": [item["code"] for item in holdings]},
    )

    fund_names = safe_call(
        warnings,
        "fund_name_em",
        getattr(ak, "fund_name_em", None),
        [{}],
        limit=None,
    )

    funds: dict[str, Any] = {}
    for holding in holdings:
        code = holding["code"]
        fund_payload = {
            "metadata": find_fund_name(fund_names, code),
            "nav": safe_call(
                warnings,
                f"{code} fund nav",
                getattr(ak, "fund_open_fund_info_em", None),
                [
                    {"symbol": code, "indicator": "单位净值走势"},
                    {"symbol": code, "indicator": "累计净值走势"},
                    {"symbol": code},
                ],
            )
            or safe_call(
                warnings,
                f"{code} etf nav",
                getattr(ak, "fund_etf_fund_info_em", None),
                [
                    {"symbol": code, "indicator": "单位净值走势"},
                    {"symbol": code},
                ],
            ),
        }
        if args.mode == "full":
            fund_payload.update(collect_fund_profile(ak, warnings, code, code))
            fund_payload["stock_holdings"] = safe_call(
                warnings,
                f"{code} portfolio holdings",
                getattr(ak, "fund_portfolio_hold_em", None),
                [
                    {"symbol": code, "date": str(today.year)},
                    {"symbol": code, "date": str(today.year - 1)},
                    {"symbol": code},
                ],
                limit=80,
            )
            fund_payload["industry_allocation"] = safe_call(
                warnings,
                f"{code} industry allocation",
                getattr(ak, "fund_portfolio_industry_allocation_em", None),
                [
                    {"symbol": code, "date": str(today.year)},
                    {"symbol": code, "date": str(today.year - 1)},
                    {"symbol": code},
                ],
                limit=80,
            )
        funds[code] = fund_payload

    indexes = {}
    for name, symbol in STYLE_INDEXES.items():
        indexes[name] = safe_call(
            warnings,
            f"{name} index",
            getattr(ak, "index_zh_a_hist", None),
            [
                {
                    "symbol": symbol,
                    "period": "daily",
                    "start_date": start_str,
                    "end_date": end_str,
                },
                {"symbol": symbol, "period": "daily"},
            ],
        )
        if not indexes[name]:
            fallback = STYLE_FALLBACKS[symbol]
            indexes[name] = safe_call(warnings, f"{name} index fallback", getattr(ak, "stock_zh_index_daily", None), [{"symbol": fallback}])
        if not indexes[name]:
            fallback = STYLE_FALLBACKS[symbol]
            indexes[name] = safe_call(warnings, f"{name} index tx fallback", getattr(ak, "stock_zh_index_daily_tx", None), [{"symbol": fallback}])

    industry_flow = {}
    concept_flow = {}
    for indicator in ["今日", "5日", "10日"]:
        industry_flow[indicator] = safe_call(
            warnings,
            f"industry flow {indicator}",
            getattr(ak, "stock_sector_fund_flow_rank", None),
            [
                {"indicator": indicator, "sector_type": "行业资金流"},
                {"indicator": indicator, "sector_type": "行业"},
            ],
            limit=80,
        )
        concept_flow[indicator] = safe_call(
            warnings,
            f"concept flow {indicator}",
            getattr(ak, "stock_sector_fund_flow_rank", None),
            [
                {"indicator": indicator, "sector_type": "概念资金流"},
                {"indicator": indicator, "sector_type": "概念"},
            ],
            limit=80,
        )

    rankings = {}
    for symbol in ["全部", "股票型", "混合型", "指数型", "QDII", "债券型"]:
        rankings[symbol] = safe_call(
            warnings,
            f"fund rank {symbol}",
            getattr(ak, "fund_open_fund_rank_em", None),
            [{"symbol": symbol}, {}],
            limit=200,
        )
    enrich_rankings_with_metadata(rankings, fund_names)
    ranking_fund_details = collect_ranking_details(ak, warnings, rankings) if args.mode == "full" else {}

    warnings.extend(item for item in _ACTIVE_CLIENT.warnings if item not in warnings)

    return {
        "warnings": warnings,
        "funds": funds,
        "market": {
            "style_indexes": indexes,
            "industry_flow": industry_flow,
            "concept_flow": concept_flow,
            "industry_boards": safe_call(
                warnings,
                "industry boards",
                getattr(ak, "stock_board_industry_name_em", None),
                [{}],
                limit=120,
            ),
            "concept_boards": safe_call(
                warnings,
                "concept boards",
                getattr(ak, "stock_board_concept_name_em", None),
                [{}],
                limit=120,
            ),
        },
        "rankings": rankings,
        "ranking_fund_details": ranking_fund_details,
        "source_status": _ACTIVE_CLIENT.statuses,
        "mode": args.mode,
    }


def mock_payload(holdings: list[dict[str, Any]]) -> dict[str, Any]:
    today = dt.date.today()
    dates = [(today - dt.timedelta(days=i)).isoformat() for i in range(260, -1, -20)]
    funds = {}
    for index, holding in enumerate(holdings):
        base = 1.0 + index * 0.04
        nav = []
        for i, day in enumerate(dates):
            nav.append({"净值日期": day, "单位净值": round(base * (1 + i * 0.012 - index * 0.002), 4)})
        funds[holding["code"]] = {
            "metadata": {"基金代码": holding["code"], "基金简称": holding.get("name") or f"模拟基金{index + 1}", "基金类型": "混合型"},
            "basic_info": [
                {"item": "基金代码", "value": holding["code"]},
                {"item": "基金名称", "value": holding.get("name") or f"模拟基金{index + 1}"},
                {"item": "最新规模", "value": f"{2 + index * 3:.2f}亿"},
                {"item": "基金类型", "value": "混合型-偏股"},
            ],
            "ths_info": [{"字段": "投资类型", "值": "混合型"}],
            "asset_allocation": [{"资产类型": "股票", "仓位占比": 85.0}, {"资产类型": "现金", "仓位占比": 8.0}],
            "nav": nav,
            "stock_holdings": [{"股票名称": "模拟科技", "占净值比例": 8.2}, {"股票名称": "模拟消费", "占净值比例": 5.1}],
            "industry_allocation": [{"行业类别": "电子", "占净值比例": 28.0}, {"行业类别": "食品饮料", "占净值比例": 18.0}],
        }

    style_indexes = {}
    for i, name in enumerate(STYLE_INDEXES):
        style_indexes[name] = [
            {"日期": day, "收盘": round(3000 + i * 40 + j * (8 - i % 4), 2)}
            for j, day in enumerate(dates)
        ]

    industry_flow = {
        "今日": [{"名称": "电子", "今日主力净流入-净额": 1500000000}, {"名称": "医药商业", "今日主力净流入-净额": -500000000}],
        "5日": [{"名称": "电子", "5日主力净流入-净额": 4200000000}, {"名称": "医药商业", "5日主力净流入-净额": -1800000000}],
        "10日": [{"名称": "电子", "10日主力净流入-净额": 6900000000}, {"名称": "医药商业", "10日主力净流入-净额": -2700000000}],
    }
    mock_rank_names = [
        "人工智能主题", "半导体先锋", "创新药精选", "港股科技", "红利低波",
        "通信设备精选", "算力基础设施", "集成电路龙头", "高端制造成长", "机器人产业",
        "数字经济先锋", "光模块精选", "科技创新成长", "医药生物精选", "先进封装主题",
        "消费复苏优选", "军工高端装备", "新能源车链", "资源周期精选", "云计算产业",
        "数据中心主题", "电子制造精选", "科创成长优选", "港股互联网", "可转债增强",
        "芯片设备主题", "AI应用精选", "低波红利增强", "创新硬件成长", "半导体材料",
    ]
    rankings = {
        "全部": [
            {"基金代码": f"00{i:04d}", "基金简称": name, "近1月": 32 - i * 0.2, "近3月": 80 - i * 0.8, "基金类型": "股票型"}
            for i, name in enumerate(mock_rank_names)
        ]
    }
    ranking_fund_details = {
        f"00{i:04d}": {
            "basic_info": [
                {"item": "基金代码", "value": f"00{i:04d}"},
                {"item": "基金名称", "value": mock_rank_names[i]},
                {"item": "最新规模", "value": f"{1 + i * 0.4:.2f}亿"},
                {"item": "基金类型", "value": "股票型-普通"},
            ],
            "ths_info": [{"字段": "投资类型", "值": "股票型"}],
            "asset_allocation": [{"资产类型": "股票", "仓位占比": 90.0}],
            "stock_holdings": [
                {"股票名称": "新易盛", "占净值比例": 9.5},
                {"股票名称": "中际旭创", "占净值比例": 8.8},
            ],
            "industry_allocation": [{"行业类别": "通信设备", "占净值比例": 40.0}],
        }
        for i in range(30)
    }
    return {
        "warnings": ["mock data enabled; live AkShare data was not requested"],
        "funds": funds,
        "market": {
            "style_indexes": style_indexes,
            "industry_flow": industry_flow,
            "concept_flow": industry_flow,
            "industry_boards": [],
            "concept_boards": [],
        },
        "rankings": rankings,
        "ranking_fund_details": ranking_fund_details,
    }


def apply_tushare_full_overlay(collected: dict[str, Any], holdings: list[dict[str, Any]], args: argparse.Namespace) -> dict[str, Any]:
    """Apply independently promoted Tushare datasets to the full portfolio collector."""
    health = load_health(Path(args.tushare_health))
    source_mismatch = None
    collected["provider_policy"] = args.provider_policy
    collected["provider_route"] = {"selected_provider": "AkShare及公开备用源", "promoted_datasets": []}
    if args.provider_policy == "akshare-only":
        return collected
    try:
        pro, ts_module, metadata = create_pro()
    except Exception as exc:
        collected["provider_route"]["runtime_status"] = f"{type(exc).__name__}: {exc}"
        if args.provider_policy == "auto" and any(
            promotion_eligible(health, name) for name in ["fund_nav", "fund_portfolio", "style_indexes", "industry_flow", "concept_flow"]
        ):
            collected.setdefault("warnings", []).append("Tushare运行时不可用，已使用AkShare及公开备用源。")
        return collected
    if health and not health_source_matches(health, metadata):
        health = dict(health)
        health["datasets"] = {}
        source_mismatch = "健康文件来自不同的Tushare来源；请重新运行健康检查和shadow。"

    client = TushareProxyClient(
        pro, ts_module, Path(args.cache_dir) / "tushare", metadata,
        timeout=args.timeout, retries=args.retries, refresh=args.refresh, context={"collector": "full", "mode": args.mode},
    )
    provider = str(metadata.get("provider") or TUSHARE_PROVIDER)
    enabled = lambda name: args.provider_policy == "shadow" or (args.provider_policy == "auto" and promotion_eligible(health, name))
    enabled_for = lambda specific, generic: args.provider_policy == "shadow" or (
        args.provider_policy == "auto" and (
            promotion_eligible(health, specific) or promotion_eligible(health, generic)
        )
    )
    today = dt.date.today()
    start = (today - dt.timedelta(days=400)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    basic_rows = []
    if any(enabled(name) for name in ["fund_basic", "fund_nav", "fund_portfolio"]) or any(
        enabled_for(f"fund_nav:{item['code']}", "fund_nav") for item in holdings
    ):
        basic_rows = collect_fund_master(client, [item["code"] for item in holdings])
    shadow: dict[str, Any] = {}
    promoted = []
    if enabled("fund_nav") or any(enabled_for(f"fund_nav:{item['code']}", "fund_nav") for item in holdings):
        for item in holdings:
            code = item["code"]
            if not enabled_for(f"fund_nav:{code}", "fund_nav"):
                continue
            ts_code = resolve_fund_ts_code(basic_rows, code)
            rows = client.call(f"fund_nav:{code}", "fund_nav", {"ts_code": ts_code, "start_date": start, "end_date": end}) if ts_code else []
            normalized = normalize_fund_nav(rows, today.isoformat())
            shadow[f"fund_nav:{code}"] = normalized
            if args.provider_policy == "auto" and normalized:
                collected["funds"].setdefault(code, {})["nav"] = normalized
                collected["funds"][code]["provider"] = provider
                promoted.append(f"fund_nav:{code}")
    if args.mode == "full" and enabled("fund_portfolio"):
        stock_rows = client.call("security_master", "stock_basic", {"exchange": "", "list_status": "L", "fields": "ts_code,symbol,name"})
        stock_names = {
            str(row.get("symbol") or row.get("ts_code") or "").split(".")[0]: str(row.get("name") or "")
            for row in stock_rows
        }
        for item in holdings:
            code = item["code"]
            ts_code = resolve_fund_ts_code(basic_rows, code)
            rows = client.call(f"fund_portfolio:{code}", "fund_portfolio", {"ts_code": ts_code}) if ts_code else []
            selected = normalize_fund_portfolio(rows, today.isoformat())
            normalized = [
                {
                    "股票代码": str(row.get("symbol") or row.get("stk_code") or "").split(".")[0],
                    "股票名称": stock_names.get(str(row.get("symbol") or row.get("stk_code") or "").split(".")[0]) or "名称待补",
                    "持仓占比": parse_number(row.get("stk_mkv_ratio")),
                    "持股数": parse_number(row.get("amount")),
                    "持股市值": parse_number(row.get("mkv")),
                    "报告期": row.get("end_date"),
                    "公告日期": row.get("ann_date"),
                    "provider": provider,
                }
                for row in selected
            ]
            shadow[f"fund_portfolio:{code}"] = normalized
            if args.provider_policy == "auto" and normalized:
                collected["funds"].setdefault(code, {})["stock_holdings"] = normalized
                collected["funds"][code]["portfolio_provider"] = provider
                promoted.append(f"fund_portfolio:{code}")
    if enabled("style_indexes") or any(enabled_for(f"style_indexes:{symbol}", "style_indexes") for symbol in STYLE_INDEXES.values()):
        for name, symbol in STYLE_INDEXES.items():
            if not enabled_for(f"style_indexes:{symbol}", "style_indexes"):
                continue
            suffix = "SZ" if symbol.startswith("399") else "SH"
            rows = client.call(f"style_index:{symbol}", "index_daily", {"ts_code": f"{symbol}.{suffix}", "start_date": start, "end_date": end})
            normalized = [
                {"日期": str(row.get("trade_date")), "收盘": parse_number(row.get("close")), "provider": provider}
                for row in rows if parse_number(row.get("close")) is not None
            ]
            shadow[f"style_index:{symbol}"] = normalized
            if args.provider_policy == "auto" and normalized:
                collected["market"]["style_indexes"][name] = normalized
                promoted.append(f"style_index:{symbol}")
    for health_key, api_name, output_key, sector_type in [
        ("industry_flow", "moneyflow_ind_ths", "industry_flow", "行业资金流"),
        ("concept_flow", "moneyflow_cnt_ths", "concept_flow", "概念资金流"),
    ]:
        if not enabled(health_key):
            continue
        flow_start = (today - dt.timedelta(days=35)).strftime("%Y%m%d")
        rows = client.call(health_key, api_name, {"start_date": flow_start, "end_date": end, "limit": 5000})
        shadow[health_key] = rows
        aggregated = {
            ("今日" if period == 1 else f"{period}日"): aggregate_sector_flow(
                rows,
                period=period,
                end_date=today.isoformat(),
                sector_type=sector_type,
                provider=provider,
            )
            for period in (1, 5, 10)
        }
        if args.provider_policy == "auto" and aggregated["5日"]:
            collected["market"][output_key] = aggregated
            promoted.append(health_key)
    collected.setdefault("source_status", []).extend(client.statuses)
    collected["provider_route"] = {
        "selected_provider": provider if promoted else "AkShare及公开备用源",
        "promoted_datasets": promoted,
        "endpoint_fingerprint": metadata["endpoint_fingerprint"],
        "source_mismatch": source_mismatch,
        "credential_risk": (
            "官方Tushare Pro token仅通过官方SDK使用并必须保密。"
            if metadata.get("provider_mode") == "official" else
            "第三方代理凭据不是官方Tushare Pro token；不要把官方token发送给代理。"
        ),
    }
    if args.provider_policy == "shadow":
        collected["provider_shadow"] = {"provider": provider, "datasets": shadow, "note": "影子数据不参与本次分析。"}
    return collected


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--holdings", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--mock", action="store_true", help="use deterministic mock market data")
    parser.add_argument("--mode", choices=["quick", "full"], default="full")
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--cache-dir", default="work/cache/fund-v2")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--provider-policy", choices=["auto", "shadow", "akshare-only"], default="auto")
    parser.add_argument("--tushare-health", default="work/tushare_proxy_health.json")
    parser.add_argument("--prompt-tushare-token", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.prompt_tushare_token and not os.environ.get("TUSHARE_TOKEN"):
        os.environ["TUSHARE_TOKEN"] = getpass.getpass("TUSHARE_TOKEN: ")

    holdings = normalize_holdings(load_json(args.holdings))
    collected = mock_payload(holdings) if args.mock else apply_tushare_full_overlay(collect_live(holdings, args), holdings, args)
    payload = {
        "as_of": dt.datetime.now().isoformat(timespec="seconds"),
        "source": "mock" if args.mock else "multi_source",
        "schema_version": 2,
        "data_revision": "2.3",
        "holdings": holdings,
        **collected,
    }
    write_json(args.output, payload)


if __name__ == "__main__":
    main()
