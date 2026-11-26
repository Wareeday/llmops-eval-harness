"""REST API v1: evaluation, caching, observability and operational endpoints."""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import PlainTextResponse

from app import __version__
from app.api.deps import get_runner, require_api_key
from app.models.schemas import (
    CacheStats,
    CacheTestRequest,
    ComparisonReport,
    EvalJobStatus,
    EvalRunRequest,
    EvalRunSummary,
    EvalTestCase,
    HealthResponse,
    ReadinessResponse,
    RunnerStatusResponse,
    TraceRecord,
)
from app.services.comparison import compare_runs
from app.services.reporting import render_csv, render_junit, render_markdown
from app.services.runner import EvaluationRunner
from app.services.telemetry import CONTENT_TYPE

Runner = Annotated[EvaluationRunner, Depends(get_runner)]

# --------------------------------------------------------------------------- open endpoints
system = APIRouter()


@system.get("/health", response_model=HealthResponse, tags=["System"])
async def health_check(request: Request, runner: Runner) -> HealthResponse:
    """Liveness + a summary of the active configuration (no secrets)."""
    cfg = runner.settings
    return HealthResponse(
        status="ok",
        version=__version__,
        environment=cfg.environment,
        cache_backend=runner.cache.backend,
        langfuse_enabled=runner.obs.langfuse_enabled,
        dataset_cases_loaded=len(runner.dataset),
        qa_endpoint_configured=bool(cfg.target_qa_endpoint_url),
        workflow_endpoint_configured=bool(cfg.target_workflow_endpoint_url),
        auth_enabled=bool(cfg.api_key.get_secret_value()),
        total_eval_runs=runner.obs.count_runs(),
    )


@system.get("/ready", response_model=ReadinessResponse, tags=["System"])
async def readiness(response: Response, runner: Runner) -> ReadinessResponse:
    """Readiness probe: the datastore and (if configured) Redis must be reachable."""
    checks = {
        "sqlite": runner.obs.ping(),
        "cache": await runner.cache.ping(),
        "dataset": len(runner.dataset) > 0,
    }
    ready = all(checks.values())
    if not ready:
        response.status_code = 503
    return ReadinessResponse(ready=ready, checks=checks)


@system.get("/metrics", include_in_schema=False)
async def prometheus_metrics(runner: Runner) -> Response:
    """Prometheus scrape endpoint. Restrict at the network level in production."""
    return Response(content=runner.telemetry.render(), media_type=CONTENT_TYPE)


# --------------------------------------------------------------------------- protected API
router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])


@router.get("/eval/dataset", response_model=list[EvalTestCase], tags=["Evaluation"])
async def get_dataset(runner: Runner) -> list[EvalTestCase]:
    """Return the loaded benchmark suite."""
    return runner.dataset


@router.post("/eval/run", response_model=EvalRunSummary, tags=["Evaluation"])
async def run_evaluation_suite(payload: EvalRunRequest, runner: Runner) -> EvalRunSummary:
    """Execute the suite. Returns 409 if a run is already in progress."""
    return await runner.run_suite(payload)


@router.get("/eval/status", response_model=RunnerStatusResponse, tags=["Evaluation"])
async def get_eval_status(runner: Runner) -> RunnerStatusResponse:
    """Return whether an evaluation is currently executing and its case progress."""
    return runner.get_status()


@router.post(
    "/eval/jobs",
    response_model=EvalJobStatus,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["Evaluation"],
)
async def start_evaluation_job(payload: EvalRunRequest, runner: Runner) -> EvalJobStatus:
    """Start an evaluation asynchronously in the background and return a job handle."""
    return runner.start_job(payload)


@router.get("/eval/jobs/{job_id}", response_model=EvalJobStatus, tags=["Evaluation"])
async def get_evaluation_job(job_id: str, runner: Runner) -> EvalJobStatus:
    """Poll the progress and summary of a background evaluation job."""
    job = runner.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    return job


@router.get("/eval/runs", tags=["Evaluation"])
async def list_eval_runs(
    runner: Runner, limit: Annotated[int, Query(ge=1, le=100)] = 20
) -> list[dict[str, Any]]:
    """Historical runs (newest first) for regression tracking."""
    return runner.obs.list_runs(limit=limit)


@router.get("/eval/compare", response_model=ComparisonReport, tags=["Evaluation"])
async def compare_eval_runs(
    runner: Runner,
    base: str,
    head: str,
    tolerance: Annotated[float, Query(ge=0, le=1)] = 0.02,
) -> ComparisonReport:
    """Diff two stored runs and flag quality regressions."""
    base_run, head_run = runner.obs.get_run(base), runner.obs.get_run(head)
    if base_run is None or head_run is None:
        missing = base if base_run is None else head
        raise HTTPException(status_code=404, detail=f"Run '{missing}' not found.")
    return compare_runs(base_run, head_run, tolerance)


@router.get("/eval/runs/{run_id}", response_model=EvalRunSummary, tags=["Evaluation"])
async def get_eval_run(run_id: str, runner: Runner) -> EvalRunSummary:
    summary = runner.obs.get_run(run_id)
    if summary is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found.")
    return summary


@router.get("/eval/runs/{run_id}/markdown", response_class=PlainTextResponse, tags=["Evaluation"])
async def download_markdown_report(run_id: str, runner: Runner) -> PlainTextResponse:
    summary = runner.obs.get_run(run_id)
    if summary is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found.")
    return PlainTextResponse(
        content=render_markdown(summary),
        headers={"Content-Disposition": f'attachment; filename="{run_id}_report.md"'},
    )


@router.get("/eval/runs/{run_id}/junit", tags=["Evaluation"])
async def download_junit_report(run_id: str, runner: Runner) -> Response:
    summary = runner.obs.get_run(run_id)
    if summary is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found.")
    return Response(
        content=render_junit(summary),
        media_type="application/xml",
        headers={"Content-Disposition": f'attachment; filename="{run_id}_junit.xml"'},
    )


@router.get("/eval/runs/{run_id}/csv", tags=["Evaluation"])
async def download_csv_report(run_id: str, runner: Runner) -> Response:
    summary = runner.obs.get_run(run_id)
    if summary is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found.")
    return Response(
        content=render_csv(summary),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{run_id}_results.csv"'},
    )


@router.get("/eval/runs/{run_id}/json", tags=["Evaluation"])
async def download_json_report(run_id: str, runner: Runner) -> Response:
    summary = runner.obs.get_run(run_id)
    if summary is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found.")
    return Response(
        content=json.dumps(summary.model_dump(), indent=2, ensure_ascii=False),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{run_id}_report.json"'},
    )


@router.post("/cache/test", tags=["Cache"])
async def test_cache_latency(payload: CacheTestRequest, runner: Runner) -> dict[str, Any]:
    """Send one query through the cache layer to compare cold vs. cached latency."""
    probe = EvalTestCase(
        case_id="cache_probe",
        target_type="qa_agent",
        category="qa_en",
        language="en",
        query=payload.query,
        reference_context=(
            "Saudi Labor Law Article 84: Half month wage for first 5 years, full month thereafter."
        ),
    )
    res = await runner.adapter.invoke(case=probe, use_cache=payload.use_cache, target_mode="auto")
    return {
        "query": payload.query,
        "cache_hit": res.cache_hit,
        "cache_backend": runner.cache.backend,
        "latency_ms": res.latency_ms,
        "tokens_used": res.prompt_tokens + res.completion_tokens,
        "estimated_cost_usd": res.estimated_cost_usd,
        "error": res.error,
        "output_preview": res.output_text[:300],
    }


@router.get("/cache/stats", response_model=CacheStats, tags=["Cache"])
async def get_cache_stats(runner: Runner) -> CacheStats:
    return await runner.cache.stats()


@router.post("/cache/flush", tags=["Cache"])
async def flush_cache(runner: Runner) -> dict[str, Any]:
    return {"status": "flushed", "cleared_keys": await runner.cache.flush()}


@router.get("/traces", response_model=list[TraceRecord], tags=["Observability"])
async def list_traces(
    runner: Runner,
    limit: Annotated[int, Query(ge=1, le=300)] = 100,
    run_id: str | None = None,
) -> list[TraceRecord]:
    """Recent per-case traces (optionally for a single run)."""
    return runner.obs.list_traces(limit=limit, run_id=run_id)
