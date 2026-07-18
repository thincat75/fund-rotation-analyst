# Weekly Fund Review

Use this reference when the user asks for 本周表现, 近1周复盘, weekly review, or the weekly performance of candidate ETFs.

## Product Standard

Weekly reviews are decision reports, not raw tables. Always produce:

- A concise chat summary with the main conclusion.
- A structured JSON data artifact when running scripts.
- A visual HTML report for user-facing delivery.
- A Markdown report only as a secondary artifact.

The default review window is three non-overlapping A-share trading weeks: W0 is the latest/current week, W-1 and W-2 are complete weeks. Label W0 `进行中` when it has not ended. W0 can raise a continuation, divergence, or turn-weak alert, but cannot independently cause a formal allocation action.

The visual report must include:

- Equal-weight or user-weighted portfolio weekly return.
- Holding leaderboard: best, worst, weekly return, week range, latest NAV date.
- Market style comparison: large/small cap, growth/value, STAR/ChiNext where available.
- Sector Top10 analysis: industry return leaders, concept return leaders, inflow leaders, outflow leaders, flow persistence, and portfolio coverage.
- Recent winner evidence: top 1-week funds and whether they confirm a theme.
- Candidate ETF panel: separate report-end close/turnover/NAV premium from the current price/IOPV snapshot, with both timestamps and eligibility states.
- Top3 replacement/observation suggestions with first-step weight.
- Clear data gaps and fallback sources.
- Three-week portfolio/fund trajectories, style rank changes, industry/concept return and natural-week flow heatmaps, confirmed rotation state, W0 monitor state, evidence-bound synthesis, and cache audit.

## Three-Week Rotation

- Compare weekly flows as non-overlapping natural-trading-week sums. Also store daily average for short weeks; do not compare overlapping rolling 10-day totals as three independent observations.
- Keep the single-period Top10 separate from natural-week evidence. Its title is `近5个交易日收益`, and it must use the exact latest five market trading dates ending on the report cutoff. If a sector is missing any target date, omit it and record the coverage issue; never borrow an older observation to fill the window. Natural-week conclusions come from the W0/W-1/W-2 matrices.
- Require two complete valid weeks for `持续主线`, `加速`, `新启动`, `退潮`, or `持续流出`. Missing return, flow, or trading-day coverage makes that entity-week invalid.
- Keep the confirmed state separate from W0 monitoring. Example: `加速 / 进行中转弱预警` means the two complete weeks confirmed acceleration, while current incomplete-week return and flow have both turned negative.
- Use market percentiles and rank change rather than raw flow multiples as the core acceleration rule.
- Historical fund rankings require a dated snapshot whose `as_of_date <= period.end_date`. Never reuse a later snapshot to reconstruct an old weekly ranking.
- If W0 is partial, report both the compound return through W0 and the compound return of complete weeks. Label the first as monitoring only; formal actions use the latter.
- A confirmed retreat that changes to `进行中修复观察` is a recovery watch, not a current fading-sector count. The confirmed state remains visible for audit.
- Heatmap rows are the union of each week's return Top5, flow Top5, and disclosed-portfolio-related sectors, capped at 15 with a deterministic priority. HTML and Markdown use the same row-selection function.
- Portfolio coverage is derived from disclosed fund themes through explicit direct/indirect aliases. Show related funds and covered weight, and disclose that active-fund holdings can change after the reporting period.

Codex synthesis is optional and evidence-bound. `analyze_weekly.py` emits the evidence file and deterministic fallback. Codex may produce only structured explanation fields, with at least two period references per main conclusion. It cannot create numbers, entities, scores, actions, or target weights. `finalize_weekly_analysis.py` rejects unknown references, mismatched hashes, invented entities, action percentages, and action language outside the permitted gate.

## Weekly Date Rules

- W0 uses the latest available trading date; on a weekday it is normally incomplete and must be labeled `进行中`. W-1 and W-2 are the two preceding complete trading weeks.
- Use the previous trading day's close before the target week as baseline when computing weekly returns.
- Show exact dates in the report, for example `2026-07-03 -> 2026-07-10`.
- Store the latest trading day at collection as `collection_trade_date`. If it is later than `end_date`, current-day flow is a post-period snapshot and must not confirm the completed-week score.
- The main inflow/outflow ranking for a completed-week report uses the 5-day net flow whose source date equals `end_date`; rank it by 5-day flow and join the same row's 5-day return. A later current-day snapshot belongs only in the separately dated 今日 panel.
- If holdings have no real amounts, label returns and weights as equal-weight assumptions.
- Never use a NAV or price dated after the requested end date. If valid NAV coverage is below 90%, suppress the formal portfolio return and display coverage plus a partial estimate.

## ETF Return Rules

Listed ETFs can have price discontinuities from splits, conversions, or share adjustments. Do not compute weekly ETF returns from unadjusted prices without checking for discontinuities.

Preferred return evidence order:

1. Adjusted ETF historical price, such as `fund_etf_hist_em(..., adjust="hfq")` or a reliable adjusted source.
2. ETF cumulative NAV over the same period.
3. Feeder fund cumulative NAV return as a proxy, clearly labeled as proxy.
4. IOPV snapshots when both endpoints are date aligned.
5. Unadjusted Eastmoney/Sina price return only when there is no suspicious discontinuity.

Never use unit NAV across a share split. Use unit NAV only with the same day's close to compute `收盘净值溢价`; real-time price/IOPV produces `实时IOPV溢价` and must be labeled separately.

`recommendation_eligible` means the report-end return, turnover, date-aligned closing premium, and sector evidence are complete enough for replacement observation. `execution_ready` additionally requires a quote no older than five minutes and confirmed live premium below 2%. Missing intraday data never changes the historical score; it changes the action to `替换观察，执行前复核实时溢价`.

When a split is present or the absolute weekly move exceeds 15%, require arithmetic and cross-source checks. A conflict suppresses the published return and score. A single reliable cumulative-NAV source may remain visible at medium confidence but cannot alone support a replacement.

If unadjusted price implies an extreme move inconsistent with sector/index moves, mark it as suspect and do not present it as true performance.

## Data Source Fallbacks

- Provider routing: `auto` promotes only health-approved proxy datasets; `shadow` collects proxy evidence without changing the report; `akshare-only` disables it.
- Proxy fund NAV: `fund_nav.adj_nav`, then continuous `accum_nav`. `unit_nav` is same-day display/split evidence only.
- Proxy ETF history: `fund_daily + fund_adj`; never use `pro_bar` adjustment as ETF evidence.
- Proxy sector flows: `net_amount` is documented in 亿元; convert it to yuan before aggregation/output. Industry return uses `close`; concept return uses `industry_index` or compounded `pct_change`.
- Proxy sector-flow rankings must state `同花顺行业全量` or `同花顺概念全量`. Do not mix or compare exact ranks with 东方财富 taxonomies as though the sector names represented the same universe.

- Fund NAV: `fund_open_fund_info_em`; for ETFs use `fund_etf_fund_info_em` or ETF historical endpoints when needed.
- Market style indexes: first try `index_zh_a_hist`; if it fails, try `stock_zh_index_daily` / `stock_zh_index_daily_tx`.
- ETF spot trading quality: `fund_etf_spot_em` for price, IOPV, premium/discount, turnover, update time.
- Weekly fund rankings: `fund_open_fund_rank_em`, using `近1周` as the primary short-term evidence.
- Sector quotes: `stock_board_industry_name_em` and `stock_board_concept_name_em`.
- Sector flows: `stock_sector_fund_flow_rank` for 今日、5日、10日 industry/concept flows.

When an endpoint fails, do not silently leave empty sections. Show the fallback used or the explicit data gap.

Warnings are dataset-level. A primary endpoint failure that was recovered by another source belongs under `已自动恢复的数据源`, not `未解决的数据缺口`.
Only required `failed/partial` logical datasets are unresolved. No requested entities is `not_required`; missing current-session quotes or IOPV is normally `optional_unavailable` and must be listed separately.
Complete, previously validated closed-day sector history may recover a report from SQLite when the current live endpoint is downgraded. Require all requested trading dates; otherwise retain the gap and never synthesize missing days.

An index endpoint returning rows is not sufficient. Reject it when the baseline or end date is missing or the latest row is stale. 中证红利 should fall through to the CSIndex or Tencent source before being declared unavailable.

## Sector Top10 Rules

Weekly sector analysis must answer:

- Which sectors were rewarded by returns this week?
- Which sectors had sustained inflow rather than a one-day spike?
- Which sectors rose but had divergent or weak flow confirmation?
- Whether the current portfolio already covers the sector.
- Whether the opportunity is better represented by an ETF, an active fund, or observation only.

Flow labels:

- `持续流入`: today is positive and at least two of today/5-day/10-day are positive.
- `短线脉冲`: today is positive but 5-day/10-day confirmation is weak or missing.
- `持续流出`: today is negative and at least two of today/5-day/10-day are negative.
- `分歧`: at least two periods are available and directions are mixed.
- `数据不足`: fewer than two of today/5-day/10-day are available.

All flow amounts are normalized internally to人民币元 and displayed with an explicit `亿元` suffix. Never display an unlabeled value such as `43`. When only one period is available, show `仅单周期，暂不判断` and name the missing periods rather than implying that no data exists.

Daily board snapshots and fund-ranking theme proxies are separate evidence panels. They must not populate the weekly industry/concept return lists.
For concept boards, `最近有效收盘概念行情` may come from the report-end `moneyflow_cnt_ths` row and is distinct from an optional intraday snapshot. Preserve the provider taxonomy and source date.
Rolling 5-day/10-day rankings must have `source_date == week.end_date` before they are used as target-week return or flow evidence. Preserve snapshot `cache_age_days` and display it.
An N-day proxy aggregate must contain exactly N distinct trading dates for that sector. Partial history cannot be labeled 5-day or 10-day, and percentage fallback is valid only when every selected trading day has a numeric daily return.
Complete delivery requires industry/concept returns and both positive and negative flow lists for each taxonomy. One populated direction cannot hide a missing opposite-direction list.
Store the report-end single-day flow as `报告期末日`; reserve `今日` for the actual collection trading date. The report-end value confirms the completed week, while the current snapshot is display-only and cannot replace it.

For the union of industry return, inflow, and outflow Top10, use `stock_sector_fund_flow_hist` to derive the end-day value and the latest 5/10 trading-day sums. If the official 5-day ranking and daily aggregation have opposite signs, label `数据冲突` and stop persistence classification. Display the periods actually obtained and name missing periods.

## Interpretation Rules

The default weekly visual deliverable follows `weekly-visual-v2`; section order, empty-state behavior, accessibility, mobile, print, and provenance rules live in `visual_report_contract.md`. Numerical and recommendation rules in this document cannot be bypassed for presentation convenience.

The mandatory leverage block follows the three-week style section. Compare W0/W-1/W-2 financing balance and financing trading intensity, but keep the module informational. Show total balance, distance from the comparable historical peak, financing leverage density, financing trading intensity, heat, pressure, policy nodes, and explicit data limitations. A partial W0 is monitoring evidence only. Missing same-day market cap or turnover suppresses heat rather than substituting a mismatched date.
Render three separate 60-session leverage tracks: total margin balance, financing leverage density, and the selected broad-index trajectory. Label the broad-index source, such as 中证全指 or a fallback index, so the user knows what market proxy is being shown.
When same-day market cap and turnover exist but long history is insufficient, say that current density/intensity are available while historical heat/pressure scoring is withheld due to an insufficient baseline. Do not describe this as missing current market scale.
For the three-week table, show heat and deleveraging-pressure scores or their coverage/blocker for each period. These values must be recomputed as of that period's end date.

- Separate "market rewarded theme" from "current portfolio exposure".
- Do not infer that all technology funds benefited when style indexes diverge.
- Compare candidate ETF performance against current holdings and against style indexes.
- Compare current holdings against sector Top10; do not rely only on fund rankings.
- For hot ETFs, premium above 1% deserves a warning; premium above 2% should be highlighted as a chase-risk signal.
- Weekly reports should suggest observation/rebalance priorities, not automatic trades.
- Fewer than three replacements is a valid outcome. Use `insufficient_evidence` rather than filling the list with unscored or high-premium candidates.
- If a complete sector universe contains no matching Top10 theme for a fund, record low confirmation evidence instead of treating the dataset as missing. This permits an auditable score without pretending that the fund has sector support.
- Market-access labels such as 融资融券、深股通、沪股通 and broad index-membership tags do not belong in the actionable concept Top10.
- User-facing text must translate internal states. The conclusion must separately state return leaders, flow-confirmed leaders, portfolio coverage, duplicate exposure, and concrete recommendation blockers.
- Holding rows must label current weight, weekly/1-month/3-month returns, one-year maximum drawdown, score, and action. Equal-weight assumptions are not account positions.
- Sector labels come from the sector taxonomy, not the fund-theme keyword dictionary. Unknown names are `待分类` with a reason; they are not automatically treated as missing opportunities.
