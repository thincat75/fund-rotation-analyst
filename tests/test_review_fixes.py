from __future__ import annotations

import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import analyze_weekly as weekly  # noqa: E402
import collect_weekly_data as collector  # noqa: E402
import data_access as access  # noqa: E402
import margin_leverage as margin  # noqa: E402
import tushare_proxy as proxy  # noqa: E402
from cache_store import PREFERRED_PROVIDER, CacheStore  # noqa: E402


WEEK = {
    "period_mode": "explicit",
    "completeness": "complete",
    "baseline_date": "2026-07-03",
    "start_date": "2026-07-06",
    "end_date": "2026-07-10",
}


class AdjustedNavTests(unittest.TestCase):
    def test_missing_growth_uses_unit_and_accumulated_nav_pair(self) -> None:
        records = access.derive_adjusted_fund_nav([
            {"净值日期": "2026-07-03", "单位净值": 2.0, "累计净值": 2.5},
            *[{"净值日期": f"2026-07-{day:02}", "单位净值": 2.0, "累计净值": 2.5} for day in range(6, 10)],
            # 0.40 distribution per share and +1% performance: unit 2.0 -> 1.62.
            {"净值日期": "2026-07-10", "单位净值": 1.62, "累计净值": 2.52},
        ])
        self.assertAlmostEqual(weekly.series_metrics(records, WEEK)["week_return"], 1.0, places=6)
        self.assertNotIn("nav_quality_flag", records[-1])

    def test_bare_unit_ratio_is_flagged(self) -> None:
        records = access.derive_adjusted_fund_nav([
            {"净值日期": "2026-07-03", "单位净值": 2.0},
            {"净值日期": "2026-07-06", "单位净值": 2.1},
        ])
        self.assertEqual(records[-1]["nav_quality_flag"], "日增长率缺失，按单位净值比值计算")
        self.assertEqual(records[-1]["series_start_date"], "2026-07-03")

    def test_young_fund_cache_is_usable_from_first_nav(self) -> None:
        records = [
            {"净值日期": "2026-05-06", "分析净值": 1.0, "series_start_date": "2026-05-06", "nav_basis": "日增长率复权单位净值"},
            {"净值日期": "2026-07-10", "分析净值": 1.1, "series_start_date": "2026-05-06", "nav_basis": "日增长率复权单位净值"},
        ]
        records = [{**row, "nav_model_version": access.NAV_MODEL_VERSION} for row in records]
        self.assertTrue(collector.cached_fund_nav_usable(records, WEEK))
        truncated = [{**row, "series_start_date": "2025-01-02"} for row in records]
        self.assertFalse(collector.cached_fund_nav_usable(truncated, WEEK))

    def test_tushare_nav_never_mixes_adjusted_and_accumulated_rows(self) -> None:
        rows = [
            {"nav_date": "20260703", "adj_nav": 5.0, "accum_nav": 2.5, "unit_nav": 2.0},
            {"nav_date": "20260710", "adj_nav": None, "accum_nav": 2.52, "unit_nav": 1.62},
        ]
        normalized = proxy.normalize_fund_nav(rows)
        self.assertEqual({row["nav_basis"] for row in normalized}, {"unit_accum_reinvested"})
        self.assertAlmostEqual(normalized[-1]["分析净值"] / normalized[0]["分析净值"], 1.01, places=6)


class CacheProviderTests(unittest.TestCase):
    def test_official_rows_win_over_later_public_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory, CacheStore(directory) as store:
            store.upsert_series(PREFERRED_PROVIDER, "margin_summary", "SZSE", [{"trade_date": "2024-08-08", "financing_balance": 6.6e11}])
            store.upsert_series("AkShare交易所汇总", "margin_summary", "SZSE", [{"trade_date": "2024-08-08", "financing_balance": 0.0}])
            rows = store.get_series("margin_summary", "SZSE")
        self.assertEqual(rows[0]["financing_balance"], 6.6e11)


class ProviderWriteTests(unittest.TestCase):
    def test_merged_rows_keep_their_own_provider(self) -> None:
        rows = [
            {"trade_date": "2026-09-10", "provider": "交易所公开汇总", "value": 1.0},
            {"trade_date": "2026-09-11", "provider": PREFERRED_PROVIDER, "value": 2.0},
        ]
        with tempfile.TemporaryDirectory() as directory, CacheStore(directory) as store:
            store.upsert_rows_by_provider("market_daily_info", "SSE", rows)
            official = store.get_series("market_daily_info", "SSE", provider=PREFERRED_PROVIDER)
            public = store.get_series("market_daily_info", "SSE", provider="交易所公开汇总")
        self.assertEqual([row["trade_date"] for row in official], ["2026-09-11"])
        self.assertEqual([row["trade_date"] for row in public], ["2026-09-10"])


class MarginQualityTests(unittest.TestCase):
    def rows(self, values: list[float], start: dt.date = dt.date(2024, 8, 5)) -> list[dict]:
        days = [start + dt.timedelta(days=offset) for offset in range(len(values) * 2) if (start + dt.timedelta(days=offset)).weekday() < 5]
        return [{"trade_date": day.isoformat(), "financing_balance": value} for day, value in zip(days, values)]

    def test_zero_placeholder_day_is_excluded_from_combined_total(self) -> None:
        combined = margin.combine_exchanges({
            "SSE": self.rows([7.4e11, 7.4e11, 7.4e11]),
            "SZSE": self.rows([6.6e11, 0.0, 6.6e11]),
        }, ("financing_balance",))
        self.assertEqual(len(combined), 2)
        self.assertTrue(all(row["financing_balance"] > 1.3e12 for row in combined))

    def test_spike_filter_uses_only_prior_values_and_checks_the_last_day(self) -> None:
        spike = margin._plausible_rows(self.rows([100.0, 101.0, 102.0, 180.0, 103.0]), ("financing_balance",))
        last_day = margin._plausible_rows(self.rows([100.0, 101.0, 102.0, 50.0]), ("financing_balance",))
        shift = margin._plausible_rows(self.rows([100.0, 101.0, 102.0, 180.0, 181.0, 182.0, 183.0]), ("financing_balance",))
        self.assertEqual(len(spike), 4)
        self.assertEqual(len(last_day), 3)
        self.assertEqual(len(shift), 5)

    def test_row_missing_level_field_is_kept_for_other_fields(self) -> None:
        rows = [
            {"trade_date": "2026-09-01", "float_market_cap": 10.0, "market_turnover": 5.0},
            {"trade_date": "2026-09-02", "float_market_cap": None, "market_turnover": 6.0},
        ]
        self.assertEqual(len(margin._plausible_rows(rows, ("float_market_cap", "market_turnover"))), 2)

    def test_tushare_nav_missing_accumulated_value_does_not_go_stale(self) -> None:
        rows = [
            {"nav_date": "20260703", "adj_nav": None, "accum_nav": 2.5, "unit_nav": 2.0},
            {"nav_date": "20260710", "adj_nav": None, "accum_nav": None, "unit_nav": 2.02},
        ]
        normalized = proxy.normalize_fund_nav(rows)
        self.assertEqual(normalized[-1]["净值日期"], "2026-07-10")
        self.assertEqual(normalized[-1]["nav_quality_flag"], "累计净值缺失，按单位净值比值计算")

    def test_change_window_stretched_by_missing_sessions_is_rejected(self) -> None:
        dense = self.rows([100.0] * 20 + [110.0])
        gapped = [dense[0]] + [{**row, "trade_date": (dt.date.fromisoformat(row["trade_date"]) + dt.timedelta(days=60)).isoformat()} for row in dense[1:]]
        self.assertAlmostEqual(margin._percent_change(dense, "financing_balance", 20), 10.0)
        self.assertIsNone(margin._percent_change(gapped, "financing_balance", 20))
        self.assertEqual(margin._rolling_changes(gapped, "financing_balance", 20), [])

    def test_cached_market_history_with_middle_gap_is_incomplete(self) -> None:
        rows = [{"trade_date": "2026-08-28"}, {"trade_date": "2026-09-11"}]
        required = ["2026-08-28", "2026-08-31", "2026-09-11"]
        self.assertFalse(collector._cached_series_complete(rows, "2026-09-11", required_dates=required))
        self.assertTrue(collector._cached_series_complete(rows, "2026-09-11"))
        self.assertEqual(collector._missing_dates(rows, required), ["2026-08-31"])


class PeriodAndTaxonomyTests(unittest.TestCase):
    def test_month_windows_use_calendar_months(self) -> None:
        self.assertEqual(weekly.months_before(dt.date(2026, 9, 11), 1), dt.date(2026, 8, 11))
        self.assertEqual(weekly.months_before(dt.date(2026, 3, 31), 1), dt.date(2026, 2, 28))
        self.assertEqual(weekly.months_before(dt.date(2026, 1, 15), 3), dt.date(2025, 10, 15))

    def test_weekend_only_gap_identifies_next_session(self) -> None:
        self.assertTrue(weekly.is_next_weekday_session(dt.date(2026, 9, 11), dt.date(2026, 9, 14)))
        self.assertFalse(weekly.is_next_weekday_session(dt.date(2026, 9, 11), dt.date(2026, 9, 15)))
        self.assertFalse(weekly.is_next_weekday_session(dt.date(2026, 9, 11), dt.date(2026, 9, 11)))

    def test_common_ths_sectors_are_classified_with_specific_rules_first(self) -> None:
        expected = {
            # Exposure keys below were checked against THS constituents on 2026-09-15:
            # 元件 holds the PCB leaders; 电子化学品 is only ~15/43 semiconductor
            # materials (~12% of semiconductor ETF holdings); 光学光电子 is mostly
            # display/LED; 人工智能 and MLCC contain no PCB/server leaders.
            "元件": ("科技", ["PCB/AI服务器"]),
            "电子化学品": ("科技", []),
            "光学光电子": ("科技", []),
            "共封装光学(CPO)": ("科技", ["AI光模块/通信"]),
            "铜缆高速连接": ("科技", ["PCB/AI服务器"]),
            "MLCC概念": ("科技", []),
            "存储芯片": ("科技", ["半导体设备/材料"]),
            "人工智能": ("科技", []),
            "风电设备": ("制造", ["新能源"]),
            "军工电子": ("制造", []),
            "汽车电子": ("消费", []),
            "农化制品": ("资源能源", []),
            "同花顺中特估100": ("主题", []),
        }
        for name, (theme_l1, exposure) in expected.items():
            result = weekly.classify_sector(name)
            self.assertEqual(result["classification_status"], "已分类", name)
            self.assertEqual((result["theme_l1"], result["exposure_keys"]), (theme_l1, exposure), name)
        self.assertEqual(weekly.classify_sector("建筑材料")["theme_l2"], "钢铁建材")

    def test_three_week_coverage_aliases_match_verified_constituents(self) -> None:
        import three_week_analysis as three_week

        rules = three_week.THEME_SECTOR_RULES
        self.assertNotIn("光学光电子", rules["AI光模块/通信"]["direct"] + rules["AI光模块/通信"]["indirect"])
        self.assertIn("铜缆高速连接", rules["PCB/AI服务器"]["direct"])
        self.assertNotIn("电子化学品", rules["半导体设备/材料"]["direct"])
        self.assertIn("电子化学品", rules["半导体设备/材料"]["indirect"])


class VerifiedMappingTests(unittest.TestCase):
    def test_sector_exposure_keys_follow_constituent_checks(self) -> None:
        expected = {
            "通信设备": ["AI光模块/通信"],
            "通信服务": [],
            "电信运营商": ["红利价值"],
            "软件开发": [],
            "IT服务": [],
            "被动元件": [],
            "参股银行": [],
            "光刻胶": [],
            "中药": [],
            "电网设备": [],
            "煤炭开采加工": ["红利价值"],
            "煤炭概念": [],
        }
        for name, exposure in expected.items():
            self.assertEqual(weekly.classify_sector(name)["exposure_keys"], exposure, name)
        self.assertEqual(weekly.classify_sector("生物质能发电")["theme_l1"], "公用事业")
        self.assertEqual(weekly.classify_sector("腾讯概念")["classification_status"], "已分类")

    def test_coverage_aliases_use_exact_names_where_substrings_mislead(self) -> None:
        import three_week_analysis as three_week

        portfolio = {"funds": [
            {"name": "红利基金", "current_weight": 0.5, "themes": ["红利价值"]},
            {"name": "PCB基金", "current_weight": 0.5, "themes": ["PCB/AI服务器"]},
        ]}
        self.assertEqual(three_week.sector_portfolio_coverage("电力", portfolio)["portfolio_coverage"], "直接主题覆盖")
        self.assertEqual(three_week.sector_portfolio_coverage("绿色电力", portfolio)["portfolio_coverage"], "未发现已披露覆盖")
        self.assertEqual(three_week.sector_portfolio_coverage("被动元件", portfolio)["portfolio_coverage"], "未发现已披露覆盖")
        self.assertEqual(three_week.sector_portfolio_coverage("通信服务", {"funds": [{"name": "光模块基金", "current_weight": 1, "themes": ["AI光模块/通信"]}]})["portfolio_coverage"], "未发现已披露覆盖")

    def test_fund_themes_come_from_holding_names_not_industry_labels(self) -> None:
        self.assertIn("AI光模块/通信", weekly.infer_themes("新易盛 中际旭创 源杰科技"))
        self.assertIn("PCB/AI服务器", weekly.infer_themes("生益科技 东山精密"))
        self.assertEqual(weekly.infer_themes("50通信服务 计算机、通信和其他电子设备制造业"), ["未识别"])
        self.assertNotIn("港股/海外科技", weekly.infer_themes("广发道琼斯石油指数(QDII-LOF)"))

    def test_flow_reason_flags_opposite_report_end_day(self) -> None:
        reason = weekly.flow_status_reason("持续流入", {"今日": -6.0, "5日": 4.0, "10日": 56.0})
        self.assertIn("报告期末日方向相反", reason)
        self.assertNotIn("方向相反", weekly.flow_status_reason("持续流入", {"今日": 1.0, "5日": 4.0, "10日": 56.0}))

    def test_profile_with_industry_allocation_only_is_partial(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            payload = collector.save_profile_cache(Path(directory), "000001", {
                "basic_info": [{"item": "基金规模", "value": "10亿"}],
                "industry_allocation": [{"行业类别": "制造业", "占净值比例": 89.8}],
            })
        self.assertEqual(payload["profile_status"], "partial_profile")


class RoundThreeTests(unittest.TestCase):
    def test_single_small_holding_does_not_define_a_theme(self) -> None:
        evidence = {"latest_holdings": [
            {"股票名称": "中际旭创", "占净值比例": 7.13}, {"股票名称": "新易盛", "占净值比例": 6.64},
            {"股票名称": "源杰科技", "占净值比例": 3.84}, {"股票名称": "生益科技", "占净值比例": 2.68},
        ]}
        themes, weights = weekly.holding_based_themes(evidence, "某成长混合A")
        self.assertEqual(themes, ["AI光模块/通信"])
        self.assertAlmostEqual(weights["PCB/AI服务器"], 2.68)

    def test_coverage_is_looked_through_by_disclosed_theme_weight(self) -> None:
        import three_week_analysis as three_week

        portfolio = {"funds": [{"name": "PCB基金", "current_weight": 0.5, "themes": ["PCB/AI服务器"], "theme_holding_weights": {"PCB/AI服务器": 20.0}}]}
        self.assertAlmostEqual(three_week.sector_portfolio_coverage("元件", portfolio)["coverage_weight"], 0.1)
        _, _, weights = weekly.portfolio_theme_context(portfolio)
        self.assertAlmostEqual(weights["PCB/AI服务器"], 0.1)

    def test_holiday_shortened_week_is_complete_on_its_last_session(self) -> None:
        dates = [day for day in collector.weekday_calendar(dt.date(2026, 9, 21), dt.date(2026, 10, 16)) if not dt.date(2026, 10, 1) <= day <= dt.date(2026, 10, 7)]
        self.assertEqual(collector.resolve_week(dates, dt.date(2026, 10, 9), dt.date(2026, 9, 30))["completeness"], "complete")
        self.assertEqual(collector.resolve_week(dates, dt.date(2026, 10, 9), dt.date(2026, 9, 29))["completeness"], "partial")

    def test_ranking_snapshot_returns_do_not_move_reference_percentiles(self) -> None:
        pool = [{"code": "A", "week_return": 1.0}, {"code": "B", "week_return": 2.0}, {"code": "C", "week_return": 3.0}]
        scores = weekly.percentile_scores(pool + [{"code": "R", "week_return": 10.0}], "week_return", reference_rows=pool)
        self.assertEqual((scores["A"], scores["B"], scores["C"], scores["R"]), (0.0, 50.0, 100.0, 100.0))

    def test_unadjusted_split_inside_period_window_clears_period_returns(self) -> None:
        rows = [
            {"日期": "2026-04-01", "收盘": 2.0}, {"日期": "2026-06-15", "收盘": 2.0}, {"日期": "2026-06-20", "收盘": 1.0},
            {"日期": "2026-07-03", "收盘": 1.0}, {"日期": "2026-07-10", "收盘": 1.01},
        ]
        metrics = weekly.series_metrics(rows, WEEK, ["收盘"])
        cleaned = weekly.without_split_period_returns(metrics, rows, WEEK, ["收盘"])
        self.assertIsNone(cleaned["one_month"])
        self.assertIsNone(cleaned["three_month"])
        self.assertAlmostEqual(cleaned["week_return"], 1.0)

    def test_nav_cache_rows_from_different_anchors_are_not_reused(self) -> None:
        base = {"分析净值": 1.0, "nav_basis": "unit_accum_reinvested"}
        rows = [
            {**base, "净值日期": "2025-07-10", "series_start_date": "2025-06-15"},
            {**base, "净值日期": "2026-07-10", "series_start_date": "2025-06-22"},
        ]
        self.assertFalse(collector.cached_fund_nav_usable(rows, WEEK))

    def test_exact_names_avoid_keyword_collisions(self) -> None:
        self.assertEqual(weekly.classify_sector("电子竞技")["theme_l1"], "消费")
        self.assertEqual(weekly.classify_sector("玻璃基板")["theme_l1"], "科技")
        self.assertEqual(weekly.classify_sector("液冷服务器")["exposure_keys"], [])
        self.assertEqual(weekly.infer_themes("纳斯达克生物科技ETF联接"), ["未识别"])


class MarginAsOfTests(unittest.TestCase):
    def test_combined_margin_ending_before_cutoff_is_partial(self) -> None:
        sys.path.insert(0, str(ROOT / "tests"))
        from test_margin_leverage import sample_history

        raw, styles = sample_history(500)
        cutoff = raw["exchanges"]["SSE"][-1]["trade_date"]
        raw["exchanges"]["SZSE"] = raw["exchanges"]["SZSE"][:-1]
        result = margin.analyze_margin_leverage(raw, styles, cutoff=cutoff)
        self.assertEqual(result["status"], "partial")
        self.assertTrue(any("早于报告截止日" in item for item in result["data_quality"]))

    def test_new_concepts_are_classified_without_fund_exposure(self) -> None:
        for name in ["OLED", "一带一路（概念）", "专精特新", "中芯国际概念", "国家大基金持股", "小米概念", "科创次新股"]:
            result = weekly.classify_sector(name)
            self.assertEqual(result["classification_status"], "已分类", name)
            self.assertEqual(result["exposure_keys"], [], name)


class EtfEvidenceTests(unittest.TestCase):
    def test_post_cutoff_snapshot_supplies_previous_day_nav_and_hfq_turnover(self) -> None:
        data = {
            "week": WEEK,
            "candidate_etfs": {
                "codes": ["560780"], "spot": [],
                "nav_spot_ths": [{"基金代码": "560780", "最新-交易日": "2026-07-13", "最新-单位净值": 1.2, "前一日-单位净值": 1.0}],
                "history": {"560780": {
                    "hfq": [{"日期": "2026-07-03", "收盘": 2.0, "成交额": 1e9}, {"日期": "2026-07-10", "收盘": 2.1, "成交额": 2e9}],
                    "qfq": [], "none": [],
                }},
                "history_sina": {"560780": [{"date": "2026-07-03", "close": 1.0}, {"date": "2026-07-10", "close": 1.05}]},
                "nav": {"560780": []}, "access": {"560780": {}},
            },
        }
        result = weekly.analyze_etfs(data)[0]
        self.assertAlmostEqual(result["premium_rate"], 5.0)
        self.assertEqual(result["turnover"], 2e9)
        self.assertNotIn("成交额缺失", result.get("quality_flags") or [])

    def test_thin_report_end_turnover_blocks_recommendation(self) -> None:
        def result_for(turnover: float) -> dict:
            data = {
                "week": WEEK,
                "candidate_etfs": {
                    "codes": ["560780"], "spot": [],
                    "history": {"560780": {
                        "hfq": [{"日期": "2026-07-03", "收盘": 2.0}, {"日期": "2026-07-10", "收盘": 2.02, "成交额": turnover}],
                        "none": [{"日期": "2026-07-03", "收盘": 1.0}, {"日期": "2026-07-10", "收盘": 1.01, "成交额": turnover}],
                    }},
                    "nav": {"560780": [{"净值日期": "2026-07-10", "单位净值": 1.0}]},
                    "history_sina": {}, "access": {"560780": {}},
                },
            }
            return weekly.analyze_etfs(data)[0]

        self.assertFalse(result_for(5e6)["recommendation_eligible"])
        self.assertTrue(result_for(2e9)["recommendation_eligible"])

    def test_etf_return_prefers_growth_adjusted_nav_over_accumulated_nav(self) -> None:
        nav = access.derive_adjusted_fund_nav([
            {"净值日期": "2026-07-03", "单位净值": 2.0, "累计净值": 2.5, "日增长率": 0.0},
            *[{"净值日期": f"2026-07-{day:02}", "单位净值": 2.0, "累计净值": 2.5, "日增长率": 0.0} for day in range(6, 10)],
            {"净值日期": "2026-07-10", "单位净值": 1.62, "累计净值": 2.52, "日增长率": 1.0},
        ])
        evidence = weekly.etf_return_evidence({"history": {}, "nav": {"X": nav}}, "X", WEEK)
        self.assertAlmostEqual(evidence["week_return"], 1.0, places=6)
        self.assertEqual(evidence["return_status"], "ok")

    def test_split_months_before_the_week_is_not_a_current_corporate_action(self) -> None:
        nav = access.derive_adjusted_fund_nav([
            {"净值日期": "2026-05-06", "单位净值": 3.6, "日增长率": 0.0},
            {"净值日期": "2026-05-07", "单位净值": 1.2, "日增长率": 0.5},
            {"净值日期": "2026-07-03", "单位净值": 1.0, "日增长率": 0.0},
            *[{"净值日期": f"2026-07-{day:02}", "单位净值": 1.0, "日增长率": 0.0} for day in range(6, 10)],
            {"净值日期": "2026-07-10", "单位净值": 1.01, "日增长率": 1.0},
        ])
        evidence = weekly.etf_return_evidence({"history": {}, "nav": {"X": nav}}, "X", WEEK)
        self.assertEqual(evidence["corporate_actions"], [])
        self.assertTrue(evidence["supports_recommendation"])


if __name__ == "__main__":
    unittest.main()
