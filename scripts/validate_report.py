#!/usr/bin/env python3
"""Semantically validate schema-v2 weekly fund reports."""

from __future__ import annotations

import argparse
import datetime as dt
import re
from pathlib import Path
from typing import Any

from data_access import load_json
from report_contract import MANDATORY_SECTION_ORDER, NAV_ITEMS, REPORT_FORMAT_VERSION


REQUIRED_SECTIONS = ["kpi", "holdings", "style", "sector-week", "sector-today", "flows", "difference", "proxy", "etf", "replacement", "quality"]
AUDITABLE_ETF_BASES = {
    "后复权价格", "ETF累计净值", "ETF单位净值（无折算）", "IOPV同期快照",
    "未复权价格（已检查断点）", "新浪历史价格（已检查断点）",
}
CURRENT_REVISIONS = {"2.2", "2.3", "2.4", "2.5", "2.6", "2.7", "2.8"}
THREE_WEEK_SECTIONS = ["llm-synthesis", "three-week-portfolio", "three-week-style", "three-week-industry", "three-week-concept", "cache-audit"]


def fail(message: str) -> None:
    raise SystemExit(f"VALIDATION FAILED: {message}")


def parse_day(value: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def validate_delivery_readiness(data: dict[str, Any], require_complete: bool = False) -> None:
    readiness = data.get("delivery_readiness") or {}
    if not readiness:
        if require_complete:
            fail("delivery_readiness is missing; regenerate analysis before complete delivery")
        return
    sectors = (data.get("market") or {}).get("sector_top10") or {}
    actual_counts = {
        key: len(sectors.get(key) or [])
        for key in readiness.get("row_counts") or {}
    }
    if actual_counts != readiness.get("row_counts"):
        fail("delivery_readiness row counts do not match sector_top10")
    expected_core = {
        "行业近5个交易日收益": actual_counts.get("industry_return", 0) > 0,
        "概念近5个交易日收益": actual_counts.get("concept_return", 0) > 0,
        "行业近5日资金流入": actual_counts.get("industry_inflow", 0) > 0,
        "行业近5日资金流出": actual_counts.get("industry_outflow", 0) > 0,
        "概念近5日资金流入": actual_counts.get("concept_inflow", 0) > 0,
        "概念近5日资金流出": actual_counts.get("concept_outflow", 0) > 0,
        "行业最新行情": actual_counts.get("industry_today", 0) > 0,
        "概念最近有效收盘": actual_counts.get("concept_today", 0) > 0,
    }
    if readiness.get("core_requirements") != expected_core:
        fail("delivery_readiness core requirements do not match report rows")
    contradictions = readiness.get("consistency_errors") or []
    if contradictions:
        fail(f"cross-section data regression: {contradictions[0]}")
    expected_status = "complete" if all(expected_core.values()) and not readiness.get("unresolved_required_datasets") else "degraded"
    if readiness.get("status") != expected_status:
        fail("delivery_readiness status is inconsistent with core rows or unresolved datasets")
    if require_complete and readiness.get("status") != "complete":
        blockers = "；".join(readiness.get("blockers") or ["核心区块不完整"])
        fail(f"report is degraded and cannot be delivered as complete: {blockers}")


def validate_analysis(path: Path, require_complete: bool = False) -> dict[str, Any]:
    data = load_json(path)
    if data.get("schema_version") != 2:
        fail("legacy analysis schema; regenerate collection and analysis with schema_version 2")
    report_format = data.get("report_format_version")
    if report_format and report_format != REPORT_FORMAT_VERSION:
        fail(f"unsupported report format {report_format}")
    if report_format:
        contract = data.get("report_contract") or {}
        if contract.get("format_version") != report_format:
            fail("report contract version does not match report_format_version")
        if contract.get("mandatory_sections") != MANDATORY_SECTION_ORDER:
            fail("report contract mandatory sections do not match the installed format")
    if data.get("data_revision") in {"2.3", "2.4", "2.5", "2.6", "2.7", "2.8"}:
        if data.get("provider_policy") not in {"auto", "shadow", "akshare-only"}:
            fail("data_revision 2.3 must disclose provider_policy")
        if not isinstance(data.get("provider_route"), dict):
            fail("data_revision 2.3 must disclose provider_route")
    week = data.get("week") or {}
    end = parse_day(week.get("end_date"))
    if not end:
        fail("week.end_date is missing or invalid")
    portfolio = data.get("portfolio") or {}
    funds = portfolio.get("funds") or []
    if not funds:
        fail("portfolio funds are missing")
    coverage = portfolio.get("nav_coverage_weight")
    if coverage is None or not 0 <= float(coverage) <= 1.000001:
        fail("portfolio nav_coverage_weight is missing or invalid")
    if float(coverage) >= 0.90 and portfolio.get("weekly_return") is None:
        fail("portfolio weekly_return is missing despite >=90% NAV coverage")
    if float(coverage) < 0.90 and portfolio.get("weekly_return") is not None:
        fail("portfolio weekly_return must be null below 90% NAV coverage")
    if data.get("data_revision") in CURRENT_REVISIONS:
        if not portfolio.get("weight_basis_display") or not portfolio.get("weight_assumption"):
            fail("portfolio weight basis and assumption must be explained")
    for fund in funds:
        latest = parse_day(fund.get("latest_date"))
        if latest and latest > end:
            fail(f"{fund.get('code')} uses future NAV date {latest} after {end}")
        for key in ["one_month", "three_month", "max_drawdown_1y", "data_status"]:
            if key not in fund:
                fail(f"{fund.get('code')} missing {key}")
        if fund.get("weekly_score") is not None and not 0 <= float(fund["weekly_score"]) <= 100:
            fail(f"{fund.get('code')} weekly score is outside 0-100")
        if fund.get("weekly_score") is not None and float(fund.get("score_coverage") or 0) < 0.70:
            fail(f"{fund.get('code')} score published below 70% coverage")
        if data.get("data_revision") in CURRENT_REVISIONS and fund.get("weekly_score") is None:
            if not fund.get("score_missing_components") or not fund.get("score_unavailable_reason"):
                fail(f"{fund.get('code')} unscored state does not explain missing components")

    styles = (data.get("market") or {}).get("style_indexes") or []
    for row in styles:
        latest = parse_day(row.get("source_latest_date") or row.get("latest_date"))
        if row.get("week_return") is not None and (not latest or latest < end):
            fail(f"style index {row.get('name')} publishes a return without end-date coverage")
        if row.get("freshness_status") == "stale_source" and row.get("week_return") is not None:
            fail(f"stale style source published a return for {row.get('name')}")
    dividend = next((row for row in styles if row.get("name") == "中证红利"), None)
    if data.get("data_revision") in CURRENT_REVISIONS and dividend and dividend.get("week_return") is None:
        attempts = [row for row in data.get("source_audit") or [] if "000922" in str(row)]
        if any(row.get("status") in {"ok", "fallback_used"} and parse_day(row.get("source_date")) == end for row in attempts):
            fail("中证红利 has a current successful source but is reported as insufficient")

    sectors = (data.get("market") or {}).get("sector_top10") or {}
    for key in ["industry_return", "concept_return"]:
        for row in sectors.get(key) or []:
            basis = str(row.get("return_basis") or "")
            if row.get("week_return") is None or "今日" in basis or "代理" in basis:
                fail(f"{key} contains non-weekly or proxy return for {row.get('name')}")
            if data.get("data_revision") in {"2.1", "2.2", "2.3"} and not row.get("universe_scope"):
                fail(f"{key} missing universe scope for {row.get('name')}")
            source_day = parse_day(row.get("source_date"))
            if data.get("data_revision") in {"2.1", "2.2", "2.3"} and (not source_day or source_day != end):
                fail(f"{key} source date for {row.get('name')} must equal report end date")
            if (row.get("cache_age_days") or 0) < 0:
                fail(f"{key} has negative cache age for {row.get('name')}")
            if data.get("data_revision") in CURRENT_REVISIONS:
                if row.get("classification_status") not in {"已分类", "待分类"}:
                    fail(f"{key} lacks classification status for {row.get('name')}")
                if row.get("classification_status") == "待分类" and not row.get("classification_basis"):
                    fail(f"{key} lacks pending-classification reason for {row.get('name')}")
                if not row.get("flow_status_reason"):
                    fail(f"{key} lacks flow evidence explanation for {row.get('name')}")
    for key in ["industry_today", "concept_today"]:
        for row in sectors.get(key) or []:
            if row.get("week_return") is not None or row.get("today_return") is None:
                fail(f"{key} mixes today and weekly returns for {row.get('name')}")
    flow_sign_field = "five_day_flow" if data.get("data_revision") in {"2.4", "2.5", "2.6", "2.7", "2.8"} else "today_flow"
    for key in ["industry_inflow", "concept_inflow"]:
        if any((row.get(flow_sign_field) or 0) <= 0 for row in sectors.get(key) or []):
            fail(f"{key} contains non-positive {flow_sign_field}")
    for key in ["industry_outflow", "concept_outflow"]:
        if any((row.get(flow_sign_field) or 0) >= 0 for row in sectors.get(key) or []):
            fail(f"{key} contains non-negative {flow_sign_field}")
    if data.get("data_revision") in {"2.4", "2.5", "2.6", "2.7", "2.8"}:
        for key in ["industry_inflow", "concept_inflow", "industry_outflow", "concept_outflow"]:
            for row in sectors.get(key) or []:
                if row.get("week_return") is None or not row.get("return_basis"):
                    fail(f"{key} lacks auditable matched 5-day return for {row.get('name')}")
                if row.get("source_date") != week.get("end_date") or row.get("flow_unit") != "元":
                    fail(f"{key} has wrong period or flow unit for {row.get('name')}")
                if data.get("data_revision") in {"2.5", "2.6", "2.7", "2.8"} and "同花顺" in str(row.get("universe_scope")):
                    if not any(token in str(row.get("return_basis")) for token in ["首尾比值", "pct_change复合"]):
                        fail(f"{key} has unauditable Tushare return basis for {row.get('name')}")
        collection_day = week.get("collection_trade_date") or week.get("end_date")
        for key in ["industry_today_inflow", "concept_today_inflow", "industry_today_outflow", "concept_today_outflow"]:
            for row in sectors.get(key) or []:
                if row.get("week_return") is not None or row.get("today_return") is None:
                    fail(f"{key} mixes completed-week and current-day returns for {row.get('name')}")
                if row.get("source_date") != collection_day or row.get("flow_unit") != "元":
                    fail(f"{key} has wrong current-day date or flow unit for {row.get('name')}")
    oil_rows = [row for rows in sectors.values() if isinstance(rows, list) for row in rows if isinstance(row, dict) and row.get("name") == "油田服务"]
    for row in oil_rows:
        if row.get("theme_l1") != "资源能源" or row.get("theme_l2") != "油气产业链/油服":
            fail("油田服务 must map to 资源能源/油气产业链/油服")

    etfs = data.get("candidate_etfs") or []
    if not etfs:
        fail("candidate_etfs are missing")
    for etf in etfs:
        basis = str(etf.get("return_basis") or "")
        if etf.get("week_return") is not None and not (basis in AUDITABLE_ETF_BASES or basis.startswith("联接基金")):
            fail(f"{etf.get('code')} has unauditable return basis {basis}")
        if "premium_rate" not in etf or "turnover" not in etf or "updated_at" not in etf:
            fail(f"{etf.get('code')} missing trading-quality fields")
        if data.get("data_revision") in {"2.1", "2.2", "2.3"}:
            for key in ["price_source", "turnover_source", "premium_basis", "premium_as_of"]:
                if key not in etf:
                    fail(f"{etf.get('code')} missing {key}")
            if etf.get("premium_basis") == "收盘净值溢价" and parse_day(etf.get("premium_as_of")) != end:
                fail(f"{etf.get('code')} closing premium is not aligned to report end date")
            if basis == "ETF累计净值" and etf.get("nav_basis") != "累计净值":
                fail(f"{etf.get('code')} cumulative NAV return lacks cumulative nav_basis")
        if etf.get("split_detected") and basis == "ETF单位净值（无折算）":
            fail(f"{etf.get('code')} uses unit NAV across a detected split")
        if etf.get("split_detected") and "份额折算" not in (etf.get("corporate_actions") or etf.get("quality_flags") or []):
            fail(f"{etf.get('code')} detected split is not disclosed")
        if data.get("data_revision") in {"2.7", "2.8"}:
            eod = etf.get("eod_quality") or {}
            live = etf.get("live_snapshot") or {}
            for key in ["as_of", "close", "turnover", "premium_rate", "premium_basis"]:
                if key not in eod:
                    fail(f"{etf.get('code')} missing eod_quality.{key}")
            if eod.get("premium_basis") == "收盘净值溢价" and parse_day(eod.get("as_of")) != end:
                fail(f"{etf.get('code')} eod premium is not aligned to report end date")
            if live.get("trade_time") and parse_day(live.get("trade_time")) and parse_day(live.get("trade_time")) > end and etf.get("premium_basis") == "实时IOPV溢价":
                fail(f"{etf.get('code')} later live premium contaminated report-end premium")
            if etf.get("execution_ready") and not live.get("fresh_within_5m"):
                fail(f"{etf.get('code')} execution_ready uses a stale live snapshot")
            if etf.get("return_status") == "data_conflict" and etf.get("weekly_score") is not None:
                fail(f"{etf.get('code')} data-conflict return entered scoring")
        if etf.get("weekly_score") is not None and not 0 <= float(etf["weekly_score"]) <= 100:
            fail(f"{etf.get('code')} weekly score is outside 0-100")

    warnings = data.get("warnings") or []
    for status in data.get("data_quality") or []:
        if status.get("status") == "fallback_used" and any(str(item).startswith(f"{status.get('dataset')}：") for item in warnings):
            fail(f"recovered dataset {status.get('dataset')} is still reported as unresolved")
        if status.get("requirement", "required") == "required" and status.get("status") in {"failed", "partial"} and not any(str(item).startswith(f"{status.get('dataset')}：") for item in warnings):
            fail(f"unresolved dataset {status.get('dataset')} is missing from warnings")
    expected_unresolved = sum(
        row.get("requirement", "required") == "required" and row.get("status") in {"failed", "partial"}
        for row in data.get("data_quality") or []
    )
    if (data.get("quality_summary") or {}).get("unresolved_datasets", expected_unresolved) != expected_unresolved:
        fail("quality_summary unresolved count does not match dataset statuses")
    validate_delivery_readiness(data, require_complete=require_complete)

    comparison = data.get("comparison") or {}
    if data.get("data_revision") in CURRENT_REVISIONS:
        conclusion = comparison.get("weekly_conclusion") or {}
        for key in ["market_summary", "flow_summary", "coverage_summary", "overlap_summary", "decision_summary", "confidence_note"]:
            if not conclusion.get(key):
                fail(f"weekly conclusion missing {key}")
        confirmed_names = {row.get("name") for row in conclusion.get("confirmed_leaders") or []}
        if any(row.get("flow_status") != "持续流入" for row in conclusion.get("confirmed_leaders") or []):
            fail("weekly conclusion confirms a sector without sustained inflow")
        for row in conclusion.get("unconfirmed_leaders") or []:
            if row.get("name") in confirmed_names:
                fail("weekly conclusion mixes confirmed and unconfirmed leaders")
    replacements = comparison.get("replacement_top3") or []
    if len(replacements) < 3 and comparison.get("replacement_status") != "insufficient_evidence":
        fail("replacement list below three must declare insufficient_evidence")
    for row in replacements:
        required = ["replace_score", "candidate_score", "score_gap", "evidence", "risk_flags", "candidate_return_basis"]
        for key in required:
            if row.get(key) is None:
                fail(f"replacement {row.get('candidate_code')} missing {key}")
        if float(row.get("score_gap")) < 5:
            fail(f"replacement {row.get('candidate_code')} score gap below five")
        if not row.get("evidence"):
            fail(f"replacement {row.get('candidate_code')} has no sector evidence")
        if row.get("candidate_premium_rate") is not None and float(row["candidate_premium_rate"]) >= 2:
            fail(f"high-premium ETF {row.get('candidate_code')} appears in actionable replacements")
        if row.get("candidate_kind") != "fund" and row.get("candidate_premium_rate") is None:
            fail(f"ETF {row.get('candidate_code')} has no confirmed premium in actionable replacements")
        if row.get("candidate_return_basis") == "不可确认" or "追高风险" in (row.get("risk_flags") or []):
            fail(f"ineligible ETF {row.get('candidate_code')} appears in actionable replacements")
        if row.get("execution_ready"):
            if row.get("suggested_first_step_weight") is None or not 0.03 <= float(row.get("suggested_first_step_weight")) <= 0.05:
                fail(f"replacement {row.get('candidate_code')} first-step weight outside 3%-5%")
        elif row.get("suggested_first_step_weight") is not None or row.get("action") == "小幅分批":
            fail(f"replacement {row.get('candidate_code')} exposes an execution action without fresh live evidence")

    allocation_validation = portfolio.get("allocation_validation")
    if allocation_validation and allocation_validation.get("status") != "ok":
        fail(f"allocation invariants failed: {allocation_validation.get('errors')}")
    if data.get("data_revision") == "2.8":
        margin = (data.get("market") or {}).get("margin_leverage") or {}
        if margin.get("model_version") != "margin-leverage-v1":
            fail("v2.8 margin leverage model/version is missing")
        if margin.get("scope") != "SSE+SZSE" or margin.get("action_policy") != "display_only":
            fail("margin leverage must use SSE+SZSE and remain display_only")
        margin_day = parse_day(margin.get("as_of"))
        if margin_day and margin_day > end:
            fail("margin leverage uses data after report end date")
        current_margin = margin.get("current") or {}
        if margin.get("status") == "complete":
            if margin_day != end:
                fail("complete margin leverage data must align to report end date")
            financing = current_margin.get("financing_balance")
            lending = current_margin.get("lending_balance")
            total = current_margin.get("margin_balance")
            if None in {financing, lending, total} or not total:
                fail("complete margin leverage data lacks balances")
            if abs((float(financing) + float(lending)) / float(total) - 1) > 0.001:
                fail("margin balance identity exceeds 0.1% tolerance")
            normalization = margin.get("normalization") or {}
            if normalization.get("financing_to_float_cap") is None or normalization.get("financing_buy_to_turnover") is None:
                fail("complete margin leverage lacks same-day normalized metrics")
        elif not margin.get("data_quality"):
            fail("degraded margin leverage must explain its missing evidence")
        for key in ["heat", "deleveraging_pressure"]:
            block = margin.get(key) or {}
            score = block.get("score")
            if score is not None and not 0 <= float(score) <= 100:
                fail(f"margin {key} score is outside 0-100")
            if score is not None and float(block.get("coverage") or 0) < 0.75:
                fail(f"margin {key} score published below 75% evidence coverage")
        normalization = margin.get("normalization") or {}
        if (normalization.get("financing_to_float_cap") is None or normalization.get("financing_buy_to_turnover") is None) and (margin.get("heat") or {}).get("score") is not None:
            fail("margin heat score published without density and trading intensity")
        bse = (current_margin.get("exchanges") or {}).get("BSE") or current_margin.get("bse_display_only")
        if bse and margin.get("scope") != "SSE+SZSE":
            fail("BSE must remain outside the long-history SSE+SZSE score")
        serialized = str(margin)
        for forbidden in ["上涨空间很大", "马上调整", "必然见底", "一定看多"]:
            if forbidden in serialized:
                fail(f"margin conclusion contains forbidden deterministic claim: {forbidden}")
        for row in (((data.get("three_week_analysis") or {}).get("margin_leverage") or {}).get("periods") or []):
            if row.get("average_financing_intensity") is None and row.get("data_status") == "ok":
                fail(f"margin three-week period {row.get('period_id')} hides missing trading intensity")
            for score_key, coverage_key in (
                ("heat_score", "heat_coverage"),
                ("deleveraging_pressure_score", "deleveraging_pressure_coverage"),
            ):
                if row.get(score_key) is not None and float(row.get(coverage_key) or 0) < 0.75:
                    fail(f"margin three-week period {row.get('period_id')} publishes {score_key} below 75% coverage")
        for row in margin.get("broad_index_series") or []:
            source_day = parse_day(row.get("trade_date"))
            if source_day and source_day > end:
                fail("margin broad-index trajectory uses data after report end date")
        for row in margin.get("historical_comparisons") or []:
            for value_key, date_key in (
                ("peak_financing_to_float_cap", "peak_financing_to_float_cap_date"),
                ("peak_financing_buy_to_turnover", "peak_financing_buy_to_turnover_date"),
            ):
                if row.get(value_key) is not None and not parse_day(row.get(date_key)):
                    fail(f"margin historical comparison {row.get('label')} lacks date for {value_key}")
        concentration = margin.get("concentration") or {}
        if int(concentration.get("history_sample_count") or 0) < 500 and concentration.get("top100_percentile") is not None:
            fail("margin concentration percentile published below 500 historical samples")
        calibration = margin.get("calibration") or {}
        if calibration:
            if calibration.get("model_version") != margin.get("model_version") or not calibration.get("evidence_hash"):
                fail("margin calibration model version or evidence hash is invalid")
            calibration_end = parse_day(calibration.get("end_date"))
            if not calibration_end or calibration_end > end:
                fail("margin calibration uses information after report end date")
            for row in (calibration.get("heat_bands") or []) + (calibration.get("pressure_bands") or []):
                if int(row.get("sample_count") or 0) < 30:
                    probability_fields = [key for key in row if key.endswith("_rate")]
                    if any(row.get(key) is not None for key in probability_fields):
                        fail("margin calibration publishes probability for an insufficient sample")
    if data.get("data_revision") in {"2.6", "2.7", "2.8"}:
        three = data.get("three_week_analysis") or {}
        periods = three.get("periods") or []
        if len(periods) != 3 or [row.get("period_id") for row in periods] != ["W-2", "W-1", "W0"]:
            fail("v2.6+ must contain ordered W-2/W-1/W0 periods")
        for index, period in enumerate(periods):
            baseline = parse_day(period.get("baseline_date"))
            start = parse_day(period.get("start_date"))
            period_end = parse_day(period.get("end_date"))
            if not baseline or not start or not period_end or not baseline < start <= period_end <= end:
                fail(f"invalid three-week boundary for {period.get('period_id')}")
            if index < 2 and period.get("completeness") != "complete":
                fail(f"historical period {period.get('period_id')} must be complete")
            if period.get("completeness") == "partial" and period.get("eligible_for_action"):
                fail(f"partial period {period.get('period_id')} cannot trigger actions")
        three_portfolio = three.get("portfolio") or {}
        for period in periods:
            pid = period["period_id"]
            coverage = (three_portfolio.get("coverage") or {}).get(pid)
            value = (three_portfolio.get("weekly_returns") or {}).get(pid)
            if coverage is None:
                fail(f"portfolio coverage missing for {pid}")
            if float(coverage) < 0.90 and value is not None:
                fail(f"portfolio return published below 90% coverage for {pid}")
        completed_periods = [period for period in periods if period.get("completeness") == "complete"]
        if completed_periods and all((three_portfolio.get("weekly_returns") or {}).get(period["period_id"]) is not None for period in completed_periods):
            if three_portfolio.get("completed_weeks_compound_return") is None:
                fail("complete-week compound return is missing")
        if periods and periods[-1].get("completeness") == "partial" and "进行中周" not in str(three_portfolio.get("compound_basis")):
            fail("partial W0 compound basis is not disclosed")
        style_regime = three.get("style_regime") or {}
        if style_regime.get("action_period") == "W0" and periods[-1].get("completeness") == "partial":
            fail("partial W0 cannot be the style action period")
        for row in (three.get("industries") or []) + (three.get("concepts") or []):
            valid_complete = sum(
                period.get("completeness") == "complete"
                and (row.get("periods") or {}).get(period.get("period_id"), {}).get("data_status") == "ok"
                for period in periods
            )
            if valid_complete < 2 and row.get("rotation_state") != "数据不足":
                fail(f"{row.get('name')} has a rotation state without two complete weeks")
            if not row.get("portfolio_coverage") or row.get("coverage_weight") is None or not row.get("coverage_basis"):
                fail(f"{row.get('name')} has no auditable portfolio coverage result")
        evidence = three.get("evidence_index") or {}
        margin = (data.get("market") or {}).get("margin_leverage") or {}
        if data.get("data_revision") == "2.8" and margin.get("historical_comparisons"):
            if not any(str(key).startswith("margin:SSE+SZSE:H") for key in evidence):
                fail("margin historical comparisons lack auditable evidence references")
        synthesis = data.get("llm_synthesis") or {}
        if synthesis.get("evidence_hash") != data.get("llm_evidence_hash"):
            fail("LLM synthesis evidence hash mismatch")
        if any(ref not in evidence for ref in synthesis.get("evidence_refs") or []):
            fail("LLM synthesis contains unknown evidence references")
        if not (data.get("cache") or {}).get("database"):
            fail("v2.6+ must disclose incremental cache database")
    return data


def validate_html(path: Path, data: dict[str, Any]) -> None:
    text = path.read_text(encoding="utf-8")
    for section in REQUIRED_SECTIONS:
        if f'data-section="{section}"' not in text:
            fail(f"HTML missing section {section}")
    if data.get("data_revision") in {"2.6", "2.7", "2.8"}:
        for section in THREE_WEEK_SECTIONS:
            if f'data-section="{section}"' not in text:
                fail(f"HTML missing three-week section {section}")
        if len(re.findall(r'data-row="three-week-fund"', text)) < len(((data.get("three_week_analysis") or {}).get("portfolio") or {}).get("funds") or []):
            fail("HTML three-week portfolio rows are incomplete")
        for label in ["三周基金轮动复盘", "完整周复合", "动作依据", "轨迹："]:
            if label not in text:
                fail(f"HTML does not explain three-week field {label}")
        if "未验证覆盖" in text:
            fail("HTML exposes obsolete unaudited coverage status")
    if data.get("data_revision") == "2.8":
        for label in [
            "A股杠杆温度", "当前两融余额", "融资杠杆密度", "融资交易强度",
            "杠杆热度", "去杠杆压力", "滚动5年分位", "历史阶段同口径比较",
            "只作市场环境展示", "低杠杆不代表上涨空间必然较大",
            "近60日两融余额", "近60日融资杠杆密度", "近60日宽基代表",
        ]:
            if label not in text:
                fail(f"HTML margin section does not explain {label}")
    if data.get("report_format_version"):
        if f'<meta name="fund-report-format" content="{REPORT_FORMAT_VERSION}">' not in text:
            fail("HTML does not declare the report format meta tag")
        if f'data-report-format="{REPORT_FORMAT_VERSION}"' not in text:
            fail("HTML body does not declare the report format")
        for required in [
            '<html lang="zh-CN">',
            '<meta name="viewport"',
            'class="skip-link"',
            '<main id="report-main">',
            '<nav class="report-nav" aria-label="报告导航">',
            "@media(max-width:900px)",
            "@media print",
        ]:
            if required not in text:
                fail(f"HTML format contract missing {required}")
        positions = []
        for section in MANDATORY_SECTION_ORDER:
            marker = f'data-section="{section}"'
            count = text.count(marker)
            if count != 1:
                fail(f"HTML section {section} must occur exactly once; found {count}")
            positions.append(text.index(marker))
        if positions != sorted(positions):
            fail("HTML mandatory sections are not in contract order")
        for index, section in enumerate(MANDATORY_SECTION_ORDER):
            start = positions[index]
            end = positions[index + 1] if index + 1 < len(positions) else text.index("</main>")
            visible = re.sub(r"<[^>]+>", "", text[start:end]).strip()
            if len(visible) < 8:
                fail(f"HTML section {section} is blank")
        for anchor, _label in NAV_ITEMS:
            if f'href="#{anchor}"' not in text or f'id="{anchor}"' not in text:
                fail(f"HTML navigation target {anchor} is incomplete")
    expected = {
        "holding": len((data.get("portfolio") or {}).get("funds") or []),
        "etf": len(data.get("candidate_etfs") or []),
        "replacement": len((data.get("comparison") or {}).get("replacement_top3") or []),
    }
    for row_type, minimum in expected.items():
        count = len(re.findall(fr'data-row="{row_type}"', text))
        if count < minimum:
            fail(f"HTML has {count} {row_type} rows, expected at least {minimum}")
    if (data.get("comparison") or {}).get("replacement_status") == "insufficient_evidence" and "decision-gap" not in text:
        fail("HTML does not explain insufficient replacement evidence")
    for internal in [
        "insufficient_data", "insufficient_evidence", "optional_unavailable", "deterministic_fallback",
        "not_required", "fallback_used", "stale_source",
    ]:
        if internal in text:
            fail(f"HTML exposes internal state {internal}")
    for label in ["当前组合占比", "本周收益", "近1月收益", "近3月收益", "近1年最大回撤", "周度综合分", "建议动作"]:
        if label not in text:
            fail(f"HTML does not label holding field {label}")
    if data.get("data_revision") in CURRENT_REVISIONS and "组合相关主题估算占比" not in text:
        fail("HTML does not explain sector coverage percentage")
    if data.get("data_revision") in {"2.4", "2.5", "2.6", "2.7", "2.8"}:
        if 'data-section="sector-today-flow"' not in text or "不参与上周结论" not in text:
            fail("HTML does not isolate post-period current-day flow")
        if "报告期5日资金流入" not in text or "亿元" not in text:
            fail("HTML does not label completed-period flow and units")
        if "收益口径：not_applicable" in text:
            fail("HTML exposes a missing return basis in flow rankings")
    if len(text) < 14000:
        fail("HTML report is unexpectedly small")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis", required=True, type=Path)
    parser.add_argument("--html", required=True, type=Path)
    parser.add_argument(
        "--require-complete", action="store_true",
        help="Reject a diagnostically valid but user-facing degraded report.",
    )
    args = parser.parse_args()
    data = validate_analysis(args.analysis, require_complete=args.require_complete)
    validate_html(args.html, data)
    print("VALIDATION OK")


if __name__ == "__main__":
    main()
