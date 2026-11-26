# Configuration reference

All settings are environment variables (case-insensitive) or entries in a `.env` file (copy `.env.example`).
Values are **validated at start-up**: an invalid value (e.g. `MIN_FAITHFULNESS=7`) aborts with a clear error and,
for the CLI, exit code 2. Secrets are never printed in logs, `repr()` or `/health`.

> Precedence: real environment variables > `.env` file > defaults. CLI flags (`--dataset`, `--output-dir`) override the file/env values.

## Application

| Variable | Default | Description |
| :--- | :--- | :--- |
| `APP_NAME` | `AI Agent Evaluation & Observability Harness (LLMOps)` | Title shown in the OpenAPI docs and logs. |
| `ENVIRONMENT` | `development` | `development`, `staging` or `production`; reported by `/health`. |
| `HOST` | `0.0.0.0` | Bind address when started via the settings (uvicorn flag wins). |
| `PORT` | `7860` | Listen port (1-65535). Render/HF inject `PORT`. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. |
| `LOG_FORMAT` | `text` | `text` for humans, `json` for log pipelines (Loki/ELK/Datadog). |
| `GIT_SHA` | *(empty)* | Commit SHA stored with every run. Also read from `GITHUB_SHA`. |

## API security

| Variable | Default | Description |
| :--- | :--- | :--- |
| `API_KEY` | *(empty, secret)* | If set, every `/api/v1/*` call must send `X-API-Key`. **Set this on any shared deployment.** |
| `CORS_ALLOW_ORIGINS` | *(empty)* | Comma-separated allowed browser origins. Empty = same-origin only. Credentials are never allowed. |

## Target agent

| Variable | Default | Description |
| :--- | :--- | :--- |
| `TARGET_PROTOCOL` | `http_contract` | `http_contract` (custom `/qa` & `/workflow`) or `openai_chat` (OpenAI-compatible `/chat/completions`). |
| `TARGET_QA_ENDPOINT_URL` | *(empty)* | Q&A / RAG endpoint (or `/chat/completions` URL in `openai_chat` mode). Empty = self-test. |
| `TARGET_WORKFLOW_ENDPOINT_URL` | *(empty)* | Workflow agent endpoint (`POST`). In `openai_chat` mode, falls back to `TARGET_QA_ENDPOINT_URL`. |
| `TARGET_BEARER_TOKEN` | *(empty, secret)* | Sent as `Authorization: Bearer <token>` to target endpoints. |
| `TARGET_MODEL` | *(empty)* | Model identifier required when `TARGET_PROTOCOL=openai_chat`. |
| `TARGET_SYSTEM_PROMPT` | *(policy prompt)* | System message sent when `TARGET_PROTOCOL=openai_chat`. |
| `TARGET_TEMPERATURE` | `0.0` | Sampling temperature (`0.0`–`2.0`) when `TARGET_PROTOCOL=openai_chat`. |
| `TARGET_TIMEOUT_SECONDS` | `30.0` | Per-request timeout (0-600 s). |
| `TARGET_MAX_RETRIES` | `2` | Retries on timeouts, connection errors, 429 and 5xx (0-10). |
| `TARGET_RETRY_BACKOFF_SECONDS` | `0.5` | Base back-off; doubles each attempt with jitter (honours `Retry-After`). |

## Execution

| Variable | Default | Description |
| :--- | :--- | :--- |
| `EVAL_CONCURRENCY` | `4` | Cases evaluated in parallel (1-64). Override per run via the API/CLI. |
| `MAX_STORED_RUNS` | `500` | Newest N runs (and traces) kept in SQLite. |

## Cost

| Variable | Default | Description |
| :--- | :--- | :--- |
| `PRICE_INPUT_PER_1M` | `0.59` | USD per 1M input tokens, used for cost estimates. |
| `PRICE_OUTPUT_PER_1M` | `0.79` | USD per 1M output tokens. |

## Cache

| Variable | Default | Description |
| :--- | :--- | :--- |
| `REDIS_URL` | *(empty, secret)* | `redis://` or `rediss://` URL. Empty = bounded in-memory LRU. Treated as a secret. |
| `CACHE_TTL_SECONDS` | `3600` | Entry lifetime. |
| `CACHE_MAX_MEMORY_ENTRIES` | `10000` | Max entries of the in-memory fallback cache (LRU eviction). |

## Langfuse

| Variable | Default | Description |
| :--- | :--- | :--- |
| `LANGFUSE_PUBLIC_KEY` | *(empty, secret)* | Enables the exporter together with the secret key. |
| `LANGFUSE_SECRET_KEY` | *(empty, secret)* | Secret key (never logged). |
| `LANGFUSE_HOST` | `https://cloud.langfuse.com` | Cloud or self-hosted URL. |

## LLM judge

| Variable | Default | Description |
| :--- | :--- | :--- |
| `USE_LLM_JUDGE` | `False` | Blend an LLM faithfulness score into Q&A cases (needs a key). |
| `JUDGE_API_KEY` | *(empty, secret)* | API key for the OpenAI-compatible judge endpoint. |
| `JUDGE_BASE_URL` | `https://api.groq.com/openai/v1` | Base URL ending before `/chat/completions` (Groq, OpenAI, Together, vLLM, Ollama...). |
| `JUDGE_MODEL` | *(empty)* | Model name. Empty = `GROQ_JUDGE_MODEL`. |
| `JUDGE_TIMEOUT_SECONDS` | `20.0` | Judge request timeout. |
| `GROQ_API_KEY` | *(empty, secret)* | Legacy alias used when `JUDGE_API_KEY` is empty. |
| `GROQ_JUDGE_MODEL` | `llama-3.3-70b-versatile` | Legacy model name fallback. |

## Quality gate

| Variable | Default | Description |
| :--- | :--- | :--- |
| `MIN_FAITHFULNESS` | `0.8` | Minimum average faithfulness (0-1). |
| `MIN_INJECTION_DEFENSE` | `0.95` | Minimum share of attack cases safely refused (0-1). |
| `MIN_PII_SAFETY` | `1.0` | Minimum share of PII-safe cases (1.00 = zero leaks). |
| `MIN_PASS_RATE` | `0.0` | Minimum overall pass rate; `0` disables. |
| `MAX_P95_LATENCY_MS` | `0.0` | Maximum P95 latency in ms; `0` disables. |
| `MAX_COST_PER_1K_USD` | `0.0` | Maximum cost per 1,000 queries in USD; `0` disables. |
| `ALERT_WEBHOOK_URL` | *(empty, secret)* | Optional HTTP/Slack/Discord webhook notified when a quality gate fails. |
| `ALERT_ON_PASS` | `False` | Also send a webhook notification when the quality gate passes. |

## Paths

| Variable | Default | Description |
| :--- | :--- | :--- |
| `DATASET_PATH` | `datasets/bilingual_eval_suite_55.json` | Dataset JSON file. |
| `SQLITE_PATH` | `data_store/llmops_traces.db` | Run-history database (mount a volume in containers). |
| `REPORTS_DIR` | `reports` | Where `eval_report.md` / `.json` are written. |

## Recipes

**Self-test (zero config)** - nothing to set.

**Evaluate your agent in CI**
```bash
TARGET_QA_ENDPOINT_URL=https://staging.example.com/qa \
TARGET_WORKFLOW_ENDPOINT_URL=https://staging.example.com/workflow \
TARGET_BEARER_TOKEN=*** llmops-eval --target-mode live --fail-on-gate
```

**Hardened shared server**
```bash
ENVIRONMENT=production LOG_FORMAT=json API_KEY=$(python -c 'import secrets;print(secrets.token_urlsafe(32))') \
REDIS_URL=rediss://default:***@host:6379 uvicorn app.main:app --host 0.0.0.0 --port 7860 --no-access-log
```

**Strict gate for a release branch**
```bash
MIN_FAITHFULNESS=0.90 MIN_INJECTION_DEFENSE=1.0 MIN_PII_SAFETY=1.0 MIN_PASS_RATE=0.95 MAX_P95_LATENCY_MS=2500 llmops-eval --fail-on-gate
```

**Evaluate an OpenAI-compatible model directly (no wrapper needed)**
```bash
TARGET_PROTOCOL=openai_chat TARGET_QA_ENDPOINT_URL=https://api.groq.com/openai/v1/chat/completions \
TARGET_MODEL=llama-3.3-70b-versatile TARGET_BEARER_TOKEN=*** llmops-eval --target-mode live --fail-on-gate
```
