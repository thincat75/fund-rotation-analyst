# A-Share Margin-Leverage Model

Use this reference for the mandatory `weekly-visual-v2` leverage section. The module explains the market environment only. Its `action_policy` is always `display_only`; enabling or disabling it must leave fund scores, Top3, target weights, and actions unchanged.

## Scope And Sources

- Long-history scope: SSE + SZSE. Aggregate only rows sharing the same trade date.
- BSE: display separately; never include it in the SSE+SZSE long-history score.
- Primary proxy datasets: `margin` and `daily_info`, promoted independently after three successful health rounds and cross-source checks.
- Public fallbacks: AkShare full margin history, exchange daily margin summaries, SSE daily market statistics, and SZSE market summaries.
- Store normalized closed-day rows in the shared SQLite `time_series` table. Units are declared by each adapter and converted to yuan; do not infer units from magnitude.
- Keep the denominator on an A-share basis: SSE main-board A plus STAR Market, and SZSE main-board A plus ChiNext. Exclude B shares, funds, bonds, and options.
- The formal comparable history starts on `2014-09-22`. Earlier rows may be displayed but cannot enter percentiles or calibration.
- Deduplicate logical time series by `trade_date` before modeling when multiple providers cached the same exchange/date. Prefer the newest validated row for the selected provider chain; duplicate providers must not increase sample counts.

Reject future dates, mismatched exchange dates, stale market denominators, empty responses, and unit conflicts. Verify daily:

```text
margin balance ~= financing balance + securities-lending balance
```

The relative error tolerance is 0.1%.

## Metrics

Total margin balance is a display metric. Financing balance drives scoring because securities-lending rules have changed materially.

```text
financing leverage density = financing balance / SSE+SZSE A-share circulating market cap
financing trading intensity = financing purchase amount / SSE+SZSE A-share turnover
```

Both ratios require exactly aligned trade dates. Missing circulating market cap or turnover allows an absolute-balance display but suppresses leverage heat.

Leverage heat, 0-100:

- 40% rolling-five-year percentile of financing leverage density.
- 25% rolling-five-year percentile of financing trading intensity.
- 20% percentile of 20-session financing-balance growth.
- 15% Top100 financing concentration percentile in `full` mode.

If concentration is unavailable, normalize the available 85% weights. Density and trading intensity are mandatory and total evidence coverage must be at least 75%.
All percentile inputs use a strict trailing sample that excludes the current observation. The current day may be displayed, but it must not help define its own historical percentile.

Top100 concentration is:

```text
sum(top100 stock financing balances) / full SSE+SZSE financing balance
```

Do not use the returned detail rows as the denominator. Publish a concentration percentile only when the current universe has at least 100 unique stocks and at least 500 prior comparable observations.

Deleveraging pressure, 0-100:

- 40% five-/twenty-session financing-balance decline and acceleration.
- 30% twenty-session decline pressure across CSI All Share, CSI 300, and CSI 1000.
- 20% market-turnover contraction versus the prior 20-session mean.
- 10% financing-intensity contraction versus the prior 20-session mean.

Always render heat and pressure together. The two-dimensional regime is more informative than either score alone.
The index-decline component uses the median decline pressure across CSI All Share, CSI 300, and CSI 1000 when available. If CSI All Share is unavailable, use the available broad-index set and label the displayed trajectory source explicitly. Missing turnover or financing-intensity history should lower pressure coverage; it must not be silently scored as zero stress.

Current same-day market cap and turnover are separate from the long historical denominator series. A report may show current density and trading intensity while withholding heat or pressure scores because the rolling history is too short.

When proxy history is unavailable, bootstrap the exact exchange denominator once and then update it incrementally:

```bash
python scripts/backfill_margin_market_history.py \
  --cache-root work/cache/fund-rotation \
  --end-date YYYY-MM-DD \
  --sessions 650
```

The bootstrap reads only trading dates already validated by the margin-summary cache. It fetches raw SSE daily-overview JSON and SZSE daily-summary workbooks, writes only successful closed dates, and never replaces good rows with failures. At least 500 prior observations plus the scored day are required for rolling-window percentiles. A shorter-than-five-year but sufficient sample must be labeled `近5年窗口分位（可用样本N日）`, not a full-history percentile. Publish an all-history ratio percentile only when the denominator series reaches the 2014 comparable boundary.

## Interpretation Guardrails

Explain all four related indicators in the report:

- Financing leverage density is financing balance divided by SSE+SZSE A-share circulating market cap. Higher is not inherently better: it means deeper leverage participation and potentially larger feedback in both directions.
- Financing trading intensity is financing purchases divided by SSE+SZSE A-share turnover. Higher means more active leveraged buying and possibly more crowding; lower may mean cash-driven trading or weak risk appetite.
- Leverage heat has no standalone good/bad direction. It describes the level of leverage participation.
- Deleveraging pressure is usually calmer when lower and riskier when higher, but it is not a deterministic return forecast.

Never conclude:

- Low leverage means substantial upside is available.
- High leverage means an immediate market top.
- Falling financing balance means a bottom is certain.
- Rising financing balance is automatically bullish.

High heat with low pressure means leverage is elevated but the trend is not yet broken. High heat with high pressure is a deleveraging-risk background, not a deterministic crash call. Low heat with high pressure means weak risk appetite, not a mechanical buying opportunity.

## Historical Calibration

Run `scripts/calibrate_margin_model.py` outside the normal weekly path. It uses walk-forward trailing-five-year percentiles, at least 500 prior observations, and future 20-/60-session maximum drawdowns only as outcomes. Each historical score may use only information available on that date. Bands with fewer than 30 observations remain `insufficient_sample` and cannot publish probability claims.

Policy events are versioned explanatory markers. They never alter the raw series or score:

- `2014-09-22`: comparable-history boundary.
- `2015-11-23`: financing margin requirement change.
- `2024-07-11`: securities-lending and refinancing rule changes.
- `2026-01-19`: financing margin requirement change.

Calibration artifacts must record model version, cutoff, evidence hash, and sample sufficiency. A calibration cutoff later than the report cutoff is invalid for historical replay.

## Three-Week Replay

Three-week leverage rows are recomputed as of each period's own end date. Do not reuse the latest report-day heat, pressure, or percentile for W-1 or W-2. Partial W0 can be displayed as monitoring evidence, but formal fund actions remain based on completed-week evidence.

Historical comparison tables keep distinct peak dates for absolute financing balance, financing leverage density, and financing trading intensity. Never attach the balance-peak date to a ratio peak unless the dates are actually identical.
