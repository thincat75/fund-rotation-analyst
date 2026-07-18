from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from margin_leverage import (  # noqa: E402
    COMPARABLE_START,
    _latest_percentile,
    analyze_margin_leverage,
    build_three_week_margin,
    combine_exchanges,
    normalize_daily_info_rows,
    normalize_exchange_market_snapshot,
    normalize_margin_rows,
    normalize_sse_market_api_rows,
    validate_margin_identity,
)
from cache_store import CacheStore  # noqa: E402
from collect_weekly_data import collect_margin_leverage_data  # noqa: E402
import collect_weekly_data as weekly_collector  # noqa: E402
import analyze_weekly as weekly_analyzer  # noqa: E402
from calibrate_margin_model import calibrate  # noqa: E402
from unittest.mock import patch  # noqa: E402


def sample_history(days: int = 620) -> tuple[dict, dict[str, list[dict]]]:
    start = dt.date(2023, 1, 2)
    exchanges = {"SSE": [], "SZSE": [], "BSE": []}
    markets = {"SSE": [], "SZSE": []}
    styles = {name: [] for name in ("中证全指", "沪深300", "中证1000")}
    for index in range(days):
        day = (start + dt.timedelta(days=index)).isoformat()
        for exchange, factor in (("SSE", 1.0), ("SZSE", 0.8)):
            financing = (10_000 + index * 2) * 100_000_000 * factor
            lending = 100 * 100_000_000 * factor
            exchanges[exchange].append({
                "trade_date": day,
                "exchange": exchange,
                "financing_balance": financing,
                "financing_buy": 400 * 100_000_000 * factor,
                "financing_repay": 390 * 100_000_000 * factor,
                "lending_balance": lending,
                "margin_balance": financing + lending,
                "unit": "元",
            })
            markets[exchange].append({
                "trade_date": day,
                "exchange": exchange,
                "float_market_cap": 500_000 * 100_000_000 * factor,
                "market_turnover": 6_000 * 100_000_000 * factor,
                "unit": "元",
            })
        exchanges["BSE"].append({
            "trade_date": day,
            "exchange": "BSE",
            "financing_balance": 10,
            "lending_balance": 1,
            "margin_balance": 11,
        })
        for name, offset in (("中证全指", 0), ("沪深300", 20), ("中证1000", -20)):
            styles[name].append({"日期": day, "收盘": 1000 + index + offset})
    return {"exchanges": exchanges, "market_daily": markets}, styles


class MarginNormalizationTests(unittest.TestCase):
    def test_margin_units_are_explicit(self) -> None:
        yuan = normalize_margin_rows([{"trade_date": "20260716", "rzye": 100, "rqye": 2, "rzrqye": 102}], "SSE", "tushare")
        yi = normalize_margin_rows([{"日期": "2026-07-16", "融资余额": 100, "融券余额": 2, "融资融券余额": 102}], "SZSE", "fixture", unit="亿元")
        self.assertEqual(yuan[0]["margin_balance"], 102)
        self.assertEqual(yi[0]["margin_balance"], 10_200_000_000)

    def test_daily_info_converts_billion_yuan(self) -> None:
        rows = normalize_daily_info_rows([{"trade_date": "20260716", "ts_code": "SH_A", "float_mv": 500, "amount": 20}], "SSE", "proxy")
        self.assertEqual(rows[0]["float_market_cap"], 50_000_000_000)
        self.assertEqual(rows[0]["market_turnover"], 2_000_000_000)

    def test_exchange_snapshots_declare_source_units(self) -> None:
        sse = normalize_exchange_market_snapshot([{"单日情况": "流通市值", "股票": 500}, {"单日情况": "成交金额", "股票": 20}], "SSE", "2026-07-16", "sse")
        szse = normalize_exchange_market_snapshot([{"证券类别": "股票", "流通市值": 500, "成交金额": 20}], "SZSE", "2026-07-16", "szse")
        self.assertEqual(sse[0]["float_market_cap"], 50_000_000_000)
        self.assertEqual(szse[0]["float_market_cap"], 500)

    def test_exchange_snapshots_prefer_a_share_components(self) -> None:
        sse = normalize_exchange_market_snapshot([
            {"单日情况": "流通市值", "股票": 515, "主板A": 400, "主板B": 15, "科创板": 100},
            {"单日情况": "成交金额", "股票": 52, "主板A": 40, "主板B": 2, "科创板": 10},
        ], "SSE", "2026-07-16", "sse")
        szse = normalize_exchange_market_snapshot([
            {"证券类别": "股票", "流通市值": 515, "成交金额": 52},
            {"证券类别": "主板A股", "流通市值": 400, "成交金额": 40},
            {"证券类别": "主板B股", "流通市值": 15, "成交金额": 2},
            {"证券类别": "创业板A股", "流通市值": 100, "成交金额": 10},
        ], "SZSE", "2026-07-16", "szse")
        self.assertEqual(sse[0]["float_market_cap"], 500 * 100_000_000)
        self.assertEqual(sse[0]["market_turnover"], 50 * 100_000_000)
        self.assertEqual(szse[0]["float_market_cap"], 500)
        self.assertEqual(szse[0]["market_turnover"], 50)

    def test_sse_raw_history_normalizer_handles_missing_aggregate_row(self) -> None:
        rows = normalize_sse_market_api_rows([
            {"PRODUCT_CODE": "01", "NEGO_VALUE": "400", "TRADE_AMT": "40"},
            {"PRODUCT_CODE": "02", "NEGO_VALUE": "15", "TRADE_AMT": "2"},
            {"PRODUCT_CODE": "03", "NEGO_VALUE": "100", "TRADE_AMT": "10"},
        ], "2022-01-04", "sse")
        self.assertEqual(rows[0]["float_market_cap"], 500 * 100_000_000)
        self.assertEqual(rows[0]["market_turnover"], 50 * 100_000_000)

    def test_identity_tolerance_and_future_rejection(self) -> None:
        rows = normalize_margin_rows([
            {"trade_date": "20260716", "rzye": 100, "rqye": 2, "rzrqye": 102},
            {"trade_date": "20260717", "rzye": 100, "rqye": 2, "rzrqye": 102},
        ], "SSE", "proxy", cutoff="2026-07-16")
        self.assertEqual(len(rows), 1)
        self.assertEqual(validate_margin_identity(rows), [])
        self.assertEqual(validate_margin_identity([{**rows[0], "margin_balance": 80}]), ["2026-07-16"])


class MarginAnalysisTests(unittest.TestCase):
    def test_current_observation_is_not_part_of_its_own_percentile(self) -> None:
        rows = [{"trade_date": f"2025-{index // 28 + 1:02d}-{index % 28 + 1:02d}", "metric": index} for index in range(500)]
        rows.append({"trade_date": "2026-07-16", "metric": 249.5})
        self.assertEqual(_latest_percentile(rows, "metric"), 50.0)

    def test_complete_model_excludes_bse_and_scores_without_concentration(self) -> None:
        raw, styles = sample_history()
        result = analyze_margin_leverage(raw, styles, cutoff=raw["exchanges"]["SSE"][-1]["trade_date"])
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["scope"], "SSE+SZSE")
        self.assertEqual(result["action_policy"], "display_only")
        self.assertIsNotNone(result["heat"]["score"])
        self.assertAlmostEqual(result["heat"]["coverage"], 0.85)
        expected = raw["exchanges"]["SSE"][-1]["margin_balance"] + raw["exchanges"]["SZSE"][-1]["margin_balance"]
        self.assertEqual(result["current"]["margin_balance"], expected)
        self.assertIsNotNone(result["current"]["bse_display_only"])
        self.assertIn("不是越高越好", result["metric_guide"]["financing_leverage_density"]["direction"])
        self.assertIn("越低越平稳", result["metric_guide"]["deleveraging_pressure"]["direction"])

    def test_partial_ratio_history_is_not_labeled_all_history(self) -> None:
        raw, styles = sample_history(620)
        result = analyze_margin_leverage(raw, styles, cutoff=raw["exchanges"]["SSE"][-1]["trade_date"])
        history = result["history_position"]
        self.assertFalse(history["full_ratio_history_available"])
        self.assertIsNone(history["financing_density_all_history_percentile"])
        self.assertEqual(history["ratio_history_observations"], 620)

    def test_missing_market_scale_suppresses_heat(self) -> None:
        raw, styles = sample_history()
        raw["market_daily"] = {}
        result = analyze_margin_leverage(raw, styles, cutoff=raw["exchanges"]["SSE"][-1]["trade_date"])
        self.assertEqual(result["status"], "partial")
        self.assertIsNone(result["heat"]["score"])
        self.assertTrue(result["data_quality"])

    def test_missing_one_exchange_does_not_publish_market_total(self) -> None:
        raw, styles = sample_history()
        raw["exchanges"]["SZSE"] = []
        result = analyze_margin_leverage(raw, styles, cutoff=raw["exchanges"]["SSE"][-1]["trade_date"])
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["current"], {})

    def test_pre_comparable_history_is_excluded(self) -> None:
        raw, styles = sample_history()
        old = {**raw["exchanges"]["SSE"][0], "trade_date": "2014-09-19", "margin_balance": 10**20}
        raw["exchanges"]["SSE"].insert(0, old)
        raw["exchanges"]["SZSE"].insert(0, {**old, "exchange": "SZSE"})
        result = analyze_margin_leverage(raw, styles, cutoff=raw["exchanges"]["SSE"][-1]["trade_date"])
        self.assertNotEqual(result["history_position"]["peak_date"], "2014-09-19")
        self.assertEqual(result["history_position"]["comparable_start"], COMPARABLE_START)

    def test_peak_gap_and_changes_are_auditable(self) -> None:
        raw, styles = sample_history()
        cutoff = raw["exchanges"]["SSE"][-1]["trade_date"]
        result = analyze_margin_leverage(raw, styles, cutoff=cutoff)
        self.assertLessEqual(result["history_position"]["peak_gap_pct"], 0)
        self.assertIsNotNone(result["trends"]["change_5d_pct"])
        self.assertIsNotNone(result["trends"]["change_20d_pct"])
        for row in result["historical_comparisons"]:
            if row.get("peak_financing_to_float_cap") is not None:
                self.assertIsNotNone(row.get("peak_financing_to_float_cap_date"))
            if row.get("peak_financing_buy_to_turnover") is not None:
                self.assertIsNotNone(row.get("peak_financing_buy_to_turnover_date"))

    def test_conclusions_are_non_deterministic(self) -> None:
        raw, styles = sample_history()
        result = analyze_margin_leverage(raw, styles, cutoff=raw["exchanges"]["SSE"][-1]["trade_date"])
        text = str(result["regime"])
        for forbidden in ("上涨空间很大", "马上调整", "必然见底", "一定看多"):
            self.assertNotIn(forbidden, text)

    def test_three_week_path_uses_non_overlapping_periods(self) -> None:
        raw, styles = sample_history()
        cutoff = raw["exchanges"]["SSE"][-1]["trade_date"]
        result = analyze_margin_leverage(raw, styles, cutoff=cutoff)
        end = dt.date.fromisoformat(cutoff)
        periods = []
        for index, pid in enumerate(("W-2", "W-1", "W0")):
            start = end - dt.timedelta(days=(2 - index) * 7 + 4)
            finish = start + dt.timedelta(days=4)
            periods.append({"period_id": pid, "start_date": start.isoformat(), "end_date": finish.isoformat(), "completeness": "complete"})
        path = build_three_week_margin(result, periods, raw, styles)
        self.assertEqual([row["period_id"] for row in path["periods"]], ["W-2", "W-1", "W0"])
        self.assertTrue(all(row["data_status"] == "ok" for row in path["periods"]))
        self.assertTrue(all(row["heat_score"] is not None for row in path["periods"]))
        historical_end = path["periods"][0]
        direct = analyze_margin_leverage(raw, styles, cutoff=periods[0]["end_date"])
        self.assertAlmostEqual(historical_end["heat_score"], direct["heat"]["score"])
        self.assertAlmostEqual(
            historical_end["deleveraging_pressure_score"],
            direct["deleveraging_pressure"]["score"],
        )

    def test_single_day_market_denominator_does_not_erase_margin_history(self) -> None:
        raw, styles = sample_history()
        for exchange in ("SSE", "SZSE"):
            raw["market_daily"][exchange] = raw["market_daily"][exchange][-1:]
        cutoff = raw["exchanges"]["SSE"][-1]["trade_date"]
        result = analyze_margin_leverage(raw, styles, cutoff=cutoff)
        self.assertEqual(len(result["series"]), 60)
        self.assertTrue(all(row.get("financing_balance") is not None for row in result["series"]))
        self.assertEqual(sum(row.get("financing_to_float_cap") is not None for row in result["series"]), 1)


class MarginCombinationTests(unittest.TestCase):
    def test_cache_deduplicates_same_day_across_providers(self) -> None:
        with tempfile.TemporaryDirectory() as directory, CacheStore(directory) as store:
            store.upsert_series("older", "sample", "X", [{"trade_date": "2026-07-16", "value": 1}])
            store.upsert_series("newer", "sample", "X", [{"trade_date": "2026-07-16", "value": 2}])
            rows = store.get_series("sample", "X")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["value"], 2)

    def test_combination_requires_same_date_for_sse_and_szse(self) -> None:
        combined = combine_exchanges({
            "SSE": [{"trade_date": "2026-07-16", "margin_balance": 100}],
            "SZSE": [{"trade_date": "2026-07-15", "margin_balance": 80}],
            "BSE": [{"trade_date": "2026-07-16", "margin_balance": 1}],
        }, ("margin_balance",))
        self.assertEqual(combined, [])

    def test_complete_cache_avoids_historical_network_calls(self) -> None:
        raw, _styles = sample_history(500)
        cutoff = raw["exchanges"]["SSE"][-1]["trade_date"]

        class NoNetworkClient:
            statuses: list[dict] = []

            def call(self, *args, **kwargs):
                raise AssertionError("complete cache should prevent historical API calls")

        with tempfile.TemporaryDirectory() as directory, CacheStore(directory) as store:
            for exchange in ("SSE", "SZSE"):
                store.upsert_series("fixture", "margin_summary", exchange, raw["exchanges"][exchange])
                store.upsert_series("fixture", "market_daily_info", exchange, raw["market_daily"][exchange])
            statuses: list[dict] = []
            result = collect_margin_leverage_data(
                NoNetworkClient(), None, {}, "akshare-only", store, statuses,
                {"end_date": cutoff}, "summary", [],
            )
        self.assertEqual(result["exchanges"]["SSE"][-1]["trade_date"], cutoff)
        self.assertEqual(result["market_daily"]["SZSE"][-1]["trade_date"], cutoff)

    def test_current_market_snapshot_cache_avoids_repeated_public_calls(self) -> None:
        raw, _styles = sample_history(500)
        cutoff = raw["exchanges"]["SSE"][-1]["trade_date"]

        class NoNetworkClient:
            statuses: list[dict] = []

            def call(self, *args, **kwargs):
                raise AssertionError("same-day public market snapshot should be reused")

        with tempfile.TemporaryDirectory() as directory, CacheStore(directory) as store:
            for exchange in ("SSE", "SZSE"):
                store.upsert_series("fixture", "margin_summary", exchange, raw["exchanges"][exchange])
                store.upsert_series("fixture", "market_daily_info", exchange, raw["market_daily"][exchange][-1:])
            statuses: list[dict] = []
            result = collect_margin_leverage_data(
                NoNetworkClient(), None, {}, "akshare-only", store, statuses,
                {"end_date": cutoff}, "summary", [],
            )
        self.assertEqual(len(result["market_daily"]["SSE"]), 1)
        market_status = next(row for row in statuses if row["dataset"] == "market_daily_info:SSE")
        self.assertEqual(market_status["history_coverage"], "current_snapshot_only")

    def test_concentration_uses_full_market_financing_as_denominator(self) -> None:
        raw, _styles = sample_history(500)
        cutoff = raw["exchanges"]["SSE"][-1]["trade_date"]

        class NoNetworkClient:
            statuses: list[dict] = []

            def call(self, *args, **kwargs):
                raise AssertionError("complete aggregate cache should prevent public calls")

        class DetailProxy:
            def __init__(self):
                self.statuses: list[dict] = []

            def call(self, dataset, function, params):
                self.statuses.append({"dataset": dataset, "function": function, "status": "ok", "record_count": 100})
                return [{"ts_code": f"{index:06d}.SZ", "rzye": 1_000_000_000} for index in range(100)]

        health = {"datasets": {"margin_concentration": {"promotion_eligible": True}}}
        with tempfile.TemporaryDirectory() as directory, CacheStore(directory) as store:
            for exchange in ("SSE", "SZSE"):
                store.upsert_series("fixture", "margin_summary", exchange, raw["exchanges"][exchange])
                store.upsert_series("fixture", "market_daily_info", exchange, raw["market_daily"][exchange])
            statuses: list[dict] = []
            result = collect_margin_leverage_data(
                NoNetworkClient(), DetailProxy(), health, "auto", store, statuses,
                {"end_date": cutoff}, "full", [],
            )
            cached = store.get_series("margin_concentration", "SSE+SZSE")
        market_total = sum(raw["exchanges"][exchange][-1]["financing_balance"] for exchange in ("SSE", "SZSE"))
        expected = 100 * 1_000_000_000 / market_total * 100
        self.assertAlmostEqual(result["concentration"]["top100_share"], expected)
        self.assertEqual(len(cached), 1)

    def test_margin_mode_does_not_change_fund_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            holdings = root / "holdings.json"
            holdings.write_text(json.dumps([
                {"code": "001170", "name": "测试基金甲", "amount": 10000},
                {"code": "004206", "name": "测试基金乙", "amount": 10000},
            ], ensure_ascii=False), encoding="utf-8")
            analyses = {}
            for mode in ("off", "summary"):
                raw = root / f"raw-{mode}.json"
                analysis = root / f"analysis-{mode}.json"
                with patch.object(sys, "argv", [
                    "collect_weekly_data.py", "--holdings", str(holdings), "--output", str(raw),
                    "--mock", "--end-date", "2026-07-16", "--margin-mode", mode,
                ]):
                    weekly_collector.main()
                with patch.object(sys, "argv", [
                    "analyze_weekly.py", "--holdings", str(holdings), "--weekly-data", str(raw),
                    "--output", str(analysis), "--cache-root", str(root / "cache"),
                ]):
                    weekly_analyzer.main()
                analyses[mode] = json.loads(analysis.read_text(encoding="utf-8"))
            for key in ("weekly_score", "decision_action", "target_weight", "first_step_target_weight"):
                left = [(row.get("code"), row.get(key)) for row in analyses["off"]["portfolio"]["funds"]]
                right = [(row.get("code"), row.get(key)) for row in analyses["summary"]["portfolio"]["funds"]]
                self.assertEqual(left, right)
            self.assertEqual(analyses["off"]["comparison"]["replacement_top3"], analyses["summary"]["comparison"]["replacement_top3"])
            evidence_keys = (analyses["summary"]["three_week_analysis"].get("evidence_index") or {}).keys()
            self.assertTrue(any(key.startswith("margin:SSE+SZSE:H") for key in evidence_keys))


class MarginCalibrationTests(unittest.TestCase):
    def test_walk_forward_calibration_reserves_future_outcome_window(self) -> None:
        start = dt.date(2020, 1, 1)
        rows = []
        indexes = []
        for index in range(720):
            day = (start + dt.timedelta(days=index)).isoformat()
            rows.append({
                "trade_date": day,
                "financing_balance": 10_000 + index * (1 + index / 5000),
                "financing_to_float_cap": 2 + index / 1000,
                "financing_buy_to_turnover": 6 + (index % 80) / 100,
                "market_turnover": 5000 + (index % 40) * 10,
            })
            indexes.append({"日期": day, "收盘": 1000 + index * 0.5})
        result = calibrate(rows, indexes, rows[0]["trade_date"])
        self.assertEqual(result["status"], "ok")
        self.assertLessEqual(result["end_date"], rows[-61]["trade_date"])
        self.assertTrue(result["evidence_hash"])
        for row in result["heat_bands"] + result["pressure_bands"]:
            if row["sample_count"] < 30:
                self.assertTrue(all(value is None for key, value in row.items() if key.endswith("_rate")))


if __name__ == "__main__":
    unittest.main()
