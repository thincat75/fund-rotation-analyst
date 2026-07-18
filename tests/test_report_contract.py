from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import render_weekly_visual_report as visual  # noqa: E402
import validate_report as validator  # noqa: E402
from report_contract import MANDATORY_SECTION_ORDER, REPORT_FORMAT_VERSION  # noqa: E402


def contract_payload() -> dict:
    return {
        "schema_version": 2,
        "data_revision": "contract-fixture",
        "report_format_version": REPORT_FORMAT_VERSION,
        "report_contract": {
            "format_version": REPORT_FORMAT_VERSION,
            "mandatory_sections": MANDATORY_SECTION_ORDER,
            "render_targets": ["markdown", "html"],
        },
        "as_of": "2026-07-17T12:00:00",
        "holdings_hash": "a" * 64,
        "llm_evidence_hash": "b" * 64,
        "week": {"start_date": "2026-07-13", "end_date": "2026-07-17", "period_mode": "current", "completeness": "partial"},
        "portfolio": {
            "funds": [{"code": "000001", "name": "测试基金", "current_weight": 1, "themes": [], "decision_action": "观察"}],
            "weight_basis_display": "等权分析假设",
            "weight_assumption": "测试假设",
        },
        "market": {"style_indexes": [], "sector_top10": {}, "weekly_top_funds": []},
        "candidate_etfs": [],
        "comparison": {
            "replacement_status": "insufficient_evidence",
            "replacement_status_display": "证据不足，暂不生成替换建议",
            "replacement_blockers": ["候选未通过评分门槛"],
            "weekly_conclusion": {},
        },
        "three_week_analysis": {
            "periods": [
                {"period_id": "W-2", "label": "上上周", "start_date": "2026-06-29", "end_date": "2026-07-03", "completeness": "complete"},
                {"period_id": "W-1", "label": "上周", "start_date": "2026-07-06", "end_date": "2026-07-10", "completeness": "complete"},
                {"period_id": "W0", "label": "本周", "start_date": "2026-07-13", "end_date": "2026-07-17", "completeness": "partial"},
            ],
            "portfolio": {"funds": [{"name": "测试基金", "trajectory_state": "数据不足", "periods": {}}]},
            "styles": [], "industries": [], "concepts": [], "evidence_index": {},
        },
        "warnings": [],
        "analysis_notes": [],
        "data_quality": [],
        "source_audit": [],
        "cache": {"database": "cache.sqlite3", "stats": {}},
        "disclaimer": "仅用于测试。",
    }


class VisualReportContractTests(unittest.TestCase):
    def test_renderer_emits_full_versioned_contract(self) -> None:
        data = contract_payload()
        output = visual.render(data)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.html"
            path.write_text(output, encoding="utf-8")
            validator.validate_html(path, data)
        self.assertIn(f'content="{REPORT_FORMAT_VERSION}"', output)
        self.assertIn("@media print", output)
        self.assertIn('aria-label="报告导航"', output)

    def test_mandatory_sections_are_unique_and_ordered(self) -> None:
        output = visual.render(contract_payload())
        positions = []
        for section in MANDATORY_SECTION_ORDER:
            marker = f'data-section="{section}"'
            self.assertEqual(output.count(marker), 1)
            positions.append(output.index(marker))
        self.assertEqual(positions, sorted(positions))

    def test_validator_rejects_format_regression(self) -> None:
        data = contract_payload()
        output = visual.render(data).replace("@media print", "@media screen", 1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.html"
            path.write_text(output, encoding="utf-8")
            with self.assertRaises(SystemExit):
                validator.validate_html(path, data)

    def test_deterministic_synthesis_status_is_translated(self) -> None:
        data = contract_payload()
        data["llm_synthesis"] = {
            "status": "deterministic_fallback",
            "confidence": "中",
            "market_regime": "无明确主线",
        }
        html = visual.render(data)
        self.assertIn("程序规则综合", html)
        self.assertNotIn("deterministic_fallback", html)

    def test_degraded_sections_remain_visible(self) -> None:
        data = contract_payload()
        data["portfolio"]["funds"] = []
        data["three_week_analysis"]["portfolio"]["funds"] = []
        output = visual.render(data)
        self.assertIn("暂无持仓数据", output)
        self.assertIn("暂无可用数据", output)
        self.assertIn("候选未通过评分门槛", output)
        self.assertNotIn("insufficient_evidence", output)

    def test_margin_visual_contract_keeps_all_three_60_day_tracks(self) -> None:
        data = contract_payload()
        data["data_revision"] = "2.8"
        data["market"]["margin_leverage"] = {
            "model_version": "margin-leverage-v1",
            "scope": "SSE+SZSE",
            "action_policy": "display_only",
            "status": "partial",
            "as_of": "2026-07-17",
            "current": {},
            "normalization": {},
            "history_position": {},
            "heat": {"score": None, "coverage": 0, "label": "数据不足"},
            "deleveraging_pressure": {"score": None, "coverage": 0, "label": "数据不足"},
            "regime": {"label": "数据不足"},
            "series": [],
            "broad_index_series": [],
            "data_quality": ["fixture缺少历史"],
        }
        output = visual.render(data)
        for label in ("近60日两融余额", "近60日融资杠杆密度", "近60日宽基代表（中证全指）"):
            self.assertIn(label, output)

    def test_complete_delivery_rejects_degraded_core_sections(self) -> None:
        data = contract_payload()
        data["delivery_readiness"] = {
            "status": "degraded",
            "row_counts": {},
            "core_requirements": {
                "行业近5个交易日收益": False, "概念近5个交易日收益": False,
                "行业近5日资金流入": False, "行业近5日资金流出": False,
                "概念近5日资金流入": False, "概念近5日资金流出": False,
                "行业最新行情": False, "概念最近有效收盘": False,
            },
            "unresolved_required_datasets": [],
            "consistency_errors": [],
            "blockers": ["行业近5个交易日收益"],
        }
        with self.assertRaises(SystemExit):
            validator.validate_delivery_readiness(data, require_complete=True)

    def test_validator_rejects_three_week_single_week_sector_regression(self) -> None:
        data = contract_payload()
        data["delivery_readiness"] = {
            "status": "degraded",
            "row_counts": {"industry_return": 0},
            "core_requirements": {
                "行业近5个交易日收益": False, "概念近5个交易日收益": False,
                "行业近5日资金流入": False, "行业近5日资金流出": False,
                "概念近5日资金流入": False, "概念近5日资金流出": False,
                "行业最新行情": False, "概念最近有效收盘": False,
            },
            "unresolved_required_datasets": [],
            "consistency_errors": ["三周行业序列在W0有90条有效记录，但单周行业Top10为空"],
            "blockers": ["行业近5个交易日收益"],
        }
        with self.assertRaises(SystemExit):
            validator.validate_delivery_readiness(data)


if __name__ == "__main__":
    unittest.main()
