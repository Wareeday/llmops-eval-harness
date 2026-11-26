from __future__ import annotations

from collections.abc import Callable

import pytest
from app.core.config import Settings
from app.main import create_app
from app.services.runner import RunInProgressError
from fastapi.testclient import TestClient

SettingsFactory = Callable[..., Settings]


def _run(client: TestClient, **body: object) -> dict:
    payload = {"use_cache": False, "target_mode": "standalone", **body}
    resp = client.post("/api/v1/eval/run", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_health_and_dataset(client: TestClient) -> None:
    health = client.get("/health").json()
    assert health["status"] == "ok" and health["dataset_cases_loaded"] == 55
    assert health["cache_backend"] == "memory_ttl" and health["auth_enabled"] is False
    assert len(client.get("/api/v1/eval/dataset").json()) == 55


def test_readiness_ok_and_degraded(client: TestClient) -> None:
    ok = client.get("/ready")
    assert ok.status_code == 200 and ok.json()["ready"] is True

    async def down() -> bool:
        return False

    client.app.state.runner.cache.ping = down  # type: ignore[attr-defined]
    bad = client.get("/ready")
    assert bad.status_code == 503 and bad.json()["checks"]["cache"] is False


def test_cold_and_warm_runs_with_quality_gate(client: TestClient) -> None:
    cold = _run(client, use_cache=True)
    assert cold["total_cases"] == cold["passed_cases"] == 55
    assert cold["quality_gate_passed"] and cold["cache_hit_rate"] == 0.0
    warm = _run(client, use_cache=True)
    assert (
        warm["cache_hit_rate"] == 1.0
        and warm["total_tokens"] == 0
        and warm["total_cost_usd"] == 0.0
    )
    assert warm["p95_latency_ms"] < cold["p95_latency_ms"]


def test_run_history_detail_markdown_and_traces(client: TestClient) -> None:
    run = _run(client, categories=["pii_privacy", "prompt_injection"])
    runs = client.get("/api/v1/eval/runs").json()
    assert [r["run_id"] for r in runs] == [run["run_id"]]
    assert client.get(f"/api/v1/eval/runs/{run['run_id']}").json()["total_cases"] == 20

    md = client.get(f"/api/v1/eval/runs/{run['run_id']}/markdown")
    assert md.status_code == 200 and "LLMOps Automated Evaluation Report" in md.text
    assert "attachment" in md.headers["content-disposition"]
    assert md.headers.get("cache-control") == "no-store"

    junit = client.get(f"/api/v1/eval/runs/{run['run_id']}/junit")
    assert junit.status_code == 200 and "<testsuites" in junit.text
    csv_resp = client.get(f"/api/v1/eval/runs/{run['run_id']}/csv")
    assert csv_resp.status_code == 200 and "case_id,category" in csv_resp.text
    json_resp = client.get(f"/api/v1/eval/runs/{run['run_id']}/json")
    assert json_resp.status_code == 200 and json_resp.json()["run_id"] == run["run_id"]

    assert len(client.get("/api/v1/traces?limit=50").json()) == 20
    assert len(client.get(f"/api/v1/traces?run_id={run['run_id']}&limit=5").json()) == 5
    assert client.get("/api/v1/traces?run_id=other").json() == []


def test_compare_endpoint(client: TestClient) -> None:
    a, b = _run(client), _run(client)
    report = client.get(
        "/api/v1/eval/compare", params={"base": a["run_id"], "head": b["run_id"]}
    ).json()
    assert report["regression_detected"] is False and report["newly_failing_cases"] == []
    missing = client.get("/api/v1/eval/compare", params={"base": a["run_id"], "head": "nope"})
    assert missing.status_code == 404
    assert (
        client.get(
            "/api/v1/eval/compare", params={"base": "a", "head": "b", "tolerance": 5}
        ).status_code
        == 422
    )


@pytest.mark.parametrize(
    ("method", "path", "body", "status"),
    [
        ("get", "/api/v1/eval/runs/unknown", None, 404),
        ("get", "/api/v1/eval/runs/unknown/markdown", None, 404),
        ("get", "/api/v1/eval/runs/unknown/junit", None, 404),
        ("get", "/api/v1/eval/runs/unknown/csv", None, 404),
        ("get", "/api/v1/eval/runs/unknown/json", None, 404),
        ("get", "/api/v1/eval/jobs/unknown", None, 404),
        ("get", "/api/v1/eval/runs?limit=0", None, 422),
        ("get", "/api/v1/traces?limit=9999", None, 422),
        ("post", "/api/v1/eval/run", {"categories": ["nonsense"]}, 422),
        ("post", "/api/v1/eval/run", {"concurrency": 0}, 422),
        ("post", "/api/v1/eval/run", {"case_ids": ["NOPE"]}, 422),
        ("post", "/api/v1/eval/run", {"target_mode": "live"}, 422),  # no endpoint configured
        ("post", "/api/v1/cache/test", {"query": "x"}, 422),
    ],
)
def test_error_responses(
    client: TestClient, method: str, path: str, body: dict | None, status: int
) -> None:
    resp = getattr(client, method)(path, **({"json": body} if body is not None else {}))
    assert resp.status_code == status
    assert resp.json()["detail"]


def test_concurrent_run_returns_409(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def busy(_: object) -> None:
        raise RunInProgressError("An evaluation run is already in progress.")

    monkeypatch.setattr(client.app.state.runner, "run_suite", busy)  # type: ignore[attr-defined]
    resp = client.post("/api/v1/eval/run", json={})
    assert resp.status_code == 409 and "already in progress" in resp.json()["detail"]


def test_cache_probe_stats_and_flush(client: TestClient) -> None:
    q = "What is the probation period under Article 53?"
    first = client.post("/api/v1/cache/test", json={"query": q, "use_cache": True}).json()
    second = client.post("/api/v1/cache/test", json={"query": q, "use_cache": True}).json()
    assert (
        first["cache_hit"] is False and second["cache_hit"] is True and second["tokens_used"] == 0
    )
    stats = client.get("/api/v1/cache/stats").json()
    assert stats["hits"] >= 1 and stats["cached_keys"] == 1
    assert client.post("/api/v1/cache/flush").json() == {"status": "flushed", "cleared_keys": 1}


def test_prometheus_metrics(client: TestClient) -> None:
    _run(client, case_ids=["QA-EN-01"])
    resp = client.get("/metrics")
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/plain")
    assert (
        "llmops_eval_cases_total" in resp.text and "llmops_eval_run_duration_seconds" in resp.text
    )


def test_security_headers_and_request_id(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert len(resp.headers["x-request-id"]) == 16
    assert (
        client.get("/health", headers={"X-Request-ID": "trace-123"}).headers["x-request-id"]
        == "trace-123"
    )
    hostile = client.get("/health", headers={"X-Request-ID": "bad id\twith spaces"}).headers[
        "x-request-id"
    ]
    assert hostile != "bad id\twith spaces" and len(hostile) == 16


def test_dashboard_is_served(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200 and "LLMOps" in resp.text


def test_async_eval_job_lifecycle_and_status(client: TestClient) -> None:
    import time

    assert client.get("/api/v1/eval/status").json()["is_running"] is False
    started = client.post(
        "/api/v1/eval/jobs",
        json={"use_cache": False, "target_mode": "standalone", "case_ids": ["QA-EN-01", "RED-01"]},
    )
    assert started.status_code == 202
    job = started.json()
    assert job["total_cases"] == 2
    for _ in range(40):
        poll = client.get(f"/api/v1/eval/jobs/{job['job_id']}").json()
        if poll["status"] == "completed":
            break
        time.sleep(0.02)
    assert poll["status"] == "completed" and poll["summary"]["passed_cases"] == 2
    assert poll["run_id"] and client.get("/api/v1/eval/status").json()["is_running"] is False


# --------------------------------------------------------------------------- authentication
@pytest.fixture
def secured(settings_factory: SettingsFactory) -> TestClient:
    with TestClient(create_app(settings_factory(api_key="s3cret-key"))) as c:
        return c


def test_api_requires_key_when_configured(secured: TestClient) -> None:
    protected = [
        "/api/v1/eval/dataset",
        "/api/v1/eval/runs",
        "/api/v1/cache/stats",
        "/api/v1/traces",
    ]
    for path in protected:
        resp = secured.get(path)
        assert resp.status_code == 401 and resp.headers["www-authenticate"] == "ApiKey"
        assert secured.get(path, headers={"X-API-Key": "wrong"}).status_code == 401
        assert secured.get(path, headers={"X-API-Key": "s3cret-key"}).status_code == 200
    assert secured.post("/api/v1/eval/run", json={}).status_code == 401
    assert secured.post("/api/v1/cache/flush").status_code == 401


def test_operational_endpoints_stay_open(secured: TestClient) -> None:
    assert secured.get("/health").json()["auth_enabled"] is True
    assert secured.get("/ready").status_code == 200
    assert secured.get("/metrics").status_code == 200
    assert secured.get("/").status_code == 200


# --------------------------------------------------------------------------- CORS
def test_cors_disabled_by_default(client: TestClient) -> None:
    resp = client.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in resp.headers


def test_cors_allows_only_configured_origins(settings_factory: SettingsFactory) -> None:
    with TestClient(create_app(settings_factory(cors_allow_origins="https://ui.example"))) as c:
        ok = c.options(
            "/api/v1/eval/run",
            headers={"Origin": "https://ui.example", "Access-Control-Request-Method": "POST"},
        )
        assert ok.headers["access-control-allow-origin"] == "https://ui.example"
        assert "access-control-allow-credentials" not in ok.headers
        denied = c.options(
            "/api/v1/eval/run",
            headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
        )
        assert "access-control-allow-origin" not in denied.headers
