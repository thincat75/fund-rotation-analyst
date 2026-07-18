# Security

## Credentials

The default AkShare workflow does not require credentials.

Official Tushare Pro and a third-party compatibility proxy use different credentials:

- `TUSHARE_PROVIDER=official` expects a token issued by `tushare.pro`. Do not set `TUSHARE_HTTP_URL`; the code uses the official SDK endpoint without a private-field override.
- `TUSHARE_PROVIDER=third-party-proxy` expects a credential issued by that proxy. It uses HTTP transport and is not an official Tushare endpoint. Treat the credential and all returned data as visible to the proxy operator.
- Never send an official Tushare Pro token to a third-party endpoint.

- Keep credentials in process environment variables or enter them through the hidden prompt.
- Never commit `.env`, tokens, proxy health files, raw logs, holdings, caches, or generated reports containing private portfolio data.
- Do not paste a token into an Issue, pull request, screenshot, report, or shell command that will be preserved in history.
- Rotate any credential that has been exposed.
- Review the proxy operator's authorization and data-use terms before use.

The code records only a short token fingerprint for local health correlation. It must never serialize the original token.

## Reporting a vulnerability

Open a GitHub Issue that describes the affected component and reproduction without including credentials or private portfolio data. For a credential leak, revoke or rotate the credential first.
