# Security

## Credentials

The default AkShare workflow does not require credentials.

The optional third-party Tushare proxy requires `TUSHARE_TOKEN`. It uses HTTP transport and is not an official Tushare endpoint. Treat the token and all returned data as visible to the proxy operator.

- Keep credentials in process environment variables or enter them through the hidden prompt.
- Never commit `.env`, tokens, proxy health files, raw logs, holdings, caches, or generated reports containing private portfolio data.
- Do not paste a token into an Issue, pull request, screenshot, report, or shell command that will be preserved in history.
- Rotate any credential that has been exposed.
- Review the proxy operator's authorization and data-use terms before use.

The code records only a short token fingerprint for local health correlation. It must never serialize the original token.

## Reporting a vulnerability

Open a GitHub Issue that describes the affected component and reproduction without including credentials or private portfolio data. For a credential leak, revoke or rotate the credential first.
