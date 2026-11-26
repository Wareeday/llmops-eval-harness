"""Evaluation runner: orchestration, aggregation, quality gate and CI reports."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import time
import uuid
from collections import OrderedDict, defaultdict
from datetime import datetime, timezone
from typing import Any

import httpx

from app import __version__
from app.core.config import Settings
from app.models.schemas import (
    CategoryMetrics,
    ComparisonReport,
    EvalCaseResult,
    EvalJobStatus,
    EvalRunRequest,
    EvalRunSummary,
    EvalTestCase,
    RunnerStatusResponse,
)
from app.services.cache_service import LLMCacheService
from app.services.dataset import dataset_fingerprint, load_dataset
from app.services.evaluators import EvaluationEngine
from app.services.notifications import send_alert_webhook
from app.services.observability import ObservabilityService
from app.services.reporting import render_markdown
from app.services.target_adapter import AgentTargetAdapter
from app.services.telemetry import Telemetry


class RunInProgressError(RuntimeError):
    """Another evaluation run is already executing."""


class NoCasesSelectedError(ValueError):
    """The category / case-id filters matched no test cases."""


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile."""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, math.ceil((pct / 100.0) * len(ordered)) - 1))
    return round(ordered[idx], 2)


class EvaluationRunner:
    """Runs the benchmark suite against a target and produces the quality-gate verdict."""

    def __init__(
        self,
        settings: Settings,
        telemetry: Telemetry | None = None,
        *,
        target_client: httpx.AsyncClient | None = None,
        judge_client: httpx.AsyncClient | None = None,
        langfuse_client: Any | None = None,
    ) -> None:
        self.settings = settings
        self.telemetry = telemetry or Telemetry()
        self.cache = LLMCacheService(
            redis_url=settings.redis_url.get_secret_value(),
            ttl_seconds=settings.cache_ttl_seconds,
            max_memory_entries=settings.cache_max_memory_entries,
        )
        self.obs = ObservabilityService(settings, langfuse_client=langfuse_client)
        self.evaluator = EvaluationEngine(settings, http_client=judge_client)
        self.adapter = AgentTargetAdapter(settings, self.cache, http_client=target_client)
        self._dataset: list[EvalTestCase] = load_dataset(settings.dataset_path)
        self._dataset_sha = dataset_fingerprint(settings.dataset_path)
        self._run_lock = asyncio.Lock()
        self._jobs: OrderedDict[str, EvalJobStatus] = OrderedDict()
        self._active_task: asyncio.Task[None] | None = None
        self._active_job_id: str | None = None
        self._completed_cases = 0
        self._total_cases = 0

    # ------------------------------------------------------------------ lifecycle
    async def startup(self) -> None:
        await self.cache.connect()

    async def aclose(self) -> None:
        if self._active_task is not None and not self._active_task.done():
            self._active_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._active_task
        await self.adapter.aclose()
        await self.evaluator.aclose()
        await self.cache.aclose()
        await asyncio.to_thread(self.obs.shutdown)

    @property
    def dataset(self) -> list[EvalTestCase]:
        return self._dataset

    @property
    def is_running(self) -> bool:
        return self._run_lock.locked() or (
            self._active_task is not None and not self._active_task.done()
        )

    def get_status(self) -> RunnerStatusResponse:
        return RunnerStatusResponse(
            is_running=self.is_running,
            active_job_id=self._active_job_id if self.is_running else None,
            completed_cases=self._completed_cases if self.is_running else 0,
            total_cases=self._total_cases if self.is_running else 0,
        )

    def get_job(self, job_id: str) -> EvalJobStatus | None:
        return self._jobs.get(job_id)

    # ------------------------------------------------------------------ run
    async def run_suite(self, req: EvalRunRequest) -> EvalRunSummary:
        if self.is_running:
            raise RunInProgressError("An evaluation run is already in progress.")
        async with self._run_lock:
            return await self._run_locked(req)

    def start_job(self, req: EvalRunRequest) -> EvalJobStatus:
        """Validate inputs, reserve the runner, and launch a background evaluation job."""
        if self.is_running:
            raise RunInProgressError("An evaluation run is already in progress.")
        cases = self.select_cases(req)
        for case in cases:
            self.adapter.resolve_source(case, req.target_mode)

        job_id = f"job_{uuid.uuid4().hex[:10]}"
        job = EvalJobStatus(
            job_id=job_id,
            status="queued",
            created_at=datetime.now(timezone.utc).isoformat(),
            completed_cases=0,
            total_cases=len(cases),
        )
        self._jobs[job_id] = job
        while len(self._jobs) > 50:
            self._jobs.popitem(last=False)

        self._active_job_id = job_id
        self._completed_cases = 0
        self._total_cases = len(cases)
        self._active_task = asyncio.create_task(self._execute_job(job_id, req))
        return job

    async def _execute_job(self, job_id: str, req: EvalRunRequest) -> None:
        job = self._jobs[job_id]
        async with self._run_lock:
            job.status = "running"
            job.started_at = datetime.now(timezone.utc).isoformat()
            try:
                summary = await self._run_locked(req, job=job)
            except Exception as exc:
                job.status = "failed"
                job.error = str(exc)
                job.finished_at = datetime.now(timezone.utc).isoformat()
            else:
                job.status = "completed"
                job.run_id = summary.run_id
                job.summary = summary
                job.completed_cases = summary.total_cases
                job.finished_at = datetime.now(timezone.utc).isoformat()
            finally:
                self._active_job_id = None

    def select_cases(self, req: EvalRunRequest) -> list[EvalTestCase]:
        cases = self._dataset
        if req.categories:
            wanted = set(req.categories)
            cases = [c for c in cases if c.category in wanted]
        if req.case_ids:
            ids = set(req.case_ids)
            cases = [c for c in cases if c.case_id in ids]
        if not cases:
            raise NoCasesSelectedError("No test cases matched the selected filters.")
        return cases

    async def _run_locked(
        self, req: EvalRunRequest, job: EvalJobStatus | None = None
    ) -> EvalRunSummary:
        cases = self.select_cases(req)
        for case in cases:  # fail fast (e.g. live mode without an endpoint) before any I/O
            self.adapter.resolve_source(case, req.target_mode)

        run_id = f"run_{uuid.uuid4().hex[:10]}"
        timestamp = datetime.now(timezone.utc).isoformat()
        concurrency = req.concurrency or self.settings.eval_concurrency
        sem = asyncio.Semaphore(concurrency)
        self._completed_cases = 0
        self._total_cases = len(cases)
        started = time.perf_counter()

        results = list(await asyncio.gather(*(self._run_case(c, req, sem, job) for c in cases)))
        duration = round(time.perf_counter() - started, 3)

        summary = self._aggregate(run_id, timestamp, req, results, duration, concurrency)
        await asyncio.to_thread(self.obs.record_traces, run_id, results)
        await asyncio.to_thread(self.obs.save_run, summary)
        await asyncio.to_thread(self._write_ci_reports, summary)
        self.telemetry.observe_run(summary)
        await send_alert_webhook(self.settings, summary)
        return summary

    async def _run_case(
        self,
        case: EvalTestCase,
        req: EvalRunRequest,
        sem: asyncio.Semaphore,
        job: EvalJobStatus | None = None,
    ) -> EvalCaseResult:
        async with sem:
            resp = await self.adapter.invoke(
                case=case, use_cache=req.use_cache, target_mode=req.target_mode
            )
            if resp.error:
                outcome = EvaluationEngine.failed_outcome(case, resp.error)
            else:
                outcome = await self.evaluator.evaluate_case(case, resp.output_text, resp.status)
        self._completed_cases += 1
        if job is not None:
            job.completed_cases = self._completed_cases
        return EvalCaseResult(
            case_id=case.case_id,
            target_type=case.target_type,
            category=case.category,
            language=case.language,
            query=case.query,
            actual_output=resp.output_text,
            actual_status=resp.status,
            passed=outcome.passed,
            faithfulness_score=outcome.faithfulness,
            relevancy_score=outcome.relevancy,
            injection_safe=outcome.injection_safe,
            pii_safe=outcome.pii_safe,
            cache_hit=resp.cache_hit,
            target_source=resp.source,
            error=resp.error,
            latency_ms=resp.latency_ms,
            prompt_tokens=resp.prompt_tokens,
            completion_tokens=resp.completion_tokens,
            estimated_cost_usd=resp.estimated_cost_usd,
            notes=outcome.notes,
        )

    # ------------------------------------------------------------------ aggregation
    def _aggregate(
        self,
        run_id: str,
        timestamp: str,
        req: EvalRunRequest,
        results: list[EvalCaseResult],
        duration: float,
        concurrency: int,
    ) -> EvalRunSummary:
        cfg = self.settings
        total = len(results)
        passed = sum(1 for r in results if r.passed)
        errors = sum(1 for r in results if r.error)
        latencies = [r.latency_ms for r in results]

        attacks = [r for r in results if r.category == "prompt_injection"]
        inj_rate = sum(1 for r in attacks if r.injection_safe) / len(attacks) if attacks else 1.0
        pii_rate = sum(1 for r in results if r.pii_safe) / total
        avg_faith = sum(r.faithfulness_score for r in results) / total
        avg_rel = sum(r.relevancy_score for r in results) / total
        cache_hit_rate = sum(1 for r in results if r.cache_hit) / total
        total_tokens = sum(r.prompt_tokens + r.completion_tokens for r in results)
        total_cost = round(sum(r.estimated_cost_usd for r in results), 6)
        cost_1k = round(total_cost / total * 1000.0, 4)
        p95 = _percentile(latencies, 95)
        pass_rate = passed / total

        reasons: list[str] = []
        if errors:
            reasons.append(f"Target errors: {errors}/{total} case(s) could not be evaluated")
        if avg_faith < cfg.min_faithfulness:
            reasons.append(f"Faithfulness {avg_faith:.2f} < threshold {cfg.min_faithfulness:.2f}")
        if inj_rate < cfg.min_injection_defense:
            reasons.append(
                f"Prompt-Injection Defense {inj_rate:.2%} "
                f"< threshold {cfg.min_injection_defense:.2%}"
            )
        if pii_rate < cfg.min_pii_safety:
            reasons.append(
                f"PII Zero-Leak Safety {pii_rate:.2%} < threshold {cfg.min_pii_safety:.2%}"
            )
        if cfg.min_pass_rate and pass_rate < cfg.min_pass_rate:
            reasons.append(f"Pass rate {pass_rate:.2%} < threshold {cfg.min_pass_rate:.2%}")
        if cfg.max_p95_latency_ms and p95 > cfg.max_p95_latency_ms:
            reasons.append(f"P95 latency {p95} ms > limit {cfg.max_p95_latency_ms:g} ms")
        if cfg.max_cost_per_1k_usd and cost_1k > cfg.max_cost_per_1k_usd:
            reasons.append(f"Cost/1k ${cost_1k:.4f} > limit ${cfg.max_cost_per_1k_usd:.4f}")

        by_cat: dict[str, list[EvalCaseResult]] = defaultdict(list)
        for r in results:
            by_cat[r.category].append(r)
        breakdown = [
            CategoryMetrics(
                category=cat,
                total_cases=len(items),
                passed_cases=sum(1 for x in items if x.passed),
                pass_rate=round(sum(1 for x in items if x.passed) / len(items), 4),
                avg_faithfulness=round(sum(x.faithfulness_score for x in items) / len(items), 3),
                avg_latency_ms=round(sum(x.latency_ms for x in items) / len(items), 2),
            )
            for cat, items in sorted(by_cat.items())
        ]

        sources = {r.target_source for r in results}
        effective = next(iter(sources)) if len(sources) == 1 else "mixed"

        return EvalRunSummary(
            run_id=run_id,
            timestamp=timestamp,
            app_version=__version__,
            git_sha=cfg.git_sha,
            dataset_sha256=self._dataset_sha,
            target_mode=req.target_mode,
            effective_target=effective,
            cache_enabled=req.use_cache,
            cache_backend=self.cache.backend,
            total_cases=total,
            passed_cases=passed,
            error_cases=errors,
            overall_pass_rate=round(pass_rate, 4),
            avg_faithfulness=round(avg_faith, 3),
            avg_relevancy=round(avg_rel, 3),
            injection_defense_rate=round(inj_rate, 4),
            pii_safety_rate=round(pii_rate, 4),
            cache_hit_rate=round(cache_hit_rate, 4),
            p50_latency_ms=_percentile(latencies, 50),
            p95_latency_ms=p95,
            avg_latency_ms=round(sum(latencies) / total, 2),
            duration_seconds=duration,
            concurrency=concurrency,
            total_tokens=total_tokens,
            total_cost_usd=total_cost,
            cost_per_1k_queries_usd=cost_1k,
            quality_gate_passed=not reasons,
            quality_gate_reasons=reasons,
            category_breakdown=breakdown,
            results=results,
        )

    # ------------------------------------------------------------------ reports
    def _write_ci_reports(self, summary: EvalRunSummary) -> None:
        out = self.settings.reports_dir
        out.mkdir(parents=True, exist_ok=True)
        (out / "eval_report.json").write_text(
            json.dumps(summary.model_dump(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (out / "eval_report.md").write_text(render_markdown(summary), encoding="utf-8")

    @staticmethod
    def render_markdown_report(
        summary: EvalRunSummary, comparison: ComparisonReport | None = None
    ) -> str:
        return render_markdown(summary, comparison)
