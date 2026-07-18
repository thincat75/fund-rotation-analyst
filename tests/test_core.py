from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import analyze_portfolio as portfolio  # noqa: E402
import analyze_weekly as weekly  # noqa: E402
import collect_weekly_data as collector  # noqa: E402
import data_access as access  # noqa: E402
import render_weekly_visual_report as visual  # noqa: E402
import validate_report as validator  # noqa: E402


WEEK = {
    "period_mode": "explicit",
    "completeness": "complete",
    "baseline_date": "2026-07-03",
    "start_date": "2026-07-06",
    "end_date": "2026-07-10",
}


class DateAndReturnTests(unittest.TestCase):
    def test_completed_week_on_weekday_uses_previous_calendar_week(self) -> None:
        dates = collector.weekday_calendar(dt.date(2026, 6, 1), dt.date(2026, 7, 31))
        result = collector.resolve_week(dates, dt.date(2026, 7, 8), None)
        self.assertEqual(result["start_date"], "2026-06-29")
        self.assertEqual(result["end_date"], "2026-07-03")

    def test_weekend_uses_just_completed_week(self) -> None:
        dates = collector.weekday_calendar(dt.date(2026, 6, 1), dt.date(2026, 7, 31))
        result = collector.resolve_week(dates, dt.date(2026, 7, 12), None)
        self.assertEqual(result["start_date"], "2026-07-06")
        self.assertEqual(result["end_date"], "2026-07-10")

    def test_future_record_is_rejected(self) -> None:
        result = weekly.series_metrics([{"日期": "2026-07-13", "收盘": 110}], WEEK)
        self.assertEqual(result["data_status"], "insufficient_data")
        self.assertIsNone(result["week_return"])

    def test_metrics_include_month_quarter_and_drawdown(self) -> None:
        records = [
            {"日期": "2026-04-01", "单位净值": 0.8},
            {"日期": "2026-06-10", "单位净值": 0.9},
            {"日期": "2026-07-03", "单位净值": 1.0},
            {"日期": "2026-07-10", "单位净值": 1.1},
        ]
        result = weekly.series_metrics(records, WEEK)
        self.assertAlmostEqual(result["week_return"], 10.0)
        self.assertIsNotNone(result["one_month"])
        self.assertIsNotNone(result["three_month"])
        self.assertIsNotNone(result["max_drawdown_1y"])

    def test_duplicate_dates_use_last_observation(self) -> None:
        records = [
            {"日期": "2026-07-03", "累计净值": 1.0},
            {"日期": "2026-07-10", "累计净值": 1.05},
            {"日期": "2026-07-10", "累计净值": 1.10},
        ]
        self.assertAlmostEqual(weekly.series_metrics(records, WEEK)["week_return"], 10.0)

    def test_stale_weekly_baseline_is_rejected(self) -> None:
        records = [{"日期": "2026-06-01", "累计净值": 1.0}, {"日期": "2026-07-10", "累计净值": 1.1}]
        result = weekly.series_metrics(records, WEEK)
        self.assertEqual(result["data_status"], "insufficient_data")
        self.assertIn("baseline is stale", result["warning"])

    def test_index_source_must_cover_target_week(self) -> None:
        stale = [{"日期": "2019-06-28", "收盘": 4000}]
        current = [{"日期": "2026-07-03", "收盘": 5000}, {"日期": "2026-07-10", "收盘": 5050}]
        self.assertFalse(collector.index_records_cover_week(stale, WEEK)[0])
        self.assertTrue(collector.index_records_cover_week(current, WEEK)[0])


class PortfolioCoverageTests(unittest.TestCase):
    def test_low_coverage_suppresses_formal_return(self) -> None:
        holdings = [{"code": "000001", "name": "A"}, {"code": "000002", "name": "B"}]
        data = {
            "week": WEEK,
            "funds": {"000001": {"nav": [{"日期": "2026-07-03", "单位净值": 1}, {"日期": "2026-07-10", "单位净值": 1.1}]}},
        }
        result = weekly.analyze_funds(data, holdings)
        self.assertAlmostEqual(result["nav_coverage_weight"], 0.5)
        self.assertIsNone(result["weekly_return"])
        self.assertAlmostEqual(result["partial_weekly_return"], 5.0)

    def test_assumed_equal_weights_are_explained(self) -> None:
        holdings = [{"code": str(index), "name": str(index)} for index in range(7)]
        basis, weights, display, assumption = weekly.derive_weights(
            holdings, {"weight_mode": "assumed_equal", "weight_note": "七只基金按等权假设分析"}
        )
        self.assertEqual(basis, "assumed_equal")
        self.assertAlmostEqual(weights["0"], 1 / 7)
        self.assertIn("等权", display)
        self.assertIn("假设", assumption)


class SectorTests(unittest.TestCase):
    def test_unlabeled_large_flow_is_not_scaled_twice(self) -> None:
        row = {"名称": "半导体设备", "今日主力净流入-净额": 1.2e9, "今日涨跌幅": 2.1}
        self.assertEqual(weekly.flow_amount_yuan(row, 1.2e9, "今日"), 1.2e9)

    def test_legacy_compact_billion_flow_is_migrated(self) -> None:
        row = {"名称": "半导体科技", "今日净流入": 43, "今日涨跌幅": 2.1}
        self.assertEqual(weekly.flow_amount_yuan(row, 43, "今日"), 4.3e9)

    def test_report_end_flow_does_not_replace_current_day_snapshot(self) -> None:
        sectors = {"fund_flow": {
            "今日": {"行业资金流": [{"名称": "半导体", "今日主力净流入-净额": -1, "资金单位": "元"}]},
            "报告期末日": {"行业资金流": [{"名称": "半导体", "今日主力净流入-净额": 2, "资金单位": "元"}]},
            "5日": {"行业资金流": [{"名称": "半导体", "5日主力净流入-净额": 3, "资金单位": "元"}]},
        }}
        completed = weekly.build_flow_lookup(sectors, "行业资金流", completed_week=True)
        current = weekly.build_flow_lookup(sectors, "行业资金流", completed_week=False)
        self.assertEqual(completed["半导体"]["今日"], 2)
        self.assertEqual(current["半导体"]["今日"], -1)

    def test_flow_status_requires_two_periods(self) -> None:
        self.assertEqual(weekly.flow_status(1, None, None), "数据不足")
        self.assertEqual(weekly.flow_status(0, 2, 0), "数据不足")
        self.assertEqual(weekly.flow_status(1, 2, -1), "持续流入")
        self.assertEqual(weekly.flow_status(-1, -2, 3), "持续流出")

    def test_complete_sector_universe_turns_no_match_into_low_score_evidence(self) -> None:
        sectors = {"industry_return": [{
            "name": "银行", "theme_l1": "金融", "theme_l2": "银行",
            "exposure_keys": ["红利价值"], "week_return": 2, "flow_status": "持续流入",
        }]}
        score, evidence = weekly.sector_confirmation(["AI光模块/通信"], sectors)
        self.assertEqual(score, 25)
        self.assertIn("暂无资金确认", evidence[0])

    def test_inflow_and_outflow_are_sign_filtered(self) -> None:
        portfolio_model = {
            "funds": [{"name": "A", "themes": ["半导体设备/材料"], "current_weight": 1.0}]
        }
        data = {
            "week": WEEK,
            "market": {
                "sectors": {
                    "industry_today": [],
                    "concept_today": [],
                    "fund_flow": {
                        "今日": {"行业资金流": [{"名称": "半导体", "今日主力净流入-净额": 10}, {"名称": "通信", "今日主力净流入-净额": -5}], "概念资金流": []},
                        "5日": {"行业资金流": [{"名称": "半导体", "5日主力净流入-净额": 20, "5日涨跌幅": 3}, {"名称": "通信", "5日主力净流入-净额": -8, "5日涨跌幅": -2}], "概念资金流": []},
                        "10日": {"行业资金流": [], "概念资金流": []},
                    },
                }
            },
        }
        result = weekly.analyze_sectors(data, portfolio_model)
        self.assertTrue(all(row["five_day_flow"] > 0 for row in result["industry_inflow"]))
        self.assertTrue(all(row["five_day_flow"] < 0 for row in result["industry_outflow"]))
        self.assertTrue(all(row["week_return"] is not None for row in result["industry_inflow"]))
        self.assertTrue(all("今日" not in row["return_basis"] for row in result["industry_return"]))

    def test_completed_week_flow_does_not_use_later_today_snapshot(self) -> None:
        data = {
            "week": {**WEEK, "collection_trade_date": "2026-07-16"},
            "market": {"sectors": {
                "industry_today": [{"板块": "半导体", "涨跌幅": -5.35}], "concept_today": [],
                "fund_flow": {
                    "今日": {"行业资金流": [{"名称": "半导体", "今日涨跌幅": -5.35, "今日主力净流入-净额": 42.88}], "概念资金流": []},
                    "5日": {"行业资金流": [{"名称": "半导体设备", "5日主力净流入-净额": 10_000_000_000, "5日涨跌幅": 3.1, "资金单位": "元"}], "概念资金流": []},
                    "10日": {"行业资金流": [], "概念资金流": []},
                },
                "flow_meta": {
                    "今日": {"行业资金流": {"source_date": "2026-07-16"}},
                    "5日": {"行业资金流": {"source_date": "2026-07-10"}},
                },
            }},
        }
        result = weekly.analyze_sectors(data, {"funds": []})
        flow = result["industry_inflow"][0]
        self.assertEqual(flow["name"], "半导体设备")
        self.assertEqual(flow["week_return"], 3.1)
        self.assertEqual(flow["five_day_flow"], 10_000_000_000)
        self.assertIsNone(flow["today_flow"])
        today = next(row for row in result["industry_today_inflow"] if row["name"] == "半导体")
        self.assertEqual(today["today_return"], -5.35)
        self.assertAlmostEqual(today["today_flow"], 4_288_000_000, delta=1)

    def test_latest_concept_close_does_not_borrow_next_day_flow(self) -> None:
        data = {
            "week": {**WEEK, "collection_trade_date": "2026-07-11"},
            "market": {"sectors": {
                "industry_today": [], "concept_today": [],
                "concept_latest_close": [{"板块名称": "煤化工概念", "涨跌幅": 2.5, "source_date": "2026-07-10"}],
                "fund_flow": {
                    "今日": {"行业资金流": [], "概念资金流": [{"名称": "煤化工概念", "今日主力净流入-净额": 1e9, "资金单位": "元"}]},
                    "5日": {"行业资金流": [], "概念资金流": []},
                    "10日": {"行业资金流": [], "概念资金流": []},
                },
                "flow_meta": {"今日": {"概念资金流": {"source_date": "2026-07-11"}}},
            }},
        }
        result = weekly.analyze_sectors(data, {"funds": []})
        self.assertEqual(result["concept_today"][0]["source_date"], "2026-07-10")
        self.assertIsNone(result["concept_today"][0]["today_flow"])
        self.assertEqual(result["concept_today_inflow"], [])

    def test_flow_units_are_explicitly_rendered_as_billion_yuan(self) -> None:
        self.assertEqual(visual.flow_money(4_288_000_000), "+42.88亿元")
        self.assertEqual(visual.flow_money(-150_000_000), "-1.50亿元")
        self.assertEqual(visual.flow_money(None), "-")

    def test_sector_snapshot_metadata_is_preserved(self) -> None:
        data = {
            "week": WEEK,
            "market": {"sectors": {
                "industry_today": [], "concept_today": [],
                "fund_flow": {"今日": {"行业资金流": [], "概念资金流": []}, "5日": {"行业资金流": [{"名称": "半导体", "5日主力净流入-净额": 20, "5日涨跌幅": 3}], "概念资金流": []}, "10日": {"行业资金流": [], "概念资金流": []}},
                "flow_meta": {"5日": {"行业资金流": {"source_date": "2026-07-10", "cache_age_days": 3}}},
            }},
        }
        result = weekly.analyze_sectors(data, {"funds": []})["industry_return"][0]
        self.assertEqual(result["source_date"], "2026-07-10")
        self.assertEqual(result["cache_age_days"], 3)

    def test_post_period_today_flow_does_not_confirm_weekly_sector(self) -> None:
        item = weekly.sector_item(
            "半导体", "industry", 3.0, {"今日": 100, "5日": 20}, "5日资金流排行涨跌幅",
            {}, {}, "2026-07-10", "全市场",
            {"今日": {"source_date": "2026-07-13", "cache_age_days": 0}, "5日": {"source_date": "2026-07-10", "cache_age_days": 3}},
            3, "2026-07-10",
        )
        self.assertIsNone(item["today_flow"])
        self.assertEqual(item["flow_status"], "数据不足")
        self.assertIn("5日@2026-07-10", item["flow_basis"])
        self.assertIn("按亿元展示", item["flow_basis"])

    def test_historical_flow_builds_day_five_and_ten_periods(self) -> None:
        records = [
            {"日期": (dt.date(2026, 6, 29) + dt.timedelta(days=index)).isoformat(), "主力净流入-净额": index + 1}
            for index in range(12)
            if (dt.date(2026, 6, 29) + dt.timedelta(days=index)).weekday() < 5
        ]
        result = weekly.historical_flow_evidence(records, WEEK)
        self.assertEqual(result["source_date"], "2026-07-10")
        self.assertIsNotNone(result["今日"])
        self.assertIsNotNone(result["5日"])
        self.assertIsNotNone(result["10日"])

    def test_sector_classification_examples(self) -> None:
        expected = {
            "油田服务": ("资源能源", "油气产业链/油服"),
            "银行": ("金融", "银行"),
            "集成电路封测": ("科技", "半导体/封测"),
            "医疗服务": ("医药", "医疗服务"),
            "影视院线": ("消费", "传媒娱乐/院线"),
            "生猪养殖": ("农业", "养殖"),
            "船舶装备": ("制造", "国防军工/高端制造"),
        }
        for name, themes in expected.items():
            result = weekly.classify_sector(name)
            self.assertEqual((result["theme_l1"], result["theme_l2"]), themes)
        self.assertIn("红利", weekly.classify_sector("银行")["style_tags"])

    def test_compat_history_parser_uses_known_oil_sector_code(self) -> None:
        class Response:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {"data": {"klines": ["2026-07-09,100,0,0,0,0,0,0,0,0,0,0,0,0,0", "2026-07-10,200,0,0,0,0,0,0,0,0,0,0,0,0,0"]}}

        fake_requests = type("Requests", (), {"get": staticmethod(lambda url, **kwargs: Response())})
        with mock.patch.dict(sys.modules, {"requests": fake_requests}):
            rows = collector.eastmoney_sector_flow_history_compat("油田服务")
        self.assertEqual(rows[-1], {"日期": "2026-07-10", "主力净流入-净额": 200.0})


class ETFTests(unittest.TestCase):
    def test_adjusted_price_precedes_raw_price(self) -> None:
        good = [{"日期": "2026-07-03", "收盘": 1.0}, {"日期": "2026-07-10", "收盘": 1.1}]
        raw = [{"日期": "2026-07-03", "收盘": 1.0}, {"日期": "2026-07-10", "收盘": 2.0}]
        result = weekly.etf_return_evidence({"history": {"X": {"hfq": good, "qfq": good, "none": raw}}}, "X", WEEK)
        self.assertEqual(result["return_basis"], "后复权价格")
        self.assertAlmostEqual(result["week_return"], 10.0)

    def test_unadjusted_discontinuity_is_not_reported(self) -> None:
        raw = [{"日期": "2026-07-03", "收盘": 1.0}, {"日期": "2026-07-10", "收盘": 2.0}]
        result = weekly.etf_return_evidence({"history": {"X": {"none": raw}}}, "X", WEEK)
        self.assertIsNone(result["week_return"])
        self.assertIn("复权口径待确认", result["quality_flags"])

    def test_nav_and_feeder_fallbacks(self) -> None:
        nav = [{"日期": "2026-07-03", "单位净值": 1.0}, {"日期": "2026-07-10", "单位净值": 1.05}]
        nav_result = weekly.etf_return_evidence({"history": {"X": {}}, "nav": {"X": nav}}, "X", WEEK)
        self.assertEqual(nav_result["return_basis"], "ETF单位净值（无折算）")
        feeder_result = weekly.etf_return_evidence({"history": {"X": {}}, "feeder_nav": {"X": {"feeder_code": "000001", "records": nav}}}, "X", WEEK)
        self.assertTrue(feeder_result["return_basis"].startswith("联接基金"))

    def test_nav_discontinuity_is_rejected(self) -> None:
        broken_nav = [{"日期": "2026-07-03", "单位净值": 1.0}, {"日期": "2026-07-10", "单位净值": 0.4}]
        result = weekly.etf_return_evidence({"history": {"X": {}}, "nav": {"X": broken_nav}}, "X", WEEK)
        self.assertIsNone(result["week_return"])
        self.assertIn("ETF单位净值异常断点", result["quality_flags"])

    def test_cumulative_nav_survives_unit_nav_split(self) -> None:
        nav = [
            {"净值日期": "2026-07-03", "单位净值": 1.7686, "累计净值": 3.5372},
            {"净值日期": "2026-07-10", "单位净值": 0.9707, "累计净值": 3.8828},
        ]
        result = weekly.etf_return_evidence({"history": {"X": {}}, "nav": {"X": nav}}, "X", WEEK)
        self.assertEqual(result["return_basis"], "ETF累计净值")
        self.assertTrue(result["split_detected"])
        self.assertIn("份额折算", result["corporate_actions"])
        self.assertNotIn("份额折算", result["quality_flags"])

    def test_closing_nav_premium_is_date_aligned(self) -> None:
        data = {
            "week": WEEK,
            "candidate_etfs": {
                "codes": ["560780"], "spot_em": [],
                "spot_sina": [{"代码": "560780", "名称": "测试ETF", "最新价": 1.10, "成交额": 2e9}],
                "spot": [{"代码": "560780", "名称": "测试ETF", "最新价": 1.10, "成交额": 2e9}],
                "history": {"560780": {"hfq": [{"日期": "2026-07-03", "收盘": 1.0}, {"日期": "2026-07-10", "收盘": 1.05}], "none": [{"日期": "2026-07-10", "收盘": 1.05}]}},
                "nav": {"560780": [{"净值日期": "2026-07-10", "单位净值": 1.0}]},
                "history_sina": {}, "access": {"560780": {}},
            },
        }
        result = weekly.analyze_etfs(data)[0]
        self.assertAlmostEqual(result["premium_rate"], 5.0)
        self.assertEqual(result["premium_basis"], "收盘净值溢价")
        self.assertEqual(result["price_source"], "报告期末历史收盘价")
        self.assertEqual(result["eod_quality"]["as_of"], "2026-07-10")

    def test_later_live_quote_does_not_replace_report_end_quality(self) -> None:
        data = {
            "week": WEEK,
            "candidate_etfs": {
                "codes": ["560780"],
                "live_snapshot": {"560780": {"price": 1.30, "iopv": 1.0, "turnover": 9e9, "trade_time": "2026-07-11 10:00:00"}},
                "history": {"560780": {"hfq": [{"日期": "2026-07-03", "收盘": 1.0}, {"日期": "2026-07-10", "收盘": 1.05}], "none": [{"日期": "2026-07-10", "收盘": 1.05, "成交额": 2e9}]}},
                "nav": {"560780": [{"净值日期": "2026-07-10", "单位净值": 1.0}]},
                "history_sina": {}, "access": {"560780": {}},
            },
        }
        result = weekly.analyze_etfs(data)[0]
        self.assertEqual(result["price"], 1.05)
        self.assertAlmostEqual(result["premium_rate"], 5.0)
        self.assertAlmostEqual(result["live_snapshot"]["premium_rate"], 30.0)
        self.assertFalse(result["execution_ready"])

    def test_sina_history_supplies_report_end_turnover(self) -> None:
        data = {
            "week": WEEK,
            "candidate_etfs": {
                "codes": ["560780"], "spot": [],
                "history": {"560780": {"hfq": [], "qfq": [], "none": []}},
                "history_sina": {"560780": [
                    {"日期": "2026-07-03", "收盘": 1.0, "成交额": 80000000},
                    {"日期": "2026-07-10", "收盘": 1.1, "成交额": 120000000},
                ]},
                "nav": {"560780": [
                    {"净值日期": "2026-07-03", "单位净值": 1.0},
                    {"净值日期": "2026-07-10", "单位净值": 1.1},
                ]},
                "access": {"560780": {}},
            },
        }
        result = weekly.analyze_etfs(data)[0]
        self.assertEqual(result["eod_quality"]["turnover"], 120000000)
        self.assertEqual(result["turnover_source"], "新浪ETF历史成交额")

    def test_exceptional_return_without_crosscheck_is_not_recommendation_evidence(self) -> None:
        nav = [
            {"净值日期": "2026-07-03", "累计净值": 4.0},
            {"净值日期": "2026-07-10", "累计净值": 3.2},
        ]
        result = weekly.etf_return_evidence({"history": {"X": {}}, "nav": {"X": nav}}, "X", WEEK)
        self.assertAlmostEqual(result["week_return"], -20.0)
        self.assertEqual(result["return_confidence"], "中")
        self.assertFalse(result["supports_recommendation"])


class SourceAndProfileTests(unittest.TestCase):
    def test_empty_optional_dataset_is_not_required(self) -> None:
        status = access.dataset_status(
            "industry_flow_history:leaders", [], requirement="optional", impact="score",
            empty_status="not_required", empty_reason="无待补采行业",
        )
        self.assertEqual(status["status"], "not_required")
        self.assertEqual(access.unresolved_warnings([status]), [])

    def test_candidate_etf_spot_filters_full_market_response(self) -> None:
        class Response:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, object]:
                return {"data": {"diff": [
                    {"f12": "560780", "f14": "目标ETF", "f2": 1.25, "f6": 2_000_000, "f124": 1784250000},
                    {"f12": "510300", "f14": "无关ETF", "f2": 4.0, "f6": 3_000_000, "f124": 1784250000},
                ]}}

        with mock.patch("requests.get", return_value=Response()) as get:
            rows = collector.eastmoney_etf_spot_candidates(["560780"])
        self.assertEqual([row["代码"] for row in rows], ["560780"])
        self.assertEqual(get.call_args.kwargs["params"]["pz"], "5000")

    def test_historical_cache_ignores_collection_day_and_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = access.AkshareClient(
                object(), Path(directory),
                context={"mode": "quick", "week": {**WEEK, "collection_trade_date": "2026-07-16"}},
            )
            second = access.AkshareClient(
                object(), Path(directory),
                context={"mode": "full", "week": {**WEEK, "collection_trade_date": "2026-07-17"}},
            )
            variants = [{"symbol": "000300", "start_date": "20260601", "end_date": "20260710"}]
            self.assertEqual(
                first._cache_path("index_zh_a_hist", variants, WEEK, 200),
                second._cache_path("index_zh_a_hist", variants, WEEK, 200),
            )

    def test_realtime_cache_keeps_collection_day(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = access.AkshareClient(
                object(), Path(directory), context={"week": {**WEEK, "collection_trade_date": "2026-07-16"}},
            )
            second = access.AkshareClient(
                object(), Path(directory), context={"week": {**WEEK, "collection_trade_date": "2026-07-17"}},
            )
            self.assertNotEqual(
                first._cache_path("fund_etf_spot_em", [{}], None),
                second._cache_path("fund_etf_spot_em", [{}], None),
            )

    def test_cache_limit_is_part_of_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = access.AkshareClient(object(), Path(directory))
            self.assertNotEqual(
                client._cache_path("fund_open_fund_rank_em", [{"symbol": "全部"}], None, 20),
                client._cache_path("fund_open_fund_rank_em", [{"symbol": "全部"}], None, 200),
            )

    def test_lof_alone_is_not_classified_as_passive(self) -> None:
        self.assertEqual(weekly.classify_product("平安鼎越混合(LOF)"), "主动基金")
        self.assertEqual(weekly.classify_product("某中证半导体指数LOF"), "被动指数/ETF联接")

    def test_only_latest_disclosure_period_drives_themes(self) -> None:
        rows = [
            {"季度": "2025年4季度股票投资明细", "股票名称": "北方华创"},
            {"季度": "2026年1季度股票投资明细", "股票名称": "新易盛"},
        ]
        latest = weekly.latest_disclosure_rows(rows)
        self.assertEqual([row["股票名称"] for row in latest], ["新易盛"])

    def test_generic_product_tags_do_not_count_as_theme_overlap(self) -> None:
        counter, _, _ = weekly.portfolio_theme_context({"funds": [{"name": "A", "themes": ["主动权益", "混合型", "AI光模块/通信"], "current_weight": 1}]})
        self.assertEqual(counter, {"AI光模块/通信": 1})

    def test_recovered_chain_is_not_an_unresolved_warning(self) -> None:
        statuses = [
            {"function": "primary", "status": "failed", "record_count": 0},
            {"function": "fallback", "status": "ok", "record_count": 10},
        ]
        result = access.dataset_status("style_index:000300", statuses)
        self.assertEqual(result["status"], "fallback_used")
        self.assertEqual(access.unresolved_warnings([result]), [])

    def test_mixed_sector_values_are_normalized(self) -> None:
        rows = collector.normalize_sector_flow_diff([
            {"f14": "半导体", "f109": "3.50", "f164": "--"},
            {"f14": "通信", "f109": -1.2, "f164": "1,200"},
        ], "5日")
        self.assertIsNone(rows[0]["5日主力净流入-净额"])
        self.assertEqual(rows[1]["5日主力净流入-净额"], 1200)

    def test_profile_cache_staleness(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            collector.save_profile_cache(root, "000001", {"basic_info": [{"item": "基金规模", "value": "10亿"}]})
            fresh = collector.load_profile_cache(root, "000001")
            self.assertEqual(fresh["profile_status"], "partial_profile")
            payload = json.loads((root / "000001.json").read_text(encoding="utf-8"))
            payload["cached_at"] = (dt.date.today() - dt.timedelta(days=100)).isoformat()
            (root / "000001.json").write_text(json.dumps(payload), encoding="utf-8")
            stale = collector.load_profile_cache(root, "000001")
            self.assertEqual(stale["profile_status"], "stale_profile")

    def test_profile_cache_marks_missing_holdings_partial(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = collector.save_profile_cache(root, "000001", {"basic_info": [{"item": "基金规模", "value": "10亿"}]})
            self.assertEqual(payload["profile_status"], "partial_profile")
            self.assertEqual(collector.load_profile_cache(root, "000001")["profile_status"], "partial_profile")

    def test_profile_cache_reuses_previous_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            collector.save_profile_cache(
                root / "v21" / "profiles", "000001",
                {"basic_info": [{"item": "基金规模", "value": "10亿"}], "stock_holdings": [{"日期": "2026-03-31", "股票名称": "新易盛"}]},
            )
            loaded = collector.load_profile_cache(root / "v22" / "profiles", "000001")
            self.assertEqual(loaded["profile_status"], "ok")
            self.assertIn("v21/profiles", loaded["cache_path"])


class AllocationTests(unittest.TestCase):
    def test_small_unchanged_position_is_not_clear_candidate(self) -> None:
        funds = [{"code": "S", "name": "Small", "score": 50, "current_weight": 0.02, "themes": [], "industries": [], "is_core": False}]
        funds += [{"code": str(index), "name": str(index), "score": 50, "current_weight": 0.14, "themes": [], "industries": [], "is_core": False} for index in range(7)]
        rows = portfolio.target_allocations(funds, {"inflow": [], "outflow": []}, {"features": []}, 100000)
        small = next(row for row in rows if row["code"] == "S")
        self.assertEqual(small["action"], "观察")

    def test_theme_cap_is_enforced(self) -> None:
        funds = [{"code": str(index), "name": str(index), "score": 70 - index, "current_weight": 0.2, "themes": ["半导体"], "industries": [], "is_core": False} for index in range(5)]
        rows = portfolio.target_allocations(funds, {"inflow": [], "outflow": []}, {"features": []}, 100000)
        target = sum(row["target_weight"] for row in rows if row["code"] != "CASH")
        self.assertLessEqual(target, 0.400001)
        self.assertEqual(portfolio.validate_allocations(rows, funds), [])

    def test_high_volatility_first_step_is_capped(self) -> None:
        funds = [{"code": str(index), "name": str(index), "score": 70 - index, "current_weight": 0.2, "themes": ["半导体"], "industries": [], "is_core": False} for index in range(5)]
        rows = portfolio.target_allocations(funds, {"inflow": [], "outflow": []}, {"features": []}, 100000)
        self.assertTrue(all(abs(row["first_step_delta"]) <= 0.100001 for row in rows if row["code"] != "CASH"))


class DecisionTests(unittest.TestCase):
    def test_small_negative_month_without_valid_score_is_observation(self) -> None:
        portfolio_model = {"funds": [{
            "code": "A", "name": "A", "week_return": -1, "one_month": -0.02,
            "themes": ["AI光模块/通信"], "current_weight": 1,
            "candidate_kind": "fund", "product_evidence_available": True,
            "theme_evidence_available": True, "quality_flags": [],
        }]}
        result = weekly.compare_and_recommend(portfolio_model, {"industry_return": [], "concept_return": []}, [], [], [])
        self.assertEqual(result["current_vs_weekly"][0]["decision_action"], "观察")
        self.assertTrue(result["current_vs_weekly"][0]["score_missing_components"])
        self.assertNotIn("insufficient", result["replacement_status_display"])

    def test_conclusion_separates_confirmed_and_unconfirmed_sectors(self) -> None:
        sector = lambda name, status: {  # noqa: E731
            "name": name, "week_return": 3, "flow_status": status, "current_coverage": "缺失",
            "exposure_keys": [name], "flow_status_reason": status,
        }
        result = weekly.compare_and_recommend(
            {"funds": []},
            {"industry_return": [sector("油田服务", "持续流入"), sector("半导体", "数据不足")], "concept_return": []},
            [], [], [],
        )
        conclusion = result["weekly_conclusion"]
        self.assertEqual([row["name"] for row in conclusion["confirmed_leaders"]], ["油田服务"])
        self.assertIn("资金待确认", conclusion["flow_summary"])

    def test_observation_only_replacements_do_not_claim_first_step_weight(self) -> None:
        summary = weekly.replacement_decision_summary([
            {"execution_ready": False}, {"execution_ready": False},
        ])
        self.assertIn("不给出即时买入比例", summary)
        self.assertNotIn("3%至5%", summary)

    def test_top10_non_match_is_not_positive_sector_evidence(self) -> None:
        self.assertFalse(weekly.has_matched_sector_evidence({"score_evidence": ["未进入板块收益Top10，暂无资金确认"]}))
        self.assertTrue(weekly.has_matched_sector_evidence({"score_evidence": ["半导体 3.20%/持续流入"]}))

    def test_fund_ranking_duplicate_does_not_supply_etf_month_score(self) -> None:
        etf = {
            "code": "560780", "name": "半导体设备ETF广发", "candidate_kind": "etf",
            "week_return": 3, "one_month": None, "turnover": 1e9, "premium_rate": 0,
            "recommendation_eligible": True, "quality_flags": [],
        }
        duplicate_fund = {
            "code": "560780", "name": "半导体设备ETF广发", "candidate_kind": "fund",
            "week_return": 3, "one_month": 20, "product_evidence_available": True,
            "theme_evidence_available": True, "quality_flags": [],
        }
        sector = {
            "name": "半导体", "week_return": 2, "flow_status": "持续流入",
            "exposure_keys": ["半导体设备/材料"],
        }
        weekly.compare_and_recommend(
            {"funds": []}, {"industry_return": [sector], "concept_return": []},
            [{"name": "科创50", "week_return": 1}], [etf], [duplicate_fund],
        )
        self.assertIsNone(etf["score_components"]["one_month_trend"])
        self.assertIsNone(etf["weekly_score"])

    def test_html_translates_internal_states_and_labels_percentages(self) -> None:
        payload = {
            "schema_version": 2, "data_revision": "2.2", "week": {},
            "portfolio": {"funds": [], "weight_basis_display": "等权分析假设", "weight_assumption": "每只约14.29%"},
            "market": {"style_indexes": [], "sector_top10": {}, "weekly_top_funds": []},
            "candidate_etfs": [], "warnings": [], "data_quality": [], "source_audit": [],
            "comparison": {"replacement_status": "insufficient_evidence", "replacement_status_display": "证据不足，暂不生成替换建议", "weekly_conclusion": {}},
        }
        output = visual.render(payload)
        self.assertNotIn("insufficient_evidence", output)
        self.assertIn("当前组合占比", output)
        self.assertIn("近1年最大回撤", output)


class ValidatorTests(unittest.TestCase):
    def test_legacy_schema_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "analysis.json"
            path.write_text(json.dumps({"portfolio": {"funds": [{}]}}), encoding="utf-8")
            with self.assertRaises(SystemExit):
                validator.validate_analysis(path)

    def test_high_premium_actionable_replacement_is_rejected(self) -> None:
        payload = {
            "schema_version": 2,
            "week": {"end_date": "2026-07-10"},
            "portfolio": {
                "nav_coverage_weight": 1,
                "weekly_return": 1,
                "funds": [{"code": "A", "one_month": 1, "three_month": 1, "max_drawdown_1y": -1, "data_status": "ok", "latest_date": "2026-07-10"}],
            },
            "market": {"sector_top10": {}},
            "candidate_etfs": [{"code": "X", "week_return": 1, "return_basis": "后复权价格", "premium_rate": 3, "turnover": 1, "updated_at": "x"}],
            "comparison": {
                "replacement_status": "ok",
                "replacement_top3": [{"candidate_code": "X", "replace_score": 40, "candidate_score": 50, "score_gap": 10, "evidence": ["x"], "risk_flags": [], "candidate_return_basis": "后复权价格", "candidate_premium_rate": 3, "suggested_first_step_weight": 0.03}],
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "analysis.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(SystemExit):
                validator.validate_analysis(path)


if __name__ == "__main__":
    unittest.main()
