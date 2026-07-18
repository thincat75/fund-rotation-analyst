from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import tushare_proxy as proxy  # noqa: E402
import smoke_test_tushare_proxy as health_tool  # noqa: E402
import validate_tushare_shadow as shadow_tool  # noqa: E402


class FakePro:
    pass


class FakeFrame:
    empty = False

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def to_dict(self, orient: str) -> list[dict[str, object]]:
        assert orient == "records"
        return self.rows


class FakeTs:
    __version__ = "test-version"

    def __init__(self) -> None:
        self.token = None
        self.pro = FakePro()
        self.bar_api = None

    def pro_api(self, token: str) -> FakePro:
        self.token = token
        return self.pro

    def pro_bar(self, *, api: FakePro, **_kwargs: object) -> FakeFrame:
        self.bar_api = api
        return FakeFrame([{"trade_date": "20260710", "close": 10}])


class TushareInitializationTests(unittest.TestCase):
    def test_factory_uses_required_private_endpoint_assignment(self) -> None:
        ts = FakeTs()
        pro, module, metadata = proxy.create_pro("secret", proxy.DEFAULT_HTTP_URL, ts_module=ts)
        self.assertIs(module, ts)
        self.assertEqual(ts.token, "secret")
        self.assertEqual(pro._DataApi__http_url, proxy.DEFAULT_HTTP_URL)
        self.assertNotIn("secret", str(metadata))
        self.assertEqual(metadata["provider"], "第三方 Tushare 代理")

    def test_unapproved_endpoint_is_rejected(self) -> None:
        with self.assertRaises(proxy.ProxyConfigurationError):
            proxy.create_pro("secret", "https://example.com", ts_module=FakeTs())

    def test_pro_bar_explicitly_receives_api(self) -> None:
        ts = FakeTs()
        pro, _, metadata = proxy.create_pro("secret", proxy.DEFAULT_HTTP_URL, ts_module=ts)
        with tempfile.TemporaryDirectory() as temp_dir:
            client = proxy.TushareProxyClient(pro, ts, Path(temp_dir), metadata)
            client.pro_bar("sample", {"ts_code": "000001.SZ", "limit": 3})
        self.assertIs(ts.bar_api, pro)


class TushareSemanticsTests(unittest.TestCase):
    def test_fund_code_is_resolved_from_master_data(self) -> None:
        rows = [{"ts_code": "001170.OF"}, {"ts_code": "560780.SH"}]
        self.assertEqual(proxy.resolve_fund_ts_code(rows, "001170"), "001170.OF")
        self.assertIsNone(proxy.resolve_fund_ts_code(rows, "999999"))

    def test_adjusted_nav_precedes_accumulated_and_unit_nav(self) -> None:
        rows = [{"nav_date": "20260710", "adj_nav": 3.0, "accum_nav": 2.0, "unit_nav": 1.0}]
        normalized = proxy.normalize_fund_nav(rows)
        self.assertEqual(normalized[0]["分析净值"], 3.0)
        self.assertEqual(normalized[0]["nav_basis"], "adj_nav")

    def test_portfolio_rejects_future_announcement_and_uses_latest_period(self) -> None:
        rows = [
            {"end_date": "20260331", "ann_date": "20260420", "symbol": "A", "stk_mkv_ratio": 5},
            {"end_date": "20260630", "ann_date": "20260720", "symbol": "B", "stk_mkv_ratio": 9},
            {"end_date": "20251231", "ann_date": "20260120", "symbol": "C", "stk_mkv_ratio": 7},
        ]
        selected = proxy.normalize_fund_portfolio(rows, "2026-07-10")
        self.assertEqual([row["symbol"] for row in selected], ["A"])

    def test_etf_adjustment_uses_fund_daily_and_fund_adj(self) -> None:
        daily = [{"trade_date": "20260703", "close": 1.0, "amount": 1000}, {"trade_date": "20260710", "close": 0.5, "amount": 2500}]
        factors = [{"trade_date": "20260703", "adj_factor": 1}, {"trade_date": "20260710", "adj_factor": 2}]
        adjusted = proxy.adjusted_etf_history(daily, factors, mode="hfq")
        self.assertEqual([row["收盘"] for row in adjusted], [1.0, 1.0])
        self.assertTrue(all(row["return_basis"] == "fund_daily+fund_adj" for row in adjusted))
        self.assertEqual([row["成交额"] for row in adjusted], [1_000_000, 2_500_000])

    def test_optional_etf_specs_use_official_interfaces(self) -> None:
        specs = health_tool._specs("optional", __import__("datetime").date(2026, 7, 17))
        by_dataset = {row["dataset"]: row for row in specs}
        self.assertEqual(by_dataset["etf_iopv"]["api"], "rt_etf_sz_iopv")
        self.assertEqual(by_dataset["etf_realtime_daily"]["kwargs"]["topic"], "HQ_FND_TICK")

    def test_sector_flow_uses_net_amount_and_compounds_pct_change(self) -> None:
        rows = [
            {"trade_date": "20260703", "name": "AI", "industry_index": None, "pct_change": 1, "net_amount": 10, "net_buy_amount": 999, "net_sell_amount": 1},
            {"trade_date": "20260710", "name": "AI", "industry_index": None, "pct_change": 2, "net_amount": -4, "net_buy_amount": 999, "net_sell_amount": 1},
        ]
        result = proxy.aggregate_sector_flow(rows, period=2, end_date="2026-07-10", sector_type="概念资金流")[0]
        self.assertEqual(result["2日主力净流入-净额"], 600_000_000)
        self.assertEqual(result["资金单位"], "元")
        self.assertEqual(result["source_date"], "2026-07-10")
        self.assertAlmostEqual(result["2日涨跌幅"], 3.02)

    def test_sector_flow_rejects_partial_period(self) -> None:
        rows = [
            {"trade_date": f"2026070{day}", "industry": "半导体", "close": 100 + day, "net_amount": day}
            for day in range(1, 5)
        ]
        self.assertEqual(
            proxy.aggregate_sector_flow(rows, period=5, end_date="2026-07-10", sector_type="行业资金流"),
            [],
        )

    def test_sector_flow_does_not_backfill_a_missing_middle_day(self) -> None:
        rows = []
        for day in (3, 6, 7, 8, 9, 10):
            compact = f"202607{day:02d}"
            rows.append({"trade_date": compact, "industry": "完整板块", "close": 100 + day, "net_amount": 1})
            if day != 8:
                rows.append({"trade_date": compact, "industry": "缺失板块", "close": 100 + day, "net_amount": 1})
        result = proxy.aggregate_sector_flow(rows, period=5, end_date="2026-07-10", sector_type="行业资金流")
        self.assertEqual([row["名称"] for row in result], ["完整板块"])

    def test_proxy_cache_is_shared_across_quick_and_full_contexts(self) -> None:
        ts = FakeTs()
        pro, _, metadata = proxy.create_pro("secret", proxy.DEFAULT_HTTP_URL, ts_module=ts)
        with tempfile.TemporaryDirectory() as temp_dir:
            quick = proxy.TushareProxyClient(pro, ts, Path(temp_dir), metadata, context={"mode": "quick"})
            full = proxy.TushareProxyClient(pro, ts, Path(temp_dir), metadata, context={"mode": "full"})
            kwargs = {"start_date": "20260701", "end_date": "20260710"}
            self.assertEqual(quick._cache_path("moneyflow_ind_ths", kwargs), full._cache_path("moneyflow_ind_ths", kwargs))

    def test_fund_master_uses_compact_persistent_index(self) -> None:
        class FakeClient:
            def __init__(self, cache_dir: Path) -> None:
                self.cache_dir = cache_dir
                self.refresh = False
                self.statuses: list[dict[str, object]] = []
                self.metadata = {"endpoint_fingerprint": "test"}
                self.calls = 0

            def call(self, dataset: str, name: str, kwargs: dict[str, object]) -> list[dict[str, object]]:
                self.calls += 1
                rows = [{"ts_code": "001170.OF", "name": "测试基金"}]
                self.statuses.append({"dataset": dataset, "status": "ok", "record_count": 1, "cache_hit": False})
                return rows

        with tempfile.TemporaryDirectory() as temp_dir:
            first = FakeClient(Path(temp_dir))
            self.assertEqual(proxy.collect_fund_master(first, ["001170"])[0]["ts_code"], "001170.OF")
            self.assertEqual(first.calls, 1)
            self.assertEqual(len(first.statuses), 1)
            self.assertEqual(first.statuses[0]["function"], "fund_basic_paginated")
            second = FakeClient(Path(temp_dir))
            self.assertEqual(proxy.collect_fund_master(second, ["001170"])[0]["ts_code"], "001170.OF")
            self.assertEqual(second.calls, 0)

    def test_slow_consistent_flow_is_background_eligible(self) -> None:
        spec = {"api": "moneyflow_ind_ths"}
        attempts = [{
            "status": "ok", "latency_ms": latency, "row_count": 10,
            "fields": ["trade_date", "industry", "net_amount"], "latest_date": "20260710",
            "content_fingerprint": "same", "flow_numeric_coverage": 1.0,
            "flow_duplicate_keys": 0, "flow_max_abs_net_amount": 500,
            "flow_positive_count": 5, "flow_negative_count": 5,
        } for latency in [6500, 7000, 9000]]
        summary = health_tool.summarize("industry_flow", spec, attempts, 3)
        self.assertTrue(summary["operational_eligible"])
        self.assertTrue(summary["promotion_eligible"])
        self.assertFalse(summary["quick_eligible"])
        self.assertEqual(summary["usage_scope"], "cached/background")

    def test_inconsistent_flow_content_is_not_promoted(self) -> None:
        spec = {"api": "moneyflow_cnt_ths"}
        attempts = [{
            "status": "ok", "latency_ms": 1000, "row_count": 10,
            "fields": ["trade_date", "name", "net_amount"], "latest_date": "20260710",
            "content_fingerprint": fingerprint, "flow_numeric_coverage": 1.0,
            "flow_duplicate_keys": 0, "flow_max_abs_net_amount": 500,
            "flow_positive_count": 5, "flow_negative_count": 5,
        } for fingerprint in ["a", "b", "a"]]
        summary = health_tool.summarize("concept_flow", spec, attempts, 3)
        self.assertFalse(summary["operational_eligible"])
        self.assertFalse(summary["promotion_eligible"])
        self.assertEqual(summary["consistency_status"], "failed")

    def test_promotion_is_dataset_specific(self) -> None:
        health = {"datasets": {"fund_nav": {"promotion_eligible": True}, "fund_portfolio": {"promotion_eligible": False}}}
        self.assertTrue(proxy.promotion_eligible(health, "fund_nav"))
        self.assertFalse(proxy.promotion_eligible(health, "fund_portfolio"))

    def test_health_requires_shadow_for_price_datasets(self) -> None:
        spec = {"api": "fund_nav"}
        attempts = [{"status": "ok", "latency_ms": 100, "row_count": 5, "fields": ["nav_date", "adj_nav"], "latest_date": "20260710"}] * 3
        summary = health_tool.summarize("fund_nav", spec, attempts, 3)
        self.assertTrue(summary["operational_eligible"])
        self.assertFalse(summary["promotion_eligible"])
        self.assertEqual(summary["crosscheck_status"], "pending_shadow_crosscheck")

    def test_three_day_shadow_can_promote_nav_after_crosscheck(self) -> None:
        shadows = []
        for day in ["2026-07-08", "2026-07-09", "2026-07-10"]:
            shadows.append({
                "as_of": day,
                "week": {"collection_trade_date": day},
                "provider_shadow": {"provider": "第三方 Tushare 代理", "datasets": {
                    "fund_nav:001170": [{"净值日期": day, "分析净值": 2.0, "累计净值": 2.001}],
                }},
                "funds": {"001170": {"nav": [{"净值日期": day, "累计净值": 2.001}]}},
            })
        checks = shadow_tool.collect_checks(shadows)
        health = {"datasets": {"fund_nav": {"operational_eligible": True, "required_for_foundation": True}}}
        promoted = shadow_tool.promote(health, checks)
        self.assertTrue(promoted["datasets"]["fund_nav"]["promotion_eligible"])

    def test_three_day_shadow_promotes_margin_datasets_per_exchange(self) -> None:
        shadows = []
        for day in ("2026-07-14", "2026-07-15", "2026-07-16"):
            proxy_rows = {}
            old_margin = {}
            old_market = {}
            for exchange, factor in (("SSE", 1.0), ("SZSE", 0.8)):
                proxy_rows[f"margin_summary:{exchange}"] = [{"trade_date": day, "margin_balance": 1000 * factor}]
                proxy_rows[f"market_daily_info:{exchange}"] = [{"trade_date": day, "float_market_cap": 50000 * factor, "market_turnover": 500 * factor}]
                old_margin[exchange] = [{"trade_date": day, "margin_balance": 1000 * factor}]
                old_market[exchange] = [{"trade_date": day, "float_market_cap": 50000 * factor, "market_turnover": 500 * factor}]
            shadows.append({
                "week": {"end_date": day, "collection_trade_date": day},
                "provider_shadow": {"provider": "第三方 Tushare 代理", "datasets": proxy_rows},
                "market": {"margin_raw": {"exchanges": old_margin, "market_daily": old_market}},
            })
        health = {"provider": "第三方 Tushare 代理", "transport": "http", "datasets": {
            "margin_summary": {"operational_eligible": True, "required_for_foundation": True},
            "market_daily_info": {"operational_eligible": True, "required_for_foundation": True},
        }}
        promoted = shadow_tool.promote(health, shadow_tool.collect_checks(shadows))
        self.assertTrue(promoted["datasets"]["margin_summary"]["promotion_eligible"])
        self.assertTrue(promoted["datasets"]["margin_summary:SSE"]["promotion_eligible"])
        self.assertTrue(promoted["datasets"]["market_daily_info:SZSE"]["promotion_eligible"])


if __name__ == "__main__":
    unittest.main()
