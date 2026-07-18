# Visual Report Contract

The default weekly deliverable is a self-contained Chinese HTML report with format version `weekly-visual-v2`. Markdown is a portable companion, not a substitute for HTML. Data revisions and report-format versions are independent: changing an analysis formula does not change the visual contract, while removing, renaming, or reordering a mandatory section requires a new report-format version.

## Mandatory Order

The HTML sections must appear exactly once and in this order:

1. Core KPIs.
2. Evidence-bound three-week synthesis.
3. Three-week portfolio trajectory.
4. Three-week style trajectory.
5. A-share margin-leverage heat and deleveraging pressure.
6. Industry rotation matrix.
7. Concept rotation matrix.
8. Structured weekly conclusion.
9. Holding performance.
10. Market style detail.
11. Real weekly sector returns.
12. Latest valid close or current-day snapshot, clearly separated from weekly evidence.
13. Post-period current-day flows, excluded from the completed-week conclusion.
14. Report-period inflow and outflow rankings.
15. Portfolio coverage and overlap gaps.
16. Fund-theme proxy and weekly fund ranking.
17. ETF report-end and live trading quality.
18. Gated replacement observations.
19. Incremental-cache summary.
20. Required, optional, recovered, and audited data quality.

`scripts/report_contract.py` is the executable source of truth for section identifiers and order. Do not duplicate a competing list in a renderer.

## Presentation Rules

- Use a restrained work-focused layout, 8px-or-less card radii, stable grids, explicit labels, and no decorative gradients or floating page-section cards.
- Positive and negative values use color plus a numeric sign; never communicate direction by color alone.
- Every percentage and currency value carries a visible label and unit. Fund flow is displayed in `亿元`; weights, returns, drawdowns, and premiums are percentages.
- The leverage block displays amounts in `亿元`, ratios in percentages, and model values in points. It always shows heat and pressure together, labels five-year versus full-history percentiles, and states that it is display-only.
- The leverage block must include three labeled 60-session tracks: two融余额, 融资杠杆密度, and the selected broad-index trajectory. It must also show per-week heat/pressure values or explicit blockers in the three-week mini table.
- Explain financing leverage density, financing trading intensity, leverage heat, and deleveraging pressure in plain Chinese. State that heat is not "higher is better" and pressure is generally calmer when lower, while neither score alone predicts returns.
- Show the ratio-history observation count and date span beside rolling-window percentiles. Do not label a partial denominator history as full-history coverage.
- Historical comparison tables must display separate peak dates for absolute financing balance, financing leverage density, and financing trading intensity when those dates differ.
- Desktop shows comparison grids. At 900px or below, every row becomes a labeled vertical layout rather than a squeezed table.
- Include a keyboard skip link, semantic main region, horizontal report navigation, Chinese language metadata, viewport metadata, and visible focus behavior.
- Include print CSS. Navigation and full interface audit may be omitted from print, while conclusions, holdings, sector evidence, ETF quality, actions, and data gaps remain readable.
- Keep HTML self-contained and offline-readable: no remote fonts, scripts, images, or chart dependencies.

## Degraded Reports

Every mandatory section remains present when its dataset is unavailable. Render an explicit Chinese empty state that names the missing evidence or blocking gate. Never hide a failed section, fill it with another dataset, expose internal enums, or manufacture rows to satisfy a visual count.

User-facing HTML must not expose internal status codes such as `insufficient_data`, `insufficient_evidence`, `fallback_used`, `not_required`, `optional_unavailable`, `stale_source`, or `deterministic_fallback`. Translate them into Chinese labels and place raw statuses only in machine-readable JSON/audit fields.

Required failures appear in `未解决的必需数据`. Optional intraday failures appear separately and must not lower completed-week report validity. Recovered fallbacks appear under `已自动恢复的数据源`, not as unresolved warnings.

Top3 may contain zero to three entries. An empty Top3 section must distinguish data blockage from candidates failing score, score-gap, premium, liquidity, or execution gates.

## Provenance

The report header and footer disclose:

- `schema_version`
- `data_revision`
- `report_format_version`
- analysis cutoff and period completeness
- weight basis
- first 12 characters of the holdings snapshot hash
- first 12 characters of the LLM evidence hash

The full hashes remain in JSON. Credentials and raw proxy URLs never appear in HTML, Markdown, JSON, logs, or cache audit.

## Acceptance

Run `scripts/validate_report.py --require-complete` for every normal user-facing weekly HTML. Diagnostic degraded reports may omit the flag, but must never be labeled or delivered as complete. The validator must verify format metadata, mandatory section uniqueness and order, nonblank degraded states, required row counts, responsive and print markers, navigation targets, translated states, data semantics, recommendation gates, and cross-section consistency. In particular, valid latest-period rows in the three-week industry/concept matrices cannot coexist with empty single-period Top10 sections. Those Top10 sections must be labeled as rolling five-trading-day observations; the three-week matrices remain the natural-week source of truth.

Before changing the format, render one current seven-fund report and one degraded fixture. Review desktop, mobile, and print behavior. A new format version is required when a consumer would need to change how it locates or interprets a section.
