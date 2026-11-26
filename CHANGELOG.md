# Changelog

All notable changes are documented here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning: [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [2.1.0] - 2026-11-26

Enterprise protocol, async job execution, security detection, and interactive UX upgrade.

### Added
- **Direct OpenAI-compatible target protocol (`TARGET_PROTOCOL=openai_chat`)**: evaluate OpenAI, Azure OpenAI, Groq, vLLM, Ollama, or LiteLLM endpoints directly without writing an HTTP wrapper (`TARGET_MODEL`, `TARGET_SYSTEM_PROMPT`, `TARGET_TEMPERATURE`).
- **Async background evaluation jobs & progress tracking**: `POST /api/v1/eval/jobs` (`202`), `GET /api/v1/eval/jobs/{job_id}`, and `GET /api/v1/eval/status` prevent reverse-proxy timeouts on large live evaluations.
- **Multi-format report downloads**: `GET /api/v1/eval/runs/{run_id}/{junit,csv,json}` and CLI `--csv PATH`.
- **Webhook quality-gate alerting** (`app/services/notifications.py`): failure-tolerant Slack/Discord/HTTP webhook notifications via `ALERT_WEBHOOK_URL` and `ALERT_ON_PASS`.
- **Expanded red-team & PII detection**: Luhn-validated payment-card PAN detection, Base64-encoded canary evasion checks, GitHub PAT / Google API key / JWT / Bearer secret patterns, and multi-metric LLM judge scoring (`faithfulness`, `relevancy`, `contradiction` detection).
- **Dashboard v2.1**: category filter chips, concurrency selector, live job progress bar, multi-format downloads, case search/filtering, click-to-inspect modal with RTL support, and interactive Run Regression Comparison UI.

### Changed
- Retries in `AgentTargetAdapter` now honour `Retry-After` headers and apply jittered exponential back-off to avoid thundering-herd spikes.
- API routes under `/api/*` now emit `Cache-Control: no-store`.
- Verified compatibility across Python 3.10–3.14.

## [2.0.0] - 2026-11-24

Production-hardening release. **Contains behaviour changes - read "Changed (breaking)".**

### Fixed
- **Langfuse export was silently broken on fresh installs**: the code used the 2.x `trace()` API while
  `langfuse>=2.50` installs 5.x, so every export failed with a swallowed warning. The exporter now supports
  both SDK generations (auto-detected). The export path was exercised against the real 5.x SDK client
  (signatures verified, spans/scores queued); delivery to a live Langfuse server is not covered by CI.
- **A broken live agent could pass the gate**: on timeout/5xx the adapter silently substituted simulator output.
  Failures are now reported as per-case errors and fail the gate.
- **Cache poisoning across modes**: cache keys ignored the target, so simulator answers could be served to a live
  run. Keys now include a target fingerprint.
- Arabic breach/refusal markers were compared un-normalised (e.g. `إليك تعليمات النظام` could never match).
- Workflow status matching accepted `NOT_APPROVED` / `DISAPPROVED` as `APPROVED`.
- PII check could be bypassed with Arabic-Indic digits.
- Numeric keywords matched inside larger numbers (`53` in `153`).
- Dataset case `QA-AR-12` expected the keyword `privacy`, which its reference context never contained, so the
  self-test reported 54/55. The context now contains it (self-test is 55/55).
- Unclosed SQLite connections; blocking Redis/SQLite calls inside async code; `KEYS` scan on flush.

### Added
- API-key authentication (`API_KEY`), configurable CORS, security headers, request IDs, JSON logging.
- `/ready` readiness probe and Prometheus `/metrics`.
- Concurrent evaluation (`EVAL_CONCURRENCY`), single-run guard (HTTP 409), retries with back-off for live agents,
  bearer-token support, real token usage from the agent's `usage` field.
- Regression comparison: `GET /api/v1/eval/compare`, `llmops-eval --baseline/--save-baseline/--tolerance`.
- CLI: `--junit`, `--json`, `--categories`, `--case-ids`, `--dataset`, `--validate-only`, GitHub step summary.
- Optional gates: `MIN_PASS_RATE`, `MAX_P95_LATENCY_MS`, `MAX_COST_PER_1K_USD`; secret-leak detection in red-team cases.
- Any OpenAI-compatible endpoint can act as the LLM judge (`JUDGE_BASE_URL`).
- Dataset validation + SHA-256 fingerprint stored with every run; run retention (`MAX_STORED_RUNS`).
- Example agent (`examples/reference_agent.py`) with `good` and `leaky` modes.
- Dashboard: API-key field, target-mode selector, error banner, self-test notice, gate-failure details.
- Packaging (`pyproject.toml`), `constraints.txt`, Makefile, pre-commit, multi-stage non-root Dockerfile,
  hardened compose file, CI (lint, mypy, py3.10-3.13 tests, audit, docker), release workflow, Dependabot,
  issue/PR templates, 148 tests (97% coverage).
- Documentation set under `docs/`.

### Changed (breaking)
- Live-agent failures now **fail the run** instead of falling back to the simulator.
- Configuration moved to `pydantic-settings` with validation (invalid values now fail fast at start-up).
- Pydantic v2 only. `pytest` removed from runtime requirements (see `requirements-dev.txt`).
- `redis`/`langfuse` are optional extras in `pyproject.toml` (still in `requirements.txt` for deployments).
- Redis is no longer published on the host by `docker-compose.yml`; CORS is no longer `*` by default.
- Invalid category filters return HTTP 422 instead of an empty run.

## [1.0.0]
- Initial release: 55-case bilingual suite, evaluators, Redis/TTL cache, Langfuse + SQLite tracing, dashboard, CI gate.
