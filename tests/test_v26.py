from __future__ import annotations

import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from cache_store import CacheStore, stable_hash  # noqa: E402
from collect_weekly_data import (  # noqa: E402
    TUSHARE_PROVIDER,
    apply_cached_sector_flow_overlay,
    chunked_tushare_flow,
    complete_cached_sector_flow,
)
from finalize_weekly_analysis import validate_synthesis  # noqa: E402
from three_week_analysis import (  # noqa: E402
    analyze_portfolio,
    build_periods,
    deterministic_synthesis,
    period_return,
    rotation_state,
    sector_portfolio_coverage,
    select_rotation_rows,
)


class CacheStoreTests(unittest.TestCase):
    def test_cached_sector_overlay_closes_required_flow_datasets_without_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory, CacheStore(directory) as store:
            dates = [dt.date(2026, 7, day) for day in (6, 7, 8, 9, 10)]
            for day in dates:
                compact = day.strftime("%Y%m%d")
                store.upsert_series(TUSHARE_PROVIDER, "industry_flow_daily", "881001.TI", [{
                    "trade_date": compact, "ts_code": "881001.TI", "industry": "半导体",
                    "net_amount": 2, "pct_change": 1,
                }])
                store.upsert_series(TUSHARE_PROVIDER, "concept_flow_daily", "885001.TI", [{
                    "trade_date": compact, "ts_code": "885001.TI", "name": "先进封装",
                    "net_amount": 1, "pct_change": 0.5, "industry_index": 1234,
                }])
            payload = {
                "week": {"end_date": "2026-07-10"},
                "market": {"sectors": {"fund_flow": {}, "flow_meta": {}, "universe_scope": {}}},
            }
            datasets = [
                {"dataset": "industry_flow:5日", "status": "failed"},
                {"dataset": "concept_flow:5日", "status": "failed"},
                {"dataset": "concept_board:latest_close", "status": "failed"},
            ]
            used = apply_cached_sector_flow_overlay(payload, datasets, store, dates)
        self.assertEqual(used, ["industry_flow", "concept_flow"])
        status = {row["dataset"]: row for row in datasets}
        self.assertEqual(status["industry_flow:5日"]["status"], "fallback_used")
        self.assertEqual(status["concept_flow:5日"]["status"], "fallback_used")
        self.assertEqual(status["concept_board:latest_close"]["status"], "fallback_used")
        self.assertEqual(payload["market"]["sectors"]["concept_latest_close"][0]["板块名称"], "先进封装")

    def test_validated_sector_cache_is_read_without_live_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as directory, CacheStore(directory) as store:
            dates = [dt.date(2026, 7, day) for day in (6, 7, 8, 9, 10)]
            for index, day in enumerate(dates):
                store.upsert_series(TUSHARE_PROVIDER, "industry_flow_daily", "881001.TI", [{
                    "trade_date": day.strftime("%Y%m%d"), "ts_code": "881001.TI",
                    "industry": "半导体", "net_amount": index + 1, "pct_change": 1,
                }])
            rows = complete_cached_sector_flow(store, "industry", dates)
            self.assertEqual(len(rows), 5)

    def test_series_is_deduplicated_by_provider_dataset_symbol_and_date(self) -> None:
        with tempfile.TemporaryDirectory() as directory, CacheStore(directory) as store:
            store.upsert_series("p", "fund_nav", "000001", [{"trade_date": "20260710", "close": 1.0}])
            store.upsert_series("p", "fund_nav", "000001", [{"trade_date": "20260710", "close": 1.1}])
            rows = store.get_series("fund_nav", "000001")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["close"], 1.1)

    def test_empty_update_does_not_erase_successful_series(self) -> None:
        with tempfile.TemporaryDirectory() as directory, CacheStore(directory) as store:
            store.upsert_series("p", "style_index", "沪深300", [{"日期": "2026-07-10", "收盘": 4000}])
            self.assertEqual(store.upsert_series("p", "style_index", "沪深300", []), 0)
            self.assertEqual(len(store.get_series("style_index", "沪深300")), 1)

    def test_audit_reports_cache_hit_rate(self) -> None:
        with tempfile.TemporaryDirectory() as directory, CacheStore(directory) as store:
            store.record_audit("run", [{"dataset": "a", "status": "ok", "cache_hit": True}, {"dataset": "b", "status": "ok"}])
            self.assertEqual(store.cache_stats("run")["hit_rate"], 0.5)

    def test_snapshot_expiry_uses_full_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as directory, CacheStore(directory) as store:
            as_of = dt.date.today().isoformat()
            store.put_snapshot("p", "etf_live_quote", "scope", as_of, [{"price": 1}], expires_at=(dt.datetime.now() - dt.timedelta(seconds=1)).isoformat())
            self.assertIsNone(store.get_snapshot("etf_live_quote", "scope", as_of, provider="p"))
            store.put_snapshot("p", "etf_live_quote", "scope", as_of, [{"price": 2}], expires_at=(dt.datetime.now() + dt.timedelta(minutes=5)).isoformat())
            self.assertEqual(store.get_snapshot("etf_live_quote", "scope", as_of, provider="p")[0]["price"], 2)

    def test_cache_schema_migration_version_is_two(self) -> None:
        with tempfile.TemporaryDirectory() as directory, CacheStore(directory) as store:
            versions = [row[0] for row in store.connection.execute("SELECT version FROM schema_migrations ORDER BY version")]
            self.assertIn(2, versions)


class ThreeWeekTests(unittest.TestCase):
    def test_periods_follow_real_trade_weeks_and_gate_partial_w0(self) -> None:
        dates = [dt.date(2026, 6, 26)] + [dt.date(2026, 6, 29) + dt.timedelta(days=i) for i in range(5)]
        dates += [dt.date(2026, 7, 6) + dt.timedelta(days=i) for i in range(5)]
        dates += [dt.date(2026, 7, 13) + dt.timedelta(days=i) for i in range(4)]
        periods = build_periods(dates, dt.date(2026, 7, 16), current_complete=False)
        self.assertEqual([row["period_id"] for row in periods], ["W-2", "W-1", "W0"])
        self.assertTrue(periods[0]["eligible_for_action"])
        self.assertFalse(periods[-1]["eligible_for_action"])

    def test_period_return_rejects_future_only_endpoint(self) -> None:
        values = [(dt.date(2026, 7, 10), 1.0), (dt.date(2026, 7, 17), 1.2)]
        value, latest = period_return(values, dt.date(2026, 7, 10), dt.date(2026, 7, 16), dt.date(2026, 7, 13))
        self.assertIsNone(value)
        self.assertEqual(latest, "2026-07-10")

    def test_period_return_rejects_stale_baseline(self) -> None:
        values = [(dt.date(2026, 6, 20), 1.0), (dt.date(2026, 7, 16), 1.2)]
        value, _ = period_return(values, dt.date(2026, 7, 10), dt.date(2026, 7, 16), dt.date(2026, 7, 13))
        self.assertIsNone(value)

    def test_rotation_needs_two_complete_valid_weeks(self) -> None:
        periods = [
            {"period_id": "W-2", "completeness": "complete"},
            {"period_id": "W-1", "completeness": "complete"},
            {"period_id": "W0", "completeness": "partial"},
        ]
        values = {
            "W-2": {"data_status": "insufficient_data", "return": None, "weekly_net_flow": 1},
            "W-1": {"data_status": "ok", "return": 2, "weekly_net_flow": 1},
            "W0": {"data_status": "ok", "return": 8, "weekly_net_flow": 5},
        }
        self.assertEqual(rotation_state(values, periods)[0], "数据不足")

    def test_portfolio_reports_complete_week_compound_separately(self) -> None:
        periods = [
            {"period_id": "W-1", "baseline_date": "2026-07-03", "start_date": "2026-07-06", "end_date": "2026-07-10", "completeness": "complete"},
            {"period_id": "W0", "baseline_date": "2026-07-10", "start_date": "2026-07-13", "end_date": "2026-07-16", "completeness": "partial"},
        ]
        raw = {"funds": {"A": {"nav": [
            {"净值日期": "2026-07-03", "累计净值": 1.0},
            {"净值日期": "2026-07-10", "累计净值": 1.1},
            {"净值日期": "2026-07-16", "累计净值": 1.21},
        ]}}}
        portfolio = {"funds": [{"code": "A", "name": "A", "current_weight": 1.0}]}
        result = analyze_portfolio(raw, portfolio, periods)
        self.assertAlmostEqual(result["completed_weeks_compound_return"], 10.0)
        self.assertAlmostEqual(result["three_week_compound_return"], 21.0)
        self.assertIn("进行中周", result["compound_basis"])

    def test_sector_coverage_uses_theme_aliases_and_weights(self) -> None:
        portfolio = {"funds": [
            {"name": "通信基金", "current_weight": 0.2, "themes": ["AI光模块/通信", "主动权益"]},
            {"name": "PCB基金", "current_weight": 0.1, "themes": ["PCB/AI服务器"]},
        ]}
        direct = sector_portfolio_coverage("通信设备", portfolio)
        indirect = sector_portfolio_coverage("云计算", portfolio)
        self.assertEqual(direct["portfolio_coverage"], "直接主题覆盖")
        self.assertAlmostEqual(direct["coverage_weight"], 0.2)
        self.assertEqual(indirect["portfolio_coverage"], "间接主题覆盖")

    def test_display_union_keeps_earlier_leader_and_portfolio_related_row(self) -> None:
        periods = [{"period_id": "W-2"}, {"period_id": "W-1"}, {"period_id": "W0"}]
        rows = [
            {"entity_id": "early", "name": "前周主线", "coverage_weight": 0, "periods": {
                "W-2": {"return_percentile": 100, "flow_percentile": 100}, "W-1": {}, "W0": {}}},
            {"entity_id": "held", "name": "持仓相关", "coverage_weight": 0.2, "periods": {
                "W-2": {}, "W-1": {}, "W0": {"return_percentile": 1, "flow_percentile": 1}}},
        ]
        selected = {row["entity_id"] for row in select_rotation_rows(rows, periods)}
        self.assertEqual(selected, {"early", "held"})

    def test_recovering_sector_is_not_reported_as_current_fading(self) -> None:
        periods = [{"period_id": "W-2"}, {"period_id": "W-1"}, {"period_id": "W0"}]
        row = {
            "entity_id": "A", "name": "修复行业", "kind": "industry",
            "rotation_state": "退潮", "rotation_reason": "完整周退潮", "monitor_state": "进行中修复观察",
            "periods": {pid: {"return_percentile": 50} for pid in ("W-2", "W-1", "W0")},
        }
        evidence = {"evidence": {}, "evidence_hash": "h", "prompt_version": "v"}
        result = deterministic_synthesis(periods, {"current_regime": "无明确主线"}, [row], {}, evidence)
        self.assertNotIn("修复行业", result["fading_sectors"])
        self.assertTrue(any("修复" in item for item in result["uncertainties"]))


class FlowPaginationTests(unittest.TestCase):
    def test_flow_collection_is_split_into_five_day_windows_and_deduplicated(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.calls = []

            def call(self, dataset, api_name, params, key_extra=None):
                self.calls.append(params)
                start = dt.datetime.strptime(params["start_date"], "%Y%m%d").date()
                end = dt.datetime.strptime(params["end_date"], "%Y%m%d").date()
                rows = []
                cursor = start
                while cursor <= end:
                    if cursor.weekday() < 5:
                        rows.append({"trade_date": cursor.strftime("%Y%m%d"), "ts_code": "A", "net_amount": 1})
                    cursor += dt.timedelta(days=1)
                return rows

        dates = [dt.date(2026, 6, 29) + dt.timedelta(days=i) for i in range(12) if (dt.date(2026, 6, 29) + dt.timedelta(days=i)).weekday() < 5]
        client = Client()
        rows = chunked_tushare_flow(client, "concept_flow", "moneyflow_cnt_ths", dates)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(len(rows), len(dates))


class LlmFinalizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.evidence = {
            "evidence": {
                "a": {"entity_name": "半导体", "period": "W-1"},
                "b": {"entity_name": "半导体", "period": "W0"},
            }
        }
        self.evidence["evidence_hash"] = stable_hash(self.evidence["evidence"])

    def synthesis(self) -> dict:
        return {
            "market_regime": "价值防御", "rotation_path": [], "persistent_leaders": ["半导体"],
            "emerging_sectors": [], "fading_sectors": [], "portfolio_implications": ["控制同质化"],
            "action_explanations": ["观察并等待确认"], "uncertainties": [], "confidence": "中",
            "evidence_refs": ["a", "b"], "model": "codex", "prompt_version": "three-week-v1",
            "generated_at": "2026-07-17T12:00:00", "evidence_hash": self.evidence["evidence_hash"],
        }

    def test_valid_cross_week_synthesis_passes(self) -> None:
        self.assertEqual(validate_synthesis(self.synthesis(), self.evidence), [])

    def test_unknown_entity_and_action_ratio_are_rejected(self) -> None:
        synthesis = self.synthesis()
        synthesis["emerging_sectors"] = ["不存在板块"]
        synthesis["action_explanations"] = ["买入10%"]
        errors = validate_synthesis(synthesis, self.evidence)
        self.assertTrue(any("证据外实体" in error for error in errors))
        self.assertTrue(any("比例" in error or "越过" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
