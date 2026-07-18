---
name: fund-rotation-analyst
description: Analyze Chinese public fund portfolios with AkShare, optional official Tushare Pro, and guarded public fallbacks, including fund rankings, sector flows, style rotation, A-share margin-leverage conditions, ETF trading quality, and target allocation suggestions. Use when the user asks to review fund holdings, compare market styles, inspect hot sectors and capital flows, evaluate market leverage, analyze top-performing funds, create recurring reports, or propose fund-level rebalancing ratios.
---

# Fund Rotation Analyst

## Overview

Use this skill to produce Chinese visual HTML and Markdown reports for fund portfolio review and active rotation suggestions. The Python CLI is agent-independent even though this `SKILL.md` is developed and validated in Codex. Use AkShare and public fallbacks by default; official Tushare Pro is the preferred authenticated enhancement, while a third-party compatibility proxy is optional and must be isolated. Promote either source per dataset only after health and consistency checks. Do not connect to broker accounts, place orders, or imply guaranteed returns.

## Workflow

1. Normalize the user's holdings into JSON records with:
   - `code`: six-digit fund code.
   - `name`: fund name when provided.
   - `amount`: current market value or holding amount.
   - `cost`: optional invested cost.
   - `current_weight`: optional current portfolio weight.
   - `is_core`: optional boolean for long-term core holdings.
   - `tags`: optional user labels such as `科技`, `医药`, `QDII`, `债基`.
2. If this is a recurring report, reuse the latest normalized holdings snapshot unless the user provides a new one.
3. Run the collector with `auto`, `shadow`, or `akshare-only` provider routing. If live calls fail, keep partial results and warnings.
4. Run `scripts/analyze_portfolio.py` to compute returns, style/sector exposure, rankings, and target weights.
5. Run `scripts/render_report.py` to produce the final Markdown report.
6. In the answer, clearly state data time, unavailable data, and that suggestions are fund-level analysis only.

## Commands

Create a holdings JSON file, then run:

```bash
python scripts/collect_fund_data.py --holdings holdings.json --output market_data.json
python scripts/analyze_portfolio.py --holdings holdings.json --market-data market_data.json --output analysis.json
python scripts/render_report.py --analysis analysis.json --output report.md
python scripts/render_visual_report.py --analysis analysis.json --output report.html
```

For weekly reviews, default to a three-week rotation report and shared incremental cache. Do not stop at a Markdown-only summary:

```bash
python scripts/collect_weekly_data.py --holdings holdings.json --output weekly_market_data.json --mode quick --history-weeks 3 --margin-mode summary --cache-root work/cache/fund-rotation
python scripts/analyze_weekly.py --holdings holdings.json --weekly-data weekly_market_data.json --output weekly_analysis.json --history-weeks 3 --cache-root work/cache/fund-rotation --llm-evidence-output weekly_llm_evidence.json
# An Agent may create evidence-bound weekly_llm_synthesis.json; pure CLI keeps deterministic synthesis.
python scripts/finalize_weekly_analysis.py --analysis weekly_analysis.json --evidence weekly_llm_evidence.json --synthesis weekly_llm_synthesis.json --output weekly_analysis.json
python scripts/render_weekly_report.py --analysis weekly_analysis.json --output weekly_report.md
python scripts/render_weekly_visual_report.py --weekly-data weekly_analysis.json --output weekly_report.html
python scripts/validate_report.py --analysis weekly_analysis.json --html weekly_report.html --require-complete
```

Weekly artifacts use `schema_version: 2`, current `data_revision: 2.8`, and visual format `weekly-visual-v2`. Treat the `--holdings` file passed to analysis as authoritative. Default periods are W0 current/latest available week, W-1 last complete week, and W-2 the preceding complete week. W0 is marked `进行中` when incomplete and cannot independently trigger a formal action. Use `--end-date YYYY-MM-DD` only for an explicit cutoff.

The weekly HTML format is a stable product contract, not a best-effort template. Preserve all mandatory sections, their order, labeled units, navigation, mobile layout, print layout, provenance fingerprints, and explicit degraded states. Read `references/visual_report_contract.md` before changing any weekly renderer or validator. A data-model revision does not by itself justify a visual-format version change.

The shared cache is `work/cache/fund-rotation/cache.sqlite3`. Historical series are incrementally updated and remain independent from report/model versions. Use `--refresh-dataset DATASET` for targeted refresh; do not create report-version cache directories.
Use the default `auto` provider policy for every normal user-facing report. `akshare-only` is a diagnostic isolation mode: do not publish its output as a complete report when `auto` can recover validated closed-day sector history from SQLite. A new module must not reduce the row coverage of an existing mandatory section; `--require-complete` enforces this delivery gate.
Previously validated closed-day sector history may be reused from SQLite even when the live proxy endpoint is currently slow or unavailable, but only when every requested trading date is present and the provider taxonomy remains unchanged. This cache recovery is `cached_validated_history`; an incomplete range cannot bypass source-health gating.
`akshare-only` remains strict and does not use proxy-derived cached rows. Use `auto` when validated closed-day proxy history is allowed to recover industry/concept flows without a live proxy runtime.

The default `--margin-mode summary` adds the informational A-share leverage module. `off` preserves the old collection path, while `full` also requests individual-stock concentration evidence. The module is always `display_only`: it must not change fund scores, Top3, target weights, or actions. Calibrate historical heat/pressure bands separately rather than during every report:

```bash
python scripts/calibrate_margin_model.py --cache-root work/cache/fund-rotation --start-date 2014-09-22 --output work/cache/fund-rotation/margin_calibration_v1.json
```

If current financing density/intensity is available but the historical percentile is missing, bootstrap the exchange denominator once and rerun collection and analysis:

```bash
python scripts/backfill_margin_market_history.py --cache-root work/cache/fund-rotation --end-date YYYY-MM-DD --sessions 650
```

The report must explain that leverage heat is a level indicator with no standalone good/bad direction, while deleveraging pressure is generally calmer when lower and riskier when higher. Neither one is a deterministic return signal.

## Optional Tushare Sources

For official Tushare Pro, obtain the token from `tushare.pro`, set `TUSHARE_PROVIDER=official`, leave `TUSHARE_HTTP_URL` unset, and use the official SDK without overriding private fields:

```python
import os
import tushare as ts

pro = ts.pro_api(os.environ["TUSHARE_TOKEN"])
d1 = pro.index_basic(limit=5)
d2 = ts.pro_bar(api=pro, ts_code="000001.SZ", limit=3)
```

Official Tushare Pro is recommended for formal, recurring, or unattended use because account permissions, points, terms, and support are directly verifiable. A proxy-purchased card is a proxy-issued credential, not an official Pro token.

Only when the user explicitly chooses the approved compatibility proxy, set `TUSHARE_PROVIDER=third-party-proxy`, use the proxy-issued credential, and let `scripts/tushare_proxy.py` apply the fixed private endpoint override. Never send an official Tushare Pro token to a third-party endpoint. Label proxy data `第三方 Tushare 兼容代理`, not `Tushare Pro 官方`.

Never place either token in code, Skill files, caches, logs, commands, or reports. Replace any credential exposed in chat before unattended automation.

Install the pinned SDK, export credentials in the shell, and run isolated health checks before enabling `auto`:

```bash
python scripts/smoke_test_tushare.py --provider official --rounds 3 --timeout 15 --group all --output work/tushare_health.json
python scripts/collect_weekly_data.py --holdings holdings.json --output weekly_shadow.json --provider-policy shadow --tushare-health work/tushare_health.json
python scripts/validate_tushare_shadow.py --health work/tushare_health.json --shadow shadow_day1.json --shadow shadow_day2.json --shadow shadow_day3.json --output work/tushare_health_promoted.json
```

`shadow` never changes report conclusions. Price/NAV/index datasets remain non-promotable until three distinct shadow trading dates pass cross-source checks; quarterly holdings use a public-report Top10 check. `auto` reads the promoted health file and uses only datasets with `promotion_eligible=true`; `akshare-only` disables the proxy. ETF adjustment must use `fund_daily + fund_adj`. Never treat `pro_bar(..., asset="FD")` adjustment as ETF-adjustment evidence. Aggregate sector flow from the supplied `net_amount`; do not infer it from buy/sell fields.

Historical checkpoint replay may be run with `scripts/backfill_tushare_shadow.py` to test cutoff handling and cross-source arithmetic. It is provisional evidence only: it records `provisional_promotion_eligible`, never `promotion_eligible`, and cannot replace three shadow files collected on three distinct live trading dates. Promotion is granular by fund code, index symbol, and ETF code; a failed symbol such as a stale or empty index must not block independently verified symbols.

For Hong Kong Stock Connect ETF availability checks:

```bash
python scripts/check_stock_connect_etfs.py --theme semiconductor --output stock_connect_semiconductor_etfs.json
```

For dry runs or interface tests without network access:

```bash
python scripts/collect_fund_data.py --holdings holdings.json --output market_data.json --mock
```

Before a live report, or when data gaps increase unexpectedly, run the repeatable interface health check:

```bash
python scripts/smoke_test_interfaces.py --output interface_smoke.json --rounds 2 --timeout 12 --end-date YYYY-MM-DD
```

Do not classify a source as reliable from one successful call. Retest unstable primary and compatibility sources separately with `--interface` or `--group`; retain the per-attempt errors and timings as audit evidence.

## Report Requirements

Always include these sections when data is available:

- 持仓概览与近期表现
- 本周复盘: 当用户问“本周表现”“这周基金怎么样”时，必须输出 HTML 三周可视化报告；至少包含 W0/W-1/W-2 组合轨迹、风格变化、A股杠杆温度、行业/概念收益与自然周资金流、轮动状态、板块Top10、重点候选 ETF 折溢价/成交额、Top3门控、缓存审计和数据缺口。Markdown 可作为补充，不能作为唯一交付物。
- 大盘与风格判断: 成长、价值、大盘、小盘、红利、科技、消费、医药、新能源
- 行业/概念资金流入流出
- 收益排行 Top 30 特征分析: 默认完整列出近1月综合业绩分前30；综合业绩分 = 0.70 × 近1月收益 + 0.30 × (近3月收益 / 3)。展示主题、主题依据/置信度、产品属性、是否被动指数/ETF联接、主动基金规模与换手线索、是否当前持有、与当前持仓差异、候选池排序、候选类型说明和推荐替换Top3。
- 香港 Stock Connect 可买性分析: 当用户问“香港能不能买”“沪股通/深股通能不能买 ETF/指数基金”时，必须区分指数、场外联接基金、场内 ETF，并用 HKEX 北向 eligible securities 清单核验。不要只查一个代码或用错误编码搜索 CSV。
- 调仓建议与目标配比
- 风险提示与数据缺口

For listed ETFs, especially hot sector ETFs, report trading quality together with performance: latest price, IOPV, premium/discount, turnover, update time, and whether the return calculation used adjusted prices or NAV. Never treat unadjusted ETF price discontinuities caused by splits/conversions as real investment losses.

For ETF multi-period performance, prefer cumulative NAV over unit NAV when adjusted price is unavailable. Unit NAV is only for same-day closing-price/NAV premium. If unit NAV jumps but cumulative NAV remains continuous, mark `份额折算` and use cumulative NAV. Distinguish `实时IOPV溢价` from `收盘净值溢价` in every report.

Separate ETF report-end evidence from the current trading snapshot. Report-end close, turnover, same-day unit NAV, and closing premium determine `recommendation_eligible`; a live quote no older than five minutes plus live IOPV determines `execution_ready`. Missing live data may block execution readiness but is optional and must not erase a valid closing score. Use proxy `rt_etf_k` for batched Shanghai/Shenzhen quotes after health promotion; Shanghai requests require `topic="HQ_FND_TICK"`. Proxy `rt_etf_sz_iopv` is Shenzhen-only. The lack of Shanghai IOPV is `not_required`, not a missing dataset.

Treat failures as logical dataset chains. A failed primary source followed by a successful fallback is `fallback_used`, not a visible data gap. Keep source-level failures in the collapsible audit; show only unresolved datasets in the user-facing warning list.
Every logical status must include `requirement` and `impact`. Only required `failed/partial` datasets count as unresolved. An empty dependency set is `not_required`; unavailable intraday-only data is `optional_unavailable`.

Weekly quick mode must reuse cached fund profiles for current holdings and ranking candidates. Full mode refreshes public scale, turnover, quarterly holdings, and industry allocation. Basic/scale/turnover evidence has a 30-day TTL, quarterly holdings a 90-day TTL, and evidence older than 180 days is unusable.

Classify `LOF` by its declared fund type or benchmark; `LOF` alone does not mean passive index. For active-fund theme evidence, use only the latest disclosed holdings/industry period. Name-only themes are low confidence and cannot make an active fund actionable.

Keep `week.end_date` separate from `week.collection_trade_date`. Rolling 5-day/10-day board data can represent the target week only when its source date equals the report end date. A later current-day snapshot may be shown separately with its real date, but it must not influence the completed-week score.
Store the report-end single-day flow separately from the collection-day `今日` snapshot; never overwrite one with the other.

Label the single-period sector Top10 as `近5个交易日收益`, because it is a rolling five-session observation ending on the report cutoff. Do not call it a natural-week return. The W0/W-1/W-2 sector matrices are the source of truth for non-overlapping natural trading weeks. A sector enters a rolling 5-day or 10-day table only when it contains every expected market trading date in that window; never reach back to an older date to conceal a missing middle session.

Never fill missing weekly sector returns with daily snapshots or fund-name proxies. Show daily board moves and fund-ranking theme signals in separate, explicitly labeled sections.
For concepts, keep `concept_latest_close` separate from `concept_intraday`. A report-end Tushare `moneyflow_cnt_ths` row may supply the latest close, index level, return, and flow under the 同花顺 taxonomy. Populate the legacy `concept_today` field only when its source date equals the collection trading date.

Never fabricate three replacement recommendations. A candidate must have an auditable return basis, sufficient score coverage, real sector/trend evidence, a score gap of at least 5, acceptable liquidity, and premium below 2%. If fewer than three candidates pass, emit `replacement_status: insufficient_evidence` and show the reason.
An ETF premium must be positively confirmed and date-labeled; an unknown premium is observation-only even when its score and liquidity are strong.

Weekly conclusions must be generated from the structured analysis model, not assembled ad hoc in HTML. Separate positive-return leaders, sectors confirmed by fund-flow persistence, portfolio coverage, duplicate exposure, and recommendation blockers. Translate internal states into Chinese in user-facing reports.

Three-week calculations, period boundaries, ranks, rotation states, scores, actions, and weights are deterministic. Codex may explain them only through `weekly_llm_evidence.json`; every main conclusion needs at least two period references. The LLM cannot invent entities or numbers, change actions/weights, or use an incomplete W0 as the sole action trigger. Validate the merge with `finalize_weekly_analysis.py`; if it fails, retain deterministic synthesis.

A-share leverage interpretation uses total margin balance for display and financing balance for scoring. Always show leverage heat and deleveraging pressure together. Low leverage is not an upside-space signal, high leverage is not a deterministic market top, and balance changes are not standalone buy/sell signals. SSE and SZSE must share the same trade date before aggregation; BSE remains display-only. Read `references/margin_leverage.md` before changing this module.
Margin percentiles must use trailing history only, excluding the scored observation. The three-week leverage table recomputes heat and pressure at each period end, and the visual block must show 60-session tracks for two融余额、融资杠杆密度 and the selected broad-index proxy.

When W0 is incomplete, display two separate cumulative results: `截至当前复合` may include W0 for monitoring, while `完整周复合` excludes W0 and is the only cumulative return eligible for action evidence. A confirmed `退潮/持续流出` sector that becomes `进行中修复观察` must not be counted as a current fading sector until the week closes.

Map disclosed fund themes to sectors with explicit direct/indirect aliases. Show matched holdings, estimated covered weight, and the disclosure-based caveat; do not infer coverage from an exact full-theme string match. Three-week heatmaps must use the union of each week's return Top5, flow Top5, and portfolio-related sectors, capped deterministically at 15, so an old leader or a held exposure is not hidden by the latest-week ranking.

Every holding percentage must carry a field label. Explain whether `当前组合占比` comes from real amounts, user weights, or an equal-weight assumption; explain that one-year maximum drawdown is the largest peak-to-trough decline and that a more negative value indicates higher historical downside risk.

Reject non-empty but stale index fallbacks. For 中证红利 `000922`, use `index_zh_a_hist`, then `stock_zh_index_hist_csindex`, then Tencent, and only accept Sina history if it covers the requested baseline and end dates. Use daily industry flow history to supplement the single-day, 5-day, and 10-day evidence for the union of industry Top10 lists. Classify sector names with `references/sector_taxonomy.json`; `油田服务` must map to `资源能源/油气产业链/油服`.

For weekly sector analysis, always include industry/concept return leaders, industry/concept inflow leaders, outflow leaders, flow persistence labels, current portfolio coverage, and candidate fund/ETF implications when data is available.
Complete delivery requires all six rolling sector lists separately: industry/concept returns, industry inflow/outflow, and concept inflow/outflow. A populated inflow list must not mask a missing outflow list, or vice versa.

Use direct allocation labels for target-weight tables: `观察`, `小幅增配`, `增配`, `减配`, `清仓候选`.

Use decision labels for Top 30 comparison tables: `保留核心`, `保留但不加`, `减配去重`, `替换候选`, `观察`. Do not recommend buying Top 30 funds mechanically; first decide whether the current portfolio already covers the rewarded theme.

## Risk Rules

- Use an active-rotation style, but keep portfolio constraints.
- Default single fund target cap: 25%.
- Default single theme aggregate cap: 40%.
- Default one-period adjustment cap for high-volatility themes: 10% of the portfolio.
- Preserve core holdings unless performance, drawdown, duplication, or sector flow strongly argues for reduction.
- Prefer reducing duplicated exposure, weak trends, large drawdowns, persistent outflows, or high-fee underperformers.
- Never output auto-trading instructions or claim investment certainty.
- Suppress formal portfolio weekly return when valid NAV coverage is below 90%; show only the covered-weight partial estimate.
- Validate all schema-v2 weekly JSON and HTML artifacts before delivery. A title-only or empty report is not valid.

## References

- Read `references/data_sources.md` when changing data collection, AkShare interfaces, or fallback behavior.
- Read `references/scoring_model.md` when changing scoring, rotation, allocation, or report interpretation.
- Read `references/weekly_review.md` when answering weekly performance, 本周表现, 近1周复盘, or ETF weekly performance questions.
- Read `references/visual_report_contract.md` when changing report sections, layout, responsive behavior, print behavior, empty states, or validation.
- Read `references/margin_leverage.md` when changing margin data, normalization, heat/pressure scoring, historical calibration, or leverage language.
- Read `references/stock_connect_etf_access.md` when answering Hong Kong Stock Connect, Northbound ETF, 沪股通/深股通, ETF Connect, or “从香港可以买哪些指数/ETF” questions.
