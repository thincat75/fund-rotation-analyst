# Scoring Model

The default posture is active rotation with risk constraints. Scores are heuristics for report generation, not investment advice.

## Fund Score

Calculate a 0-100 score when enough data exists:

- Momentum, 35 points: recent 1-week, 1-month, 3-month, and 6-month returns.
- Risk, 20 points: lower drawdown and lower volatility receive higher scores.
- Sector/style alignment, 20 points: fund theme matches strong style indexes and persistent sector inflows.
- Ranking confirmation, 15 points: fund or similar theme appears in recent Top 30 rankings.
- Portfolio fit, 10 points: lower duplication and useful diversification receive higher scores.

When data is incomplete, compute the available components and mark confidence as `低`.

## Market Style

Classify style strength from index returns:

- `强势`: positive 1-month and 3-month return, and better than broad-market median.
- `修复`: positive 1-month return after weak 3-month return.
- `震荡`: mixed or low-magnitude returns.
- `弱势`: negative 1-month and 3-month return.

Compare at least these buckets:

- 大盘: 沪深300, 上证50
- 小盘: 中证500, 中证1000
- 成长: 国证成长, 创业板指, 科创50
- 价值: 国证价值, 中证红利

## Sector Flow

Use 今日、5日、10日 where available:

- `持续流入`: today is positive and at least two available periods among today/5-day/10-day are positive.
- `短线脉冲`: strong positive today, but weak or missing 5-day/10-day flow.
- `持续流出`: today is negative and at least two available periods among today/5-day/10-day are negative.
- `分歧`: mixed direction.
- `数据冲突`: official 5-day direction and daily-history aggregation disagree; do not score persistence.
- `数据不足`: fewer than two periods are available; list the missing periods.

## Weekly Review Score

Use this score for weekly replacement and observation suggestions:

- Weekly performance, 30 points: stronger 1-week return receives higher score.
- 1-month trend, 20 points: positive 1-month return confirms the move.
- Sector return and fund-flow confirmation, 20 points: themes aligned with sector Top10 and sustained inflow receive higher score.
- Style alignment, 10 points: fund theme matches strong weekly style indexes.
- Trading quality, 10 points: ETFs lose points for high premium, weak liquidity, or unconfirmed return basis.
- Portfolio fit, 10 points: candidates that add missing exposure score higher than duplicate exposure.

Normalize weekly and one-month performance to cross-sectional 0-100 percentiles. Do not produce a total score unless at least 70% of component weight is available and weekly performance, one-month trend, and real sector confirmation are all present.
When 70%-99% of component weight is available, normalize the weighted sum by the available weight so the published score remains on a 0-100 scale; always display `score_coverage` beside it.

An actionable replacement also requires a candidate score at least 5 points above the current holding. ETF candidates need `recommendation_eligible`: confirmed report-end return, turnover, same-date closing premium below 2%, and sector evidence. `execution_ready` is a separate pre-trade gate requiring a fresh live quote and live premium below 2%. A closing-eligible ETF without live evidence remains `替换观察` and receives no immediate 3%-5% instruction.
Do not label a current fund `替换候选` from a trivially negative one-month return alone. The weekly score must be valid and below 45; when core score evidence is missing, keep the action at `观察` and state which evidence is absent.

## Three-Week Confirmation

Apply weekly candidate scoring only after the three-week evidence layer is built. Formal allocation or replacement actions require at least two complete weeks of valid trend/sector evidence. An incomplete W0 changes confidence and can issue a risk alert, but cannot be the sole trigger.

Rotation states use complete weeks:

- `持续主线`: at least two complete weeks have positive return and positive weekly net flow; latest complete week remains positive.
- `加速`: the latest two complete weeks are positive on both dimensions and return or flow percentile improves by at least 20 points.
- `新启动`: the previous complete week was unconfirmed; latest complete week turns positive on both dimensions and both percentiles enter the top 30%.
- `高位分歧`: return is positive but weekly flow is not, or ranks diverge materially.
- `退潮`: a previously strong complete week is followed by negative return and negative flow.
- `持续流出`: at least two complete weeks have negative return and negative flow.
- `单周脉冲`: only the latest complete week is strong.
- `数据不足`: fewer than two complete entity-weeks have full trading-day, return, and flow evidence.

For partial W0, add a separate monitor label: `进行中延续`, `进行中分歧`, `进行中转弱预警`, `进行中修复观察`, or `进行中数据不足`. Do not overwrite the confirmed state with the monitor label.

If W0 is partial, `three_week_compound_return` is an as-of monitoring statistic and `completed_weeks_compound_return` is the action basis. Exclude `进行中修复观察` entities from the current fading count, while retaining their complete-week confirmed state in the audit trail.

## Informational Margin-Leverage Model

The A-share leverage module is separate from all fund and replacement scores. Its `action_policy` is `display_only`; enabling, disabling, or degrading it must leave weekly scores, Top3, target weights, and action labels unchanged.

Display total margin balance, but score financing balance. Leverage heat combines rolling-five-year percentiles of financing-to-circulating-market-cap (40%), financing-purchase-to-turnover (25%), 20-session financing growth (20%), and optional Top100 concentration (15%). Density and trading intensity are mandatory; evidence coverage must be at least 75%. Missing concentration is re-normalized over the available 85%.

Deleveraging pressure combines financing-balance decline (40%), broad-index decline (30%), turnover contraction (20%), and financing-intensity contraction (10%). Interpret heat and pressure as a two-dimensional environment. Never turn low heat into an upside-space claim or high heat into a deterministic market-top call. Detailed formulas and thresholds live in `margin_leverage.md`.

All margin percentiles use trailing history only and exclude the observation being scored. Three-week margin heat and pressure are recomputed at each period end, so W-1/W-2 do not inherit the latest report-day percentile, market denominator, or pressure state.

For ETFs:

- Premium above 1% is a caution flag.
- Premium above 2% is a chase-risk flag.
- Unadjusted price discontinuity must override the score with `复权口径待确认` until NAV/adjusted data confirms the return.
- A split or absolute weekly move above 15% must pass cross-source checks. `data_conflict` suppresses the return and score; a lone reliable NAV source is medium-confidence display evidence only.

## Allocation Rules

Start from current weights. If weights are missing, derive them from holding amounts.

Action labels:

- `增配`: target weight at least 5 percentage points above current weight.
- `小幅增配`: target weight 2-5 percentage points above current weight.
- `观察`: target weight within +/-2 percentage points.
- `减配`: target weight 2-8 percentage points below current weight.
- `清仓候选`: target weight more than 8 percentage points below current weight or score is very weak.

Do not label a small unchanged position as `清仓候选`. A very weak score can trigger the label only for a non-core fund with score below 25 and target weight near zero.

Default constraints:

- Single fund target cap: 25%.
- Single theme aggregate cap: 40%.
- High-volatility theme one-period adjustment cap: 10%.
- Core holdings minimum: keep at least 50% of current weight unless clear risk signals exist.
- Cash or unallocated weight is allowed when all candidates are weak.

Apply constraints in this order: raw target, core floor, one-period adjustment cap, single-fund cap, conservative theme cap, then cash. Do not renormalize constrained weights upward.

When an existing high-volatility theme already breaches the 40% strategic cap, report two weights: `target_weight` is the strategic concentration-compliant target; `first_step_target_weight` limits the current-period adjustment to 10%. Do not present the strategic target as a one-time trade.

## Top 30 Fund Feature Analysis

Use `近1月` as the primary ranking view. Rank candidates by a composite performance score:

```text
综合业绩分 = 0.70 × 近1月收益 + 0.30 × (近3月收益 / 3)
```

Rationale:

- `近1月` captures the current market reward signal.
- `近3月 / 3` converts a 3-month cumulative return into an approximate monthly pace, then uses it as trend confirmation.
- The score is for sorting and comparison only; it is not expected return.

Report the full Top 30 table for the primary view, then use other periods only as supporting evidence:

- `近1周`: identify short-term spikes or crowded trades.
- `近3月` / `近6月`: confirm whether the theme has persisted.
- `今年来` / `近1年`: distinguish long-cycle winners from sudden rebounds.

Theme and product classification must state evidence quality:

- High confidence: current or recent disclosed top holdings / industry allocation confirms the theme.
- Medium confidence: benchmark, fund type, or index-tracking information confirms the theme, but holdings are unavailable.
- Low confidence: only the fund name or HKEX English security name matches a keyword.

For passive index funds and ETF feeder funds, emphasize benchmark/index, scale, liquidity, tracking target, and Stock Connect eligibility where relevant. For active funds, show fund scale and turnover or disclosed-holding churn when available; mark these fields as unavailable instead of guessing.

Do not mechanically recommend buying Top 30 funds. Use them as evidence for market heat, crowding, portfolio overlap, and replacement candidates.
