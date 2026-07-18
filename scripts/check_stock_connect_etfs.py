#!/usr/bin/env python3
"""Check HKEX Northbound Stock Connect ETF eligibility by theme."""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from urllib.request import Request, urlopen


HKEX_URLS = {
    "SSE": "https://www.hkex.com.hk/-/media/HKEX-Market/Mutual-Market/Stock-Connect/Eligible-Stocks/View-All-Eligible-Securities/SSE_Securities.csv",
    "SZSE": "https://www.hkex.com.hk/-/media/HKEX-Market/Mutual-Market/Stock-Connect/Eligible-Stocks/View-All-Eligible-Securities/SZSE_Securities.csv",
}

THEME_PATTERNS = {
    "semiconductor": re.compile(r"semiconductor|semicon|\bsemi\b|chip|integrated circuit", re.I),
    "electronics_related": re.compile(r"electronics", re.I),
}


def decode_hkex_csv(data: bytes) -> str:
    for encoding in ("utf-16", "utf-8-sig", "utf-8"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-16", errors="ignore")


def fetch_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    data = urlopen(request, timeout=30).read()
    return decode_hkex_csv(data)


def parse_source_meta(text: str) -> dict[str, str]:
    meta: dict[str, str] = {}
    for line in text.splitlines()[:20]:
        clean = " ".join(part.strip() for part in line.split("\t") if part.strip())
        if clean.startswith("Updated:"):
            meta["updated"] = clean.removeprefix("Updated:").strip()
        elif "following Northbound trading day" in clean:
            meta["effective_note"] = clean
    return meta


def parse_hkex_rows(text: str, market: str) -> list[dict[str, str]]:
    rows = []
    for row in csv.reader(text.splitlines(), delimiter="\t"):
        row = [cell.strip().lstrip("\ufeff") for cell in row]
        if len(row) < 6:
            continue
        if row[5] != "TRST":
            continue
        rows.append(
            {
                "market": market,
                "channel": "沪股通" if market == "SSE" else "深股通",
                "code": row[1],
                "ccass_code": row[2],
                "name_en": row[3],
                "instrument_type": row[5],
            }
        )
    return rows


def classify(row: dict[str, str]) -> str | None:
    name = row["name_en"]
    if THEME_PATTERNS["semiconductor"].search(name):
        return "semiconductor"
    if THEME_PATTERNS["electronics_related"].search(name):
        return "electronics_related"
    return None


def collect(theme: str) -> tuple[list[dict[str, str]], dict[str, dict[str, str]]]:
    matches = []
    source_meta = {}
    for market, url in HKEX_URLS.items():
        text = fetch_text(url)
        source_meta[market] = {"url": url, **parse_source_meta(text)}
        for row in parse_hkex_rows(text, market):
            category = classify(row)
            if category == theme or (theme == "semiconductor" and category == "electronics_related"):
                matches.append({**row, "category": category})
    return matches, source_meta


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--theme", default="semiconductor", choices=["semiconductor"])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    rows, source_meta = collect(args.theme)
    counts = {
        "total": len(rows),
        "semiconductor": sum(1 for row in rows if row["category"] == "semiconductor"),
        "electronics_related": sum(1 for row in rows if row["category"] == "electronics_related"),
    }
    payload = {
        "source": HKEX_URLS,
        "source_meta": source_meta,
        "theme": args.theme,
        "counts": counts,
        "notes": [
            "HKEX CSV files are UTF-16 encoded and tab-delimited.",
            "Rows are filtered to Instrument Type TRST to avoid mixing ETF results with stocks.",
            "Theme matching is based on HKEX English security names; inspect the ETF benchmark/index and constituents before treating related ETFs as exact substitutes.",
            "electronics_related rows are related but not pure semiconductor exposure.",
        ],
        "rows": rows,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    main()
