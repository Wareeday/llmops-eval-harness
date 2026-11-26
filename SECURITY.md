# Security Policy

## Supported versions

Security fixes are released for the latest minor version (currently **2.x**).

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Use GitHub's private reporting: **Security -> Advisories -> Report a vulnerability** on this repository.
Include the affected version (`GET /health`), reproduction steps and the impact you observed.

What to expect: acknowledgement within **3 working days**, an initial assessment within **7 days**, and
coordinated disclosure once a fix is released. Reporters are credited in the release notes unless they
prefer otherwise.

## Security model (what this project does and does not protect)

| Area | Behaviour |
| :--- | :--- |
| Authentication | Optional API key (`API_KEY`) on `/api/v1/*`, compared in constant time. `/health`, `/ready`, `/metrics` and the dashboard shell stay open for probes - restrict `/metrics` at the network level. |
| Secrets | API keys, Redis URL, Langfuse, judge keys, and `ALERT_WEBHOOK_URL` are `SecretStr`: never in `repr()`, logs or the `/health` response. `/api/*` responses set `Cache-Control: no-store`. |
| CORS | Disabled unless `CORS_ALLOW_ORIGINS` is set; credentials are never allowed. |
| Untrusted agent output | Rendered with HTML escaping in the dashboard, truncated to 20 000 characters, checked for plaintext/Base64 leaks + Luhn-valid payment cards + credentials, and passed to the optional LLM judge inside a hardened prompt that treats it as data. |
| SSRF | Target URLs come from server-side configuration only - no request parameter can redirect the harness to another host. |
| Abuse | One evaluation run at a time (HTTP 409), bounded concurrency, bounded cache memory, bounded history. |
| Container | Non-root user, read-only root filesystem and dropped capabilities in `docker-compose.yml`. Redis is not published to the host. |

Out of scope: the harness cannot make *your agent* secure - it measures it. Its built-in heuristics are
regression detectors, not proof of safety (see `docs/METRICS.md`).

## Hardening checklist for production

1. Set a strong `API_KEY` and terminate TLS in front of the service (Caddy, nginx, a cloud load balancer).
2. Use `ENVIRONMENT=production`, `LOG_FORMAT=json`; ship logs somewhere durable.
3. Keep Redis private (internal network / VPC / Upstash with TLS `rediss://`).
4. Do not put real customer PII into datasets or reports - use synthetic identifiers.
5. Pin dependencies with `constraints.txt`, enable Dependabot and the `pip-audit` CI job.
