#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
OUT="examples/generated"

cd "$ROOT"
mkdir -p "$OUT" "work/cache/fund-rotation"

"$PYTHON" "scripts/collect_weekly_data.py" \
  --holdings "examples/holdings.sample.json" \
  --output "$OUT/weekly_market_data.json" \
  --mock \
  --mode quick \
  --history-weeks 3 \
  --margin-mode off \
  --provider-policy akshare-only \
  --cache-root "work/cache/fund-rotation"

"$PYTHON" "scripts/analyze_weekly.py" \
  --holdings "examples/holdings.sample.json" \
  --weekly-data "$OUT/weekly_market_data.json" \
  --output "$OUT/weekly_analysis.json" \
  --history-weeks 3 \
  --cache-root "work/cache/fund-rotation" \
  --llm-evidence-output "$OUT/weekly_llm_evidence.json"

"$PYTHON" "scripts/render_weekly_report.py" \
  --analysis "$OUT/weekly_analysis.json" \
  --output "$OUT/weekly_report.md"

"$PYTHON" "scripts/render_weekly_visual_report.py" \
  --weekly-data "$OUT/weekly_analysis.json" \
  --output "$OUT/weekly_report.html"

"$PYTHON" "scripts/validate_report.py" \
  --analysis "$OUT/weekly_analysis.json" \
  --html "$OUT/weekly_report.html"

printf 'Generated:\n  %s\n  %s\n  %s\n' \
  "$OUT/weekly_analysis.json" \
  "$OUT/weekly_report.md" \
  "$OUT/weekly_report.html"
