# Stock Connect ETF Access

Use this reference when the user asks whether a fund, ETF, or index can be bought from Hong Kong through Stock Connect / Northbound trading / 沪股通 / 深股通.

## Core Rules

- An index itself is not a tradable security. It can be observed or used as a benchmark; the tradable instruments are exchange-listed ETFs, stocks, or other listed products.
- OTC public fund shares, ETF feeder funds, and A/C share classes such as `020639 广发半导体设备ETF联接A` or `020356 华夏半导体材料设备ETF联接A` are not directly bought through Stock Connect.
- For Hong Kong investors, only SSE/SZSE listed securities in the HKEX Northbound eligible securities files can be bought through Stock Connect.
- For ETFs, filter HKEX rows where `Instrument Type` is `TRST`. Do not mix in individual stocks (`EQTY`).
- SSE-listed ETFs are bought via 沪股通. SZSE-listed ETFs are bought via 深股通.
- Always verify the latest HKEX CSV before answering because eligibility changes. Treat any hard-coded list below as a dated snapshot only, not as the source of truth.

## HKEX CSV Parsing

HKEX eligible securities CSV files are UTF-16 encoded and tab-delimited. Do not search them as plain UTF-8 text.

Current URLs:

- SSE: `https://www.hkex.com.hk/-/media/HKEX-Market/Mutual-Market/Stock-Connect/Eligible-Stocks/View-All-Eligible-Securities/SSE_Securities.csv`
- SZSE: `https://www.hkex.com.hk/-/media/HKEX-Market/Mutual-Market/Stock-Connect/Eligible-Stocks/View-All-Eligible-Securities/SZSE_Securities.csv`

Correct parsing approach:

```python
rows = csv.reader(text.splitlines(), delimiter="\t")
```

where `text` is decoded with `utf-16`.

Prefer using the bundled checker before answering:

```bash
python scripts/check_stock_connect_etfs.py --theme semiconductor --output stock_connect_semiconductor_etfs.json
```

The checker records the HKEX file `Updated:` date, filters ETF/fund rows to `TRST`, and reports counts for pure semiconductor versus related electronics rows.

## Semiconductor ETF Query Pattern

When searching HKEX eligible ETFs for semiconductor exposure, include English patterns:

- `semiconductor`
- `semicon`
- `semi`
- `chip`
- `integrated circuit`

Also inspect `electronics` separately as related but not pure semiconductor exposure.

## Known Northbound Semiconductor/Chip ETFs Snapshot

This section is a reference snapshot, not a substitute for the live HKEX check. As of HKEX files updated 29 June 2026 for the following Northbound trading day, pure semiconductor/chip ETF rows included:

| Channel | Code | HKEX English Name | Notes |
| --- | --- | --- | --- |
| 沪股通 | `512480` | CPIC SEMICON ETF | Semiconductor related; inspect constituents/index before treating as equipment/materials. |
| 沪股通 | `512760` | GUOTAI CES CHINA SEMICON CHIP INDEX ETF | Semiconductor/chip chain. |
| 沪股通 | `516350` | E FUND CSI CHIP INDUSTRY ETF | Tracks CSI chip industry style exposure. |
| 沪股通 | `516640` | FULLGOAL CSI CHIP INDUSTRY INDEX ETF | Tracks CSI chip industry style exposure. |
| 沪股通 | `516920` | CUAM SEMICONDUCTOR ETF | Tracks CSI chip industry style exposure in current fund profile. |
| 沪股通 | `560780` | GF CSI SEMICONDUCTOR MATL & EQPT ETF | Directly related to CSI Semiconductor Materials & Equipment theme. |
| 沪股通 | `561980` | CN MERCH CSI SEMICONDUCTOR IND IDX ETF | Broader semiconductor industry exposure. |
| 沪股通 | `562590` | CHINAAMC CSI SEMICONDUCTOR MATL ETF | Directly related to CSI Semiconductor Materials & Equipment theme. |
| 沪股通 | `588170` | CHINAAMC CN SCI&TECH INNOV SEMI ETF | STAR board semiconductor materials/equipment exposure. |
| 沪股通 | `588200` | HARVEST SSE STAR CHIP INDEX ETF | STAR board chip exposure. |
| 沪股通 | `588290` | HUAAN SSE STAR CHIP INDEX ETF | STAR board chip exposure. |
| 沪股通 | `588750` | CUAM STAR SEMICONDUCTOR ETF | STAR board chip exposure in current profile. |
| 沪股通 | `588780` | CPIC SSE STAR CHIP DESIGN THEMATIC ETF | STAR board chip design, not pure equipment/materials. |
| 沪股通 | `588890` | CHINA SOUTHERN SSE STAR CHIP ETF | STAR board chip exposure. |
| 深股通 | `159310` | CHIP INDUSTRY ETF | Tracks CSI chip industry style exposure. |
| 深股通 | `159516` | SEMI EQUIPMENT ETF | Directly related to CSI Semiconductor Materials & Equipment theme. |
| 深股通 | `159558` | E FUND SEMICON M&E | Directly related to CSI Semiconductor Materials & Equipment theme. |
| 深股通 | `159801` | GF CHIPS ETF | Broader semiconductor/chip exposure. |
| 深股通 | `159813` | CHIP | Semiconductor related; inspect constituents/index if precision matters. |
| 深股通 | `159995` | CHIPS ETF | Semiconductor/chip exposure. |

Related electronics ETFs that should not be treated as pure semiconductor substitutes:

- `515260` HWABAO CSI ELECTRONICS 50 INDEX ETF
- `159732` CONSUMER ELECTRONICS
- `159997` TH ELECTRONICS ETF

## CSI Semiconductor Materials & Equipment Theme

When the user asks specifically about `中证半导体材料设备主题指数`:

- The index itself cannot be bought directly.
- Tradable ETF choices through Northbound Stock Connect include:
  - `560780` 广发中证半导体材料设备ETF / GF CSI Semiconductor Materials & Equipment ETF, 沪股通
  - `562590` 华夏中证半导体材料设备主题ETF / ChinaAMC CSI Semiconductor Materials ETF, 沪股通
  - `159516` 国泰半导体设备ETF / Semi Equipment ETF, 深股通
  - `159558` 易方达半导体设备ETF / E Fund Semicon M&E, 深股通
- ETF feeder funds such as `020639` and `020356` are not directly tradable through Stock Connect, but their corresponding exchange-listed ETFs may be eligible.

## Response Checklist

When answering:

1. State whether the queried code is an index, OTC fund, feeder fund, listed ETF, or stock.
2. If it is an index or feeder fund, identify the relevant listed ETF code(s).
3. Verify the listed ETF code against HKEX SSE/SZSE eligible CSV.
4. State the channel: 沪股通 for SSE, 深股通 for SZSE.
5. Separate pure semiconductor ETFs from broad electronics/consumer electronics ETFs.
6. Mention the source date from HKEX CSV if available.
7. If the answer depends on a theme name search, state the matching basis, for example "HKEX English security name contains semiconductor/chip/semi"; do not infer exact index exposure from the English name alone.
