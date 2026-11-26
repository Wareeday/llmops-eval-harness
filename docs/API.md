# REST API reference

Interactive docs (always in sync with the code): **`/docs`** (Swagger UI) and **`/openapi.json`**.

Base URL in examples: `http://localhost:7860`. When `API_KEY` is set, add `-H "X-API-Key: $API_KEY"` to every
`/api/v1/*` call. A missing/wrong key returns `401` with `WWW-Authenticate: ApiKey`.

## Endpoints

| Method | Path | Auth | Purpose |
| :--- | :--- | :---: | :--- |
| GET | `/health` | - | Liveness + non-secret configuration summary |
| GET | `/ready` | - | Readiness: SQLite, cache and dataset checks (`503` if degraded) |
| GET | `/metrics` | - | Prometheus metrics (restrict at the network level) |
| GET | `/` | - | Dashboard |
| GET | `/api/v1/eval/dataset` | key | The loaded benchmark cases |
| POST | `/api/v1/eval/run` | key | Run the suite synchronously and return the full summary |
| POST | `/api/v1/eval/jobs` | key | Start an async background run (`202` + `job_id`) |
| GET | `/api/v1/eval/jobs/{job_id}` | key | Poll async job status, case progress and final summary |
| GET | `/api/v1/eval/status` | key | Live runner lock & case progress state |
| GET | `/api/v1/eval/runs?limit=20` | key | Run history (newest first, `limit` 1-100) |
| GET | `/api/v1/eval/runs/{run_id}` | key | One run with per-case results |
| GET | `/api/v1/eval/runs/{run_id}/markdown` | key | Markdown report (download) |
| GET | `/api/v1/eval/runs/{run_id}/junit` | key | JUnit XML report (download) |
| GET | `/api/v1/eval/runs/{run_id}/csv` | key | Per-case CSV export (download) |
| GET | `/api/v1/eval/runs/{run_id}/json` | key | Full JSON report (download) |
| GET | `/api/v1/eval/compare?base=&head=&tolerance=0.02` | key | Regression diff of two runs |
| POST | `/api/v1/cache/test` | key | Probe one query through the cache (cold vs warm) |
| GET | `/api/v1/cache/stats` | key | Hits, misses, hit rate, keys, errors |
| POST | `/api/v1/cache/flush` | key | Delete all cache entries |
| GET | `/api/v1/traces?limit=100&run_id=` | key | Per-case traces (`limit` 1-300) |

## Run an evaluation

`POST /api/v1/eval/run`
```json
{
  "use_cache": false,
  "target_mode": "auto",
  "categories": ["prompt_injection", "pii_privacy"],
  "case_ids": null,
  "concurrency": 4
}
```
| Field | Default | Notes |
| :--- | :--- | :--- |
| `use_cache` | `true` | `false` forces every case to hit the target (cold run). |
| `target_mode` | `auto` | `standalone` = built-in reference agent (self-test); `live` = your endpoints (`422` if none configured); `auto` = live when the endpoint for that case type is configured. |
| `categories` | all | Any of `qa_en`, `qa_ar`, `pii_privacy`, `prompt_injection`, `structured_workflow`. Unknown values -> `422`. |
| `case_ids` | all | Restrict to specific IDs. No match -> `422`. |
| `concurrency` | `EVAL_CONCURRENCY` | 1-64. |

```bash
curl -s -X POST http://localhost:7860/api/v1/eval/run \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"use_cache": false, "target_mode": "standalone"}' | jq '{run_id, passed_cases, total_cases, quality_gate_passed, quality_gate_reasons, p95_latency_ms}'
```
```json
{ "run_id": "run_ab12cd34ef", "passed_cases": 55, "total_cases": 55, "quality_gate_passed": true,
  "quality_gate_reasons": [], "p95_latency_ms": 13.17 }
```

### Response fields (`EvalRunSummary`)
`run_id`, `timestamp`, `app_version`, `git_sha`, `dataset_sha256`, `target_mode`, `effective_target`
(`standalone` | `live` | `mixed`), `cache_enabled`, `cache_backend`, `total_cases`, `passed_cases`, `error_cases`,
`overall_pass_rate`, `avg_faithfulness`, `avg_relevancy`, `injection_defense_rate`, `pii_safety_rate`,
`cache_hit_rate`, `p50_latency_ms`, `p95_latency_ms`, `avg_latency_ms`, `duration_seconds`, `concurrency`,
`total_tokens`, `total_cost_usd`, `cost_per_1k_queries_usd`, `quality_gate_passed`, `quality_gate_reasons[]`,
`category_breakdown[]`, `results[]` (per case: scores, `passed`, `notes`, `cache_hit`, `target_source`, `error`,
`latency_ms`, token counts, `actual_output`).

## Compare two runs (regression check)
```bash
curl -s "http://localhost:7860/api/v1/eval/compare?base=run_aaa&head=run_bbb&tolerance=0.02" -H "X-API-Key: $API_KEY"
```
Returns `metrics[]` (base/head/delta per metric), `newly_failing_cases[]`, `newly_passing_cases[]`,
`regressions[]` (human-readable) and `regression_detected`.

## Errors
| Status | When |
| :---: | :--- |
| 401 | `API_KEY` is configured and the header is missing or wrong |
| 404 | Unknown `run_id` |
| 409 | Another run is already executing in this process |
| 422 | Validation error, unknown category, no cases matched, or `live` mode without a configured endpoint |
| 500 | Dataset file invalid at runtime (details in `detail`) |
| 503 | `/ready` only: a dependency is down |

Every response carries `X-Request-ID` (send your own to correlate logs) and security headers.
Error bodies are `{"detail": "..."}`.

## Prometheus metrics
| Metric | Type | Labels |
| :--- | :--- | :--- |
| `llmops_eval_runs_total` | counter | `gate` = `pass` \| `fail` |
| `llmops_eval_cases_total` | counter | `category`, `outcome` = `pass` \| `fail` \| `error` |
| `llmops_cache_lookups_total` | counter | `result` = `hit` \| `miss` |
| `llmops_eval_run_duration_seconds` | histogram | - |
| `llmops_last_run_metric` | gauge | `metric` = `overall_pass_rate`, `avg_faithfulness`, `avg_relevancy`, `injection_defense_rate`, `pii_safety_rate`, `cache_hit_rate`, `p95_latency_ms`, `cost_per_1k_queries_usd`, `quality_gate_passed` |

Example alert: `llmops_last_run_metric{metric="quality_gate_passed"} == 0` for 1 run.

## CLI reference (`llmops-eval`, alias `python -m app.cli`, legacy `python cli_eval.py`)

| Flag | Meaning |
| :--- | :--- |
| `--no-cache` | Cold benchmark (every case hits the target) |
| `--target-mode {auto,standalone,live}` | See above |
| `--categories C [C ...]`, `--case-ids ID [ID ...]` | Subset of the suite |
| `--dataset PATH`, `--output-dir DIR`, `--concurrency N` | Overrides |
| `--junit PATH` | JUnit XML (one suite per category) |
| `--csv PATH` | Per-case CSV spreadsheet report |
| `--baseline PATH`, `--tolerance 0.02` | Compare with a saved `eval_report.json` |
| `--save-baseline PATH` | Write this run's JSON as the new baseline |
| `--json` | Print the full summary as JSON instead of Markdown |
| `--validate-only` | Validate the dataset and exit |
| `--fail-on-gate` | Exit `1` if the gate or the regression check fails |
| `--version` | Print the version |

Exit codes: `0` success - `1` gate/regression failed (only with `--fail-on-gate`) - `2` configuration error
(invalid env, bad dataset/baseline, live mode without endpoint, no cases matched).
When `GITHUB_STEP_SUMMARY` is set the Markdown report is appended to the Actions job summary automatically.
