# Deployment guide

Pick the path that matches your goal:

| Goal | Path | Time |
| :--- | :--- | :--- |
| Try it / develop | [Local venv](#1-local-python) | 2 min |
| Run a shared server with Redis | [Docker Compose](#2-docker-compose) | 3 min |
| Free public demo | [Render](#3-render-free) or [Hugging Face Spaces](#4-hugging-face-spaces) | 5 min |
| Gate every PR in CI | [GitHub Actions](#5-github-actions-quality-gate) | 5 min |
| Kubernetes | [Manifest](#6-kubernetes) | 10 min |

> **Before exposing the service to anyone you do not trust:** set `API_KEY`, put it behind TLS, set
> `ENVIRONMENT=production` and `LOG_FORMAT=json`. See `SECURITY.md`.
> **Heads-up:** the Docker image build is exercised by the `docker` CI job; it could not be run in the environment this
> release was authored in, so run `make docker-build` once and check `docker compose up` before relying on it.

## 1. Local Python
```bash
python -m venv .venv && source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt -c constraints.txt       # add requirements-dev.txt to develop
cp .env.example .env                                     # optional - works with zero config
uvicorn app.main:app --reload --port 7860                # http://localhost:7860  (docs: /docs)
python -m app.cli --no-cache --fail-on-gate              # CLI gate
```

## 2. Docker Compose
```bash
cp .env.example .env            # set API_KEY, TARGET_* ...
docker compose up --build -d    # harness + Redis (Redis is NOT published to the host)
open http://localhost:7860

docker compose --profile demo up --build        # + agent-good / agent-leaky on :9000 (internal network)
docker compose --profile monitoring up -d       # + Prometheus on :9090 scraping /metrics
docker compose down             # stop (volumes keep history); add -v to wipe data
```
Hardening already applied: non-root user, read-only root filesystem, `cap_drop: ALL`, `no-new-privileges`,
Redis `maxmemory` + `allkeys-lru`, health-gated start-up.

Evaluate the demo agents from inside the compose network:
```bash
docker compose run --rm \
  -e TARGET_QA_ENDPOINT_URL=http://agent-leaky:9000/qa \
  -e TARGET_WORKFLOW_ENDPOINT_URL=http://agent-leaky:9000/workflow \
  harness python -m app.cli --target-mode live --fail-on-gate     # exit 1: the leaky agent is caught
```

## 3. Render (free)
1. Push the repo to GitHub.
2. Render -> **New -> Blueprint** -> select the repo (`render.yaml` is picked up).
3. Open the service -> **Environment**: copy the generated `API_KEY` (paste it into the dashboard's key field), and
   optionally set `REDIS_URL`, `LANGFUSE_*`, `TARGET_*`.
4. Free instances sleep and have an ephemeral disk: run history resets on restart. Use CI artifacts or a paid disk
   for persistence.

## 4. Hugging Face Spaces
Create a **Docker** Space, push the repo (the `Dockerfile` listens on 7860 as UID 1000, as Spaces require). Add
secrets (`API_KEY`, `REDIS_URL`, `TARGET_*`) in *Settings -> Variables and secrets*. Spaces disks are ephemeral.

## 5. GitHub Actions quality gate
Two workflows ship in `.github/workflows/`:

- `ci.yml` - ruff, format check, mypy (strict), tests on Python 3.10-3.13 with a 90% coverage floor, `pip-audit`,
  Docker build + smoke test.
- `llmops-eval-ci.yml` - the **quality gate**:
  - Every push/PR: self-test gate (+ regression check when `baselines/main.json` exists) -> proves harness and dataset are healthy.
  - **Live gate** (manual run with *live = true*, or nightly when repository variable `LIVE_EVAL_ENABLED=true`):
    evaluates *your* agent. Add repository **secrets** `TARGET_QA_ENDPOINT_URL`, `TARGET_WORKFLOW_ENDPOINT_URL`,
    optionally `TARGET_BEARER_TOKEN`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `JUDGE_API_KEY`.

Make it a required check: *Settings -> Branches -> Branch protection -> Require status checks -> "Self-test gate"*.

To gate **your agent's** pull requests, call the harness from your agent's repo:
```yaml
- uses: actions/checkout@v4
  with: { repository: Wareeday/llmops-eval-harness, path: harness }
- run: pip install -r harness/requirements.txt -c harness/constraints.txt
- run: python -m app.cli --target-mode live --fail-on-gate --junit report.xml
  working-directory: harness
  env:
    TARGET_QA_ENDPOINT_URL: https://pr-${{ github.event.number }}.staging.example.com/qa
```
Releases: push a tag `vX.Y.Z` -> `release.yml` builds and pushes `ghcr.io/Wareeday/llmops-eval-harness:X.Y.Z` and creates a GitHub Release.

### Keeping a regression baseline
```bash
python -m app.cli --no-cache --save-baseline baselines/main.json   # commit this file
python -m app.cli --no-cache --baseline baselines/main.json --fail-on-gate
```
Refresh the baseline deliberately (in its own PR) when you *intend* behaviour to change.

## 6. Kubernetes
```yaml
apiVersion: apps/v1
kind: Deployment
metadata: { name: llmops-harness }
spec:
  replicas: 1                      # one replica: run lock + SQLite history are per-process
  selector: { matchLabels: { app: llmops-harness } }
  template:
    metadata:
      labels: { app: llmops-harness }
      annotations: { prometheus.io/scrape: "true", prometheus.io/port: "7860", prometheus.io/path: /metrics }
    spec:
      securityContext: { runAsNonRoot: true, runAsUser: 1000, fsGroup: 1000 }
      containers:
        - name: harness
          image: ghcr.io/Wareeday/llmops-eval-harness:2.1.0
          ports: [{ containerPort: 7860 }]
          envFrom: [{ secretRef: { name: llmops-harness-secrets } }]   # API_KEY, REDIS_URL, TARGET_*, LANGFUSE_*
          env:
            - { name: ENVIRONMENT, value: production }
            - { name: LOG_FORMAT, value: json }
          readinessProbe: { httpGet: { path: /ready, port: 7860 }, periodSeconds: 10 }
          livenessProbe:  { httpGet: { path: /health, port: 7860 }, periodSeconds: 20 }
          resources: { requests: { cpu: 100m, memory: 128Mi }, limits: { memory: 512Mi } }
          securityContext: { readOnlyRootFilesystem: true, allowPrivilegeEscalation: false, capabilities: { drop: [ALL] } }
          volumeMounts:
            - { name: data, mountPath: /app/data_store }
            - { name: reports, mountPath: /app/reports }
            - { name: tmp, mountPath: /tmp }
      volumes:
        - { name: data, persistentVolumeClaim: { claimName: llmops-harness-data } }
        - { name: reports, emptyDir: {} }
        - { name: tmp, emptyDir: {} }
```
Expose it through an Ingress with TLS; add a NetworkPolicy so only your CI/ingress reach `/api/v1`.

## 7. Reverse proxy with TLS (Caddy, simplest)
```
eval.example.com {
    reverse_proxy localhost:7860
}
```
Start uvicorn with `--proxy-headers` (already in the Docker CMD) so client IPs/schemes are honoured.

## 8. Free Redis and Langfuse
- **Redis (Upstash free)**: create a database -> copy the `rediss://default:<password>@<host>:6379` URL into `REDIS_URL`.
  `/health` then reports `cache_backend: redis`. Note a remote Redis adds a network round-trip to every cache hit
  (milliseconds, not microseconds) - still far cheaper than an LLM call.
- **Langfuse (cloud free tier or self-hosted)**: set `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST`.
  Each case becomes a trace with scores `faithfulness`, `relevancy`, `pii_safe`, `injection_safe`, `passed`.
  The exporter auto-detects SDK 2.x vs 3.x+. `/health` -> `langfuse_enabled: true`.
  Delivery to a live Langfuse server depends on your network/keys - if traces do not appear, check the container logs
  for `Langfuse` warnings (export problems never fail a run).

## 9. Backups, upgrades, operations
- **Backup**: copy `data_store/llmops_traces.db` (SQLite WAL: use `sqlite3 llmops_traces.db ".backup out.db"` for a hot copy).
- **Upgrade**: read `CHANGELOG.md`, pull the new image/tag, restart. SQLite schema changes are additive (`PRAGMA user_version`).
- **Dashboards/alerts**: scrape `/metrics`; alert on `llmops_last_run_metric{metric="quality_gate_passed"} == 0`.
- **Capacity**: 1 vCPU / 256 MB is plenty; time is dominated by your agent's latency x cases / `EVAL_CONCURRENCY`.

## 10. Troubleshooting

| Symptom | Likely cause | Fix |
| :--- | :--- | :--- |
| `401 Unauthorized` in the dashboard | `API_KEY` is set | Enter the key in the top-right field |
| `422 target_mode=live requires TARGET_QA_ENDPOINT_URL` | No endpoint configured | Set the URL or use `--target-mode standalone` |
| Gate fails with `Target errors: 55/55` | Agent unreachable / wrong URL / auth | `curl` the endpoint with the same body; check `TARGET_BEARER_TOKEN`; read logs |
| Dashboard banner "Self-test mode" | Using the built-in reference agent | Configure `TARGET_*` endpoints for a real evaluation |
| `409 An evaluation run is already in progress` | Another run is executing | Wait for it; one run per process at a time |
| `cache_backend: memory_ttl` though `REDIS_URL` is set | Redis unreachable at start-up (see warning in logs) | Check URL/TLS (`rediss://`), network, password |
| `langfuse_exported: false` on traces | Keys unset, wrong host, or SDK error | Check `LANGFUSE_*` and logs |
| Start-up fails with a validation error | Invalid env value | Read the message; see `docs/CONFIGURATION.md` |
| History vanished after redeploy | Ephemeral disk (Render free / HF) | Mount a persistent volume for `data_store/` |
| Faithfulness low on a good agent | No citations / different wording than `expected_keywords` | See `docs/METRICS.md` and tune the dataset |
