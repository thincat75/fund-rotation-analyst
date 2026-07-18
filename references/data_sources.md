# Data Sources

## Shared Incremental Cache

Use `work/cache/fund-rotation/cache.sqlite3` for all report versions. The SQLite store uses WAL, `busy_timeout`, transactions, and these tables: `time_series`, `snapshots`, `fund_profiles`, `api_audit`, `weekly_artifacts`, and `schema_migrations`.

- Time-series identity is `provider + dataset + symbol + trade_date`. Use provider-native `ts_code` for sectors; namespaced stable IDs are fallback only. Never merge different taxonomies by display name.
- Read a validated cached range before requesting history. Request only missing dates; successful closed-day history is immutable unless `--refresh-dataset` explicitly names the dataset.
- ETF spot TTL is exactly 5 minutes using full timestamps, fund-ranking snapshots expire next trading day, basic/scale/turnover TTL is 30 days, and quarterly holdings TTL is 90 days. Profiles older than 180 days are not scoring evidence.
- Empty, stale, truncated, wrong-field, or failed responses never overwrite a successful row.
- Frozen Tushare sector history is reusable in `auto` mode after a later health check downgrades the live endpoint, provided the exact requested trading-date set is complete and rows remain under the original provider taxonomy. Label this `cached_validated_history`; do not make a network call merely to replace identical closed-day rows.
- `akshare-only` is strict: it must not use proxy-derived cached rows even if they are complete. Use `auto` when previously validated proxy history is allowed to recover a closed-day report without a live proxy runtime.
- Current-day market snapshots and long historical market-denominator series have independent cache states. A current `daily_info` snapshot can prevent repeat network calls for today's density/intensity display, but it cannot satisfy the 500-observation historical baseline required for heat/pressure percentiles.
- When multiple providers write the same logical series/date, readers deduplicate by `trade_date` and select the newest validated row from the active source route. Duplicate cached providers are audit evidence, not extra historical samples.
- Concept flow is requested in five-trading-day windows. If returned rows approach the API limit or expected dates are absent, bisect the window and deduplicate by `trade_date + ts_code`.
- Cache raw-data version independently from analysis and HTML versions. A scoring or rendering change must not redownload historical data.
- Record cache hits and API attempts in `api_audit`; exclude real-time ETF calls from historical cache-hit targets.

Use AkShare and public fallbacks as the default source chain. An authenticated third-party Tushare proxy is optional and is promoted independently by dataset after health and cross-source checks. It is acceptable for individual endpoints to fail; preserve partial results and surface only unresolved logical datasets as warnings.

## Third-Party Tushare Proxy

The only permitted initialization is the environment-variable contract in `SKILL.md`, implemented once in `scripts/tushare_proxy.py`. The client restricts `TUSHARE_HTTP_URL` to the configured host and port and records only endpoint/token hashes, never raw credentials. Because transport is HTTP, assume credentials and returned data are exposed to the proxy operator. Label all returned data `第三方 Tushare 代理`.

Run `scripts/smoke_test_tushare_proxy.py` in isolated subprocesses with a hard timeout. The seller examples, fund NAV, fund portfolio, industry flow, and concept flow require 3/3 success. Quick-path interfaces require median latency at most 3 seconds and P95 at most 10 seconds. Slow but repeatable `fund_portfolio` and sector-flow datasets may be classified as `cached/background` when median latency is at most 8 seconds and P95 at most 15 seconds; they must be prefetched and reused rather than blocking every quick report. A slower live endpoint does not invalidate already verified frozen history, but it cannot refresh missing dates. HTTP 200 with HTML, empty data, wrong fields, or stale dates is failure.

Industry/concept flow health additionally requires three identical content fingerprints for the same query window, at least 99% numeric `net_amount` coverage, no duplicate `(trade_date, sector)` keys, both positive and negative observations, and a sane magnitude. A slow response that passes these checks is usable from cache; it is not described as a fast real-time interface.

Dataset routes in `auto` mode:

- Fund NAV: proxy `fund_nav` (`adj_nav`, then continuous `accum_nav`) -> AkShare.
- Fund portfolio: proxy `fund_portfolio` using latest `ann_date <= report.end_date` -> AkShare -> quarterly profile cache.
- Style indexes: proxy `index_daily` -> CSIndex -> Tencent -> date-qualified Sina.
- Industry/concept flow: proxy `moneyflow_ind_ths` / `moneyflow_cnt_ths` -> AkShare -> same-period cache.
- ETF history: proxy `fund_daily + fund_adj` -> AkShare adjusted price -> checked Sina history.
- ETF live quote: promoted proxy `rt_etf_k` -> targeted Eastmoney -> Sina snapshot. Batch Shanghai `5*.SH` with `topic="HQ_FND_TICK"`; batch Shenzhen `15*.SZ` without that topic.
- ETF IOPV: promoted proxy `rt_etf_sz_iopv` for Shenzhen -> existing public quote chain. There is no corresponding promoted Shanghai proxy IOPV in this workflow; use date-aligned closing price/unit NAV for Shanghai report-end premium and mark intraday IOPV not applicable.
- Fund rankings: AkShare; do not reconstruct a full-market ranking from high-frequency proxy NAV calls.

The proxy flow APIs use the 同花顺 industry/concept taxonomy, while the AkShare public chain can use 东方财富 classifications. Keep the chosen taxonomy and universe scope on every ranking. Do not silently merge names or describe one provider's taxonomy as a provider-neutral whole-market universe. When proxy data replaces the public ranking, preserve the public result as alternate audit evidence rather than discarding it.

Treat report-end concept close and intraday concept quotes as different datasets. `moneyflow_cnt_ths` may resolve `concept_latest_close` from `industry_index`, `pct_change`, and `net_amount`. It does not become `concept_intraday` unless the returned source date equals the collection trading date. Use the label `同花顺概念体系` and never merge it with 东方财富 concepts by name alone.

Resolve fund TS codes from `fund_basic`; never guess `.OF`, `.SH`, or `.SZ`. Persist a compact fund-code index and collapse pagination into one logical audit row; do not embed every fund-master page in weekly shadow artifacts. For ETF return, calculate adjusted close from `fund_daily.close × fund_adj.adj_factor`; normalize qfq to the report-end factor. `pro_bar` must always receive `api=pro`, but its `adj` parameter is not ETF adjustment evidence.

For `moneyflow_ind_ths` and `moneyflow_cnt_ths`, `net_amount` is documented in 亿元. Convert it to人民币元, then sum it over the report-end trading dates. Do not derive net flow from `net_buy_amount` and `net_sell_amount`. The THS industry-summary fallback also reports `净流入` in 亿元 and requires the same conversion. AkShare/Eastmoney sector-rank and historical-flow amounts are raw yuan. Persist `资金单位=元`, and render an explicit 亿元 suffix. Industry return uses the industry `close` series. Concept return uses `industry_index`; `close_price` is a leading-stock field and must not be used as the concept index. When index levels are unavailable, compound daily `pct_change`; never add percentages.

Use `shadow` for at least three distinct live trading dates before promotion, then run `scripts/validate_tushare_shadow.py`. Historical replay is useful for cutoff and arithmetic checks but only sets `provisional_promotion_eligible`; it cannot promote a source. Fund NAV must differ from AkShare by at most 0.1%; ETF close and style-index close by at most 0.2%; quarterly portfolio Top10 stock-code overlap must be at least 80%. Promotion is recorded per dataset and, where applicable, per fund code, index symbol, or ETF code. Each entry owns its `operational_eligible`, `promotion_eligible`, `crosscheck_status`, provider, transport, and endpoint fingerprint. One successful proxy API never promotes unrelated datasets. Proxy failure after promotion must fall back without preventing a degraded report.

## Holdings Input

Preferred JSON shape when weights are assumptions:

```json
{
  "portfolio_meta": {
    "weight_mode": "assumed_equal",
    "weight_note": "用户尚未提供真实金额，按等权假设分析",
    "amounts_are_assumptions": true
  },
  "holdings": [{"code": "161725", "name": "招商中证白酒指数", "is_core": false}]
}
```

The legacy list format remains supported. Required field: `code`. Use explicit user weights first, then real amounts, otherwise equal weight; always disclose the basis.

## AkShare Interfaces

Fund data:

- `fund_name_em`: fund code/name/type lookup.
- `fund_open_fund_info_em`: open-end fund NAV and performance details.
- `fund_etf_fund_info_em`: ETF NAV/market data fallback.
- `fund_individual_basic_info_xq`: fund profile fallback.
- `fund_info_ths`: fund profile fallback.
- `fund_individual_detail_hold_xq`: asset allocation snapshot for product risk context.
- `fund_portfolio_hold_em`: quarterly fund stock holdings.
- `fund_portfolio_industry_allocation_em`: quarterly fund industry allocation.
- `fund_open_fund_rank_em`: fund performance rankings.

Market style and index data:

- `index_zh_a_hist`: A-share index historical data.
- `stock_zh_index_hist_csindex`, `stock_zh_index_daily_tx`, and `stock_zh_index_daily`: index fallbacks. Validate target-period coverage after every call; a non-empty stale series is not success.
- CSI-family indexes should try the CSIndex history fallback before declaring data insufficient when AkShare's generic index history endpoint is stale or empty.

Default style index map:

- 沪深300: `000300`
- 上证50: `000016`
- 中证500: `000905`
- 中证1000: `000852`
- 创业板指: `399006`
- 科创50: `000688`
- 国证成长: `399370`
- 国证价值: `399371`
- 中证红利: `000922`
- 中证全指: `000985`（两融去杠杆压力的宽基分项）

Sector and fund flow data:

- `stock_board_industry_name_em`: industry board quotes.
- `stock_board_concept_name_em`: concept board quotes.
- `stock_sector_fund_flow_rank`: industry/concept fund flow rankings.
- `stock_sector_fund_flow_hist`: daily industry flow history used to reconstruct end-day and 5/10 trading-day sums for Top10 unions.
- `stock_individual_fund_flow`: individual stock fund flow when explaining major holdings.
- Weekly sector Top10 should combine return leaders and flow leaders. Use the 5-day return field from sector-flow rankings or board history. Never use a latest quote field as a weekly return; present it only as `今日涨跌`.

Stock Connect ETF eligibility:

- HKEX SSE eligible securities CSV: `SSE_Securities.csv`.
- HKEX SZSE eligible securities CSV: `SZSE_Securities.csv`.
- These files are UTF-16 encoded and tab-delimited. Decode as `utf-16` and parse with tab delimiters.
- Filter `Instrument Type == TRST` when looking for ETFs/funds. `EQTY` rows are stocks and should not be mixed into ETF results.
- Read `references/stock_connect_etf_access.md` for Stock Connect ETF rules and known semiconductor ETF patterns.

Listed ETF trading quality:

- `fund_etf_spot_em`: spot ETF price, IOPV real-time estimate, premium/discount, turnover, bid/ask-related fields where available.
- `fund_etf_hist_em`: ETF historical price. For weekly or multi-day return calculations, prefer adjusted prices (`adjust="hfq"` or another verified adjusted source) when the ETF has split/conversion/share-adjustment risk.
- If adjusted ETF prices fail or show suspicious discontinuities, use ETF NAV/IOPV or related feeder-fund NAV as a clearly labeled proxy instead of presenting unadjusted price moves as true returns.
- `fund_etf_category_sina` and `fund_etf_hist_sina`: price, turnover, and historical-price fallback when Eastmoney ETF quote/history endpoints disconnect.
- `fund_etf_spot_ths`: same-trading-day unit/cumulative NAV snapshot fallback. Use unit NAV for closing premium only, not cross-period return.
- ETF return priority is adjusted price, cumulative ETF NAV, cumulative feeder NAV proxy, checked raw/Sina history. A unit-NAV discontinuity with continuous cumulative NAV is a share adjustment, not a loss.
- Preserve `fund_daily.amount` and convert its documented 千元 unit to人民币元. Report-end close and turnover must come from the same selected historical source. Closing premium requires close and unit NAV on the identical date.
- For a split or an absolute weekly return above 15%, compare cumulative NAV against compounded daily growth and any available adjusted-price/feeder source. More than 0.5 percentage-point NAV arithmetic disagreement or more than 1 percentage-point independent-source disagreement is `data_conflict` and removes the ETF from scoring.
- Quick mode must stop once adjusted ETF price evidence is complete. Do not paginate full-market ETF spot/NAV endpoints for a four-code candidate list, and do not fetch cumulative NAV or feeder NAV unless adjusted history failed. Historical end-day turnover may support liquidity scoring, but an ETF with unconfirmed premium remains observation-only.

A-share margin leverage:

- Proxy `margin` supplies exchange-level financing and securities-lending balances in yuan. Promote SSE, SZSE, and BSE independently; BSE remains display-only.
- Proxy `daily_info` supplies `SH_A` and `SZ_MARKET` circulating market cap and turnover in 亿元; adapters must explicitly convert these fields to yuan.
- AkShare `macro_china_market_margin_sh/sz` supplies full public margin history. Exchange daily summary endpoints are current-day crosschecks or fallbacks, not substitutes for missing long-history denominators.
- Join SSE and SZSE only on the identical trade date. Verify total margin balance against financing plus securities-lending balances within 0.1%.
- Cache `margin_summary` and `market_daily_info` in the existing SQLite time-series table. Closed dates freeze after validation; a complete warm cache must not call historical APIs again.
- If `daily_info` history is unavailable, run `backfill_margin_market_history.py` once for exact exchange daily denominators. The SSE adapter sums main-board A plus STAR, and the SZSE adapter sums main-board A plus ChiNext. Do not use the exchange-wide `股票` total when A-share components are available because it includes B shares.
- Cache `margin_concentration` in the same time-series table when `full` mode collects individual-stock financing details. Its denominator is the full SSE+SZSE financing balance on the same date, not the sum of returned stock rows.
- Formal percentiles start at `2014-09-22`. Read `margin_leverage.md` for model and calibration rules.

## Fallback Rules

- If `akshare` is missing, return warnings and allow `--mock` for test reports.
- Cache successful JSON by endpoint, parameters, collector version, report period, mode, and ETF list unless `--refresh` is requested. Never overwrite successful cache with an empty or failed result.
- If a fund NAV endpoint fails, try ETF endpoint variants, then leave the fund with metadata only.
- If fund profile endpoints fail, still classify passive index funds from fund name/type keywords and mark active fund scale/turnover as unavailable.
- If a ranking endpoint fails, omit Top 30 feature analysis and add a warning.
- If sector fund flow fails, still provide index/style analysis.
- If the primary index endpoint fails, continue through the configured chain and reject sources that do not cover both the report baseline and end date. For 中证红利 use `index_zh_a_hist -> stock_zh_index_hist_csindex -> stock_zh_index_daily_tx -> stock_zh_index_daily`.
- If a column name changes, use fuzzy matching for date, price, return, name, and flow columns.
- If today is not a trading day, use the latest available date and show it in the report.
- Use `tool_trade_date_hist_sina` for the A-share calendar. On weekdays, default to the previous completed trading week; on weekends, use the week that just ended.
- Record `source_status` for every endpoint with status, cache hit, attempt, record count, fetch time, and failure reason.
- Aggregate endpoint attempts into `dataset_status`. Only datasets whose complete source chain failed belong in visible warnings; recovered chains are `fallback_used`.
- For sector flows, retry the AkShare wrapper with a local Eastmoney compatibility collector that converts mixed numeric fields before sorting. Then use a dated successful snapshot no older than 10 days. Always expose `source_date` and `stale_days`.
- Treat the local Eastmoney compatibility collector as another attempt in the same source family, not as an independent reliable provider. A success followed by disconnects is `unstable`; prefer a dated successful cache or a different provider and keep the dataset unresolved when neither is available.
- Cross-check concept-flow names against the concept-board universe. Reject a compatibility response when fewer than 20% of its names overlap, because the provider may have returned an industry taxonomy under a concept request.
- `stock_board_industry_summary_ths` may recover today's industry return and net flow. It cannot provide 5-day/10-day evidence. Do not use `stock_board_concept_summary_ths` as concept performance data because it is a concept timetable, not a market-return table.
- Persist fund profiles by code. Scale/basic/turnover becomes stale after 30 days, quarterly holdings/industry after 90 days, and the whole profile is unusable after 180 days.
- Use real Unix wall-clock timeouts when supported. Mark timeout enforcement in source status.
- In weekly `quick` mode, ranking funds are signals only. In `full` mode, collect profile, scale, turnover clues, holdings, and industry allocation for the leading weekly candidates before allowing them into actionable replacement scoring.
- If a weekly report is requested, preserve the visual-report path even when some data sources fail; show partial visual sections and explicit data gaps rather than reverting to a short Markdown-only response.
- Validate weekly reports with `scripts/validate_report.py` before delivery.
- Diagnose live source health with `scripts/smoke_test_interfaces.py`. Run at least two rounds, then retest failures and intermittent sources separately. Record AkShare version, arguments, success rate, latency, latest data date, and exact error; do not infer stability from a single success.
- Classify industry names with `references/sector_taxonomy.json`. Store level-1/level-2 theme, style tags, basis, and confidence separately from fund-holding theme inference.
- Never make live trading or account-state assumptions from public data.
