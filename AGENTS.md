# Agent Integration Contract

This repository is developed and validated in Codex, but its Python pipeline is agent-independent.

## Required workflow

1. Normalize holdings into the documented JSON format.
2. Run `collect_weekly_data.py` with a shared cache.
3. Run `analyze_weekly.py`; treat its numbers, scores, actions, and weights as authoritative.
4. Optionally synthesize prose only from `weekly_llm_evidence.json`.
5. Render Markdown and HTML from the same analysis JSON.
6. Run `validate_report.py` before delivery.

Never invent missing market data, alter deterministic actions, expose credentials, connect to a broker, or place orders.

## Data credentials

- AkShare and the public fallback chain require no key.
- Official Tushare Pro is optional and preferred for authenticated enrichment. Set `TUSHARE_PROVIDER=official` and provide an official `TUSHARE_TOKEN`; do not set `TUSHARE_HTTP_URL`.
- A third-party compatibility proxy uses its own proxy-issued credential. It is not an official Tushare Pro token. Never send an official token to a third-party endpoint.
- The CLI does not call the OpenAI API and does not require an OpenAI API key.

See `README.md` for commands and `SKILL.md` for the full analysis policy.
