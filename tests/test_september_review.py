from __future__ import annotations

import copy
import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import analyze_weekly as weekly
import data_access as access
import collect_weekly_data as collector
import three_week_analysis as three
import margin_leverage as margin
import render_weekly_report as markdown
import render_weekly_visual_report as visual
import validate_report as validator
from test_report_contract import contract_payload
from test_margin_leverage import sample_history
from test_review_fixes import WEEK


class SeptemberReviewTests(unittest.TestCase):
    def test_gapped_growth_is_not_published_as_a_week_return(self):
        rows = access.derive_adjusted_fund_nav([
            {"净值日期": "2026-07-03", "单位净值": 1.0, "日增长率": 0},
            {"净值日期": "2026-07-10", "单位净值": 1.2, "日增长率": 1},
        ])
        self.assertIsNone(weekly.series_metrics(rows, WEEK)["week_return"])
        portfolio = {"funds": [{"code": "X", "current_weight": 1}]}
        result = three.analyze_portfolio({"funds": {"X": {"nav": rows}}}, portfolio, [{**WEEK, "period_id": "W0"}])
        self.assertIsNone(result["weekly_returns"]["W0"])
        self.assertFalse(weekly.etf_return_evidence({"nav": {"X": rows}}, "X", WEEK)["supports_recommendation"])

    def test_derived_nav_duplicate_day_is_not_compounded_twice(self):
        rows = [{"净值日期": "2026-07-03", "单位净值": 1.0, "日增长率": 0},
                {"净值日期": "2026-07-06", "单位净值": 1.1, "日增长率": 10}]
        self.assertEqual(access.derive_adjusted_fund_nav(rows), access.derive_adjusted_fund_nav(rows + rows))

    def test_exchange_calendar_allows_real_holidays(self):
        rows = [{"净值日期": "2026-09-30", "单位净值": 1.0, "日增长率": 0},
                {"净值日期": "2026-10-08", "单位净值": 1.01, "日增长率": 1}]
        dates = [dt.date(2026, 9, 30), dt.date(2026, 10, 8)]
        result = access.derive_adjusted_fund_nav(rows, dates)
        self.assertNotIn("nav_quality_flag", result[-1])

    def test_old_derived_cache_is_refreshed(self):
        rows = [{"净值日期": day, "分析净值": 1.0, "nav_basis": "unit_accum_reinvested", "series_start_date": "2025-07-03"}
                for day in ("2025-07-03", "2026-07-10")]
        self.assertFalse(collector.cached_fund_nav_usable(rows, WEEK))

    def test_margin_filter_does_not_rewrite_prior_decisions(self):
        rows = [{"trade_date": f"2026-09-{day:02}", "financing_balance": value}
                for day, value in enumerate([100, 101, 102, 180, 181, 182, 183], 1)]
        complete = margin._plausible_rows(rows, ("financing_balance",))
        for length in range(1, len(rows) + 1):
            prefix = margin._plausible_rows(rows[:length], ("financing_balance",))
            self.assertEqual(prefix, {day: row for day, row in complete.items()
                                      if day <= rows[length - 1]["trade_date"]})

    def test_margin_cutoff_excludes_future_before_cleaning_and_bse_display(self):
        raw, styles = sample_history(510)
        cutoff = raw["exchanges"]["SSE"][-3]["trade_date"]
        for exchange in ("SSE", "SZSE"):
            for row in raw["exchanges"][exchange][-3:]:
                for key in ("financing_balance", "lending_balance", "margin_balance"):
                    row[key] *= 1.8
        historical = copy.deepcopy(raw)
        for group in ("exchanges", "market_daily"):
            for key in historical[group]:
                historical[group][key] = [r for r in historical[group][key] if r["trade_date"] <= cutoff]
        a = margin.analyze_margin_leverage(raw, styles, cutoff=cutoff)
        b = margin.analyze_margin_leverage(historical, styles, cutoff=cutoff)
        self.assertEqual(a, b)

    def test_growth_crosscheck_requires_every_session_and_deduplicates(self):
        rows = [{"净值日期": f"2026-07-{day:02}", "日增长率": 1.0} for day in range(6, 11)]
        self.assertAlmostEqual(weekly._compound_reported_growth(rows, WEEK), (1.01 ** 5 - 1) * 100)
        self.assertEqual(weekly._compound_reported_growth(rows + rows, WEEK), weekly._compound_reported_growth(rows, WEEK))
        self.assertIsNone(weekly._compound_reported_growth(rows[:2] + rows[3:], WEEK))
        self.assertIsNone(weekly._compound_reported_growth(rows + [{**rows[-1], "日增长率": 2.0}], WEEK))
        holiday = {**WEEK, "trading_dates": ["2026-07-06", "2026-07-10"]}
        self.assertAlmostEqual(weekly._compound_reported_growth([rows[0], rows[-1]], holiday), 2.01)

    def test_disclosed_holdings_take_priority_over_product_name(self):
        evidence = {"latest_holdings": [{"股票名称": "中微公司", "占净值比例": 20}]}
        themes, _ = weekly.holding_based_themes(evidence, "海外科技主动混合")
        self.assertEqual(themes, ["半导体设备/材料"])

    def test_ranking_snapshot_cannot_receive_report_week_score(self):
        candidate = {"code": "999999", "candidate_kind": "fund", "name": "排行快照",
                     "return_basis": "基金排行收益字段", "week_return": 20, "one_month": 30,
                     "themes": ["半导体设备/材料"], "return_period_aligned": False}
        weekly.score_rows([candidate], {}, [], {"funds": []})
        self.assertIsNone(candidate["weekly_score"])
        self.assertIn("报告期", candidate["score_unavailable_reason"])

    def test_all_four_flow_lists_survive_rendering_and_validation(self):
        data = contract_payload()
        sectors = data["market"]["sector_top10"]
        for group in ("industry", "concept"):
            for direction in ("inflow", "outflow"):
                key = f"{group}_{direction}"
                sectors[key] = [{"name": f"{key}_{i}", "type": group,
                                 "five_day_flow": (1000 if group == "industry" else 1) * (1 if direction == "inflow" else -1)}
                                for i in range(10)]
        page = visual.render(data)
        md = markdown.render(data)
        for rows in sectors.values():
            for row in rows:
                self.assertIn(row["name"], page)
                self.assertIn(row["name"], md)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.html"
            path.write_text(page, encoding="utf-8")
            validator.validate_html(path, data)
            path.write_text(page.replace('data-flow-list="concept_outflow"', 'data-flow-list="removed"'), encoding="utf-8")
            with self.assertRaises(SystemExit):
                validator.validate_html(path, data)

    def test_missing_margin_chart_is_a_valid_explicit_degradation(self):
        data = contract_payload()
        data["data_revision"] = "2.8"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.html"
            path.write_text(visual.render(data), encoding="utf-8")
            validator.validate_html(path, data)


if __name__ == "__main__":
    unittest.main()
