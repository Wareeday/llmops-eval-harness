from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from app.core.config import Settings
from app.models.schemas import EvalRunRequest
from app.services.runner import (
    EvaluationRunner,
    NoCasesSelectedError,
    RunInProgressError,
    _percentile,
)
from app.services.target_adapter import TargetConfigError

SettingsFactory = Callable[..., Settings]


def test_percentile_nearest_rank() -> None:
    assert _percentile([], 95) == 0.0
    assert _percentile([5.0], 50) == 5.0
    values = [float(i) for i in range(1, 101)]
    assert (_percentile(values, 50), _percentile(values, 95), _percentile(values, 100)) == (
        50.0,
        95.0,
        100.0,
    )


async def test_builtin_reference_agent_passes_all_55(settings: Settings) -> None:
    """Self-test invariant: if this fails, the harness (or the dataset) is broken."""
    runner = EvaluationRunner(settings)
    await runner.startup()
    summary = await runner.run_suite(EvalRunRequest(use_cache=False))
    await runner.aclose()
    assert summary.total_cases == summary.passed_cases == 55
    assert summary.quality_gate_passed and summary.quality_gate_reasons == []
    assert summary.effective_target == "standalone" and summary.error_cases == 0
    assert summary.injection_defense_rate == summary.pii_safety_rate == 1.0
    assert summary.dataset_sha256 and summary.app_version


async def test_results_keep_dataset_order_under_concurrency(settings: Settings) -> None:
    runner = EvaluationRunner(settings)
    summary = await runner.run_suite(EvalRunRequest(use_cache=False, concurrency=16))
    assert [r.case_id for r in summary.results] == [c.case_id for c in runner.dataset]
    assert summary.concurrency == 16
    await runner.aclose()


async def test_cold_then_warm_cache(settings: Settings) -> None:
    runner = EvaluationRunner(settings)
    cold = await runner.run_suite(EvalRunRequest(use_cache=True))
    warm = await runner.run_suite(EvalRunRequest(use_cache=True))
    assert cold.cache_hit_rate == 0.0 and cold.total_tokens > 0
    assert warm.cache_hit_rate == 1.0 and warm.total_tokens == 0 and warm.total_cost_usd == 0.0
    assert warm.p95_latency_ms < cold.p95_latency_ms
    await runner.aclose()


async def test_filters_and_empty_selection(settings: Settings) -> None:
    runner = EvaluationRunner(settings)
    by_cat = await runner.run_suite(EvalRunRequest(categories=["pii_privacy", "prompt_injection"]))
    assert by_cat.total_cases == 20
    by_id = await runner.run_suite(EvalRunRequest(case_ids=["QA-EN-01", "RED-01"]))
    assert {r.case_id for r in by_id.results} == {"QA-EN-01", "RED-01"}
    with pytest.raises(NoCasesSelectedError):
        await runner.run_suite(EvalRunRequest(case_ids=["DOES-NOT-EXIST"]))
    await runner.aclose()


async def test_only_one_run_at_a_time(settings: Settings) -> None:
    runner = EvaluationRunner(settings)
    release = asyncio.Event()
    original = runner.adapter.invoke

    async def slow(*args: Any, **kwargs: Any) -> Any:
        await release.wait()
        return await original(*args, **kwargs)

    runner.adapter.invoke = slow  # type: ignore[method-assign]
    first = asyncio.create_task(runner.run_suite(EvalRunRequest(case_ids=["QA-EN-01"])))
    await asyncio.sleep(0.05)
    assert runner.is_running
    with pytest.raises(RunInProgressError):
        await runner.run_suite(EvalRunRequest(case_ids=["QA-EN-01"]))
    release.set()
    assert (await first).total_cases == 1
    assert not runner.is_running
    await runner.aclose()


async def test_live_mode_without_endpoint_fails_before_any_work(settings: Settings) -> None:
    runner = EvaluationRunner(settings)
    with pytest.raises(TargetConfigError):
        await runner.run_suite(EvalRunRequest(target_mode="live"))
    assert runner.obs.count_runs() == 0
    await runner.aclose()


async def test_unreachable_live_agent_fails_the_gate(settings_factory: SettingsFactory) -> None:
    def refuse(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    cfg = settings_factory(
        target_qa_endpoint_url="http://agent/qa",
        target_workflow_endpoint_url="http://agent/wf",
        target_max_retries=0,
    )
    runner = EvaluationRunner(
        cfg, target_client=httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    )
    summary = await runner.run_suite(EvalRunRequest(use_cache=False))
    await runner.aclose()
    assert summary.error_cases == 55 and summary.passed_cases == 0
    assert summary.effective_target == "live"
    assert not summary.quality_gate_passed
    assert any("Target errors" in r for r in summary.quality_gate_reasons)
    assert summary.injection_defense_rate == 0.0  # unanswered attacks are not "defended"


@pytest.mark.parametrize(
    ("override", "needle"),
    [
        ({"max_p95_latency_ms": 0.001}, "P95 latency"),
        ({"max_cost_per_1k_usd": 0.0000001}, "Cost/1k"),
        ({"min_faithfulness": 1.0, "min_pass_rate": 1.0}, None),  # still passes: 55/55
    ],
)
async def test_optional_gate_thresholds(
    settings_factory: SettingsFactory, override: dict[str, float], needle: str | None
) -> None:
    runner = EvaluationRunner(settings_factory(**override))
    summary = await runner.run_suite(EvalRunRequest(use_cache=False))
    await runner.aclose()
    if needle is None:
        assert summary.quality_gate_passed
    else:
        assert not summary.quality_gate_passed
        assert any(needle in r for r in summary.quality_gate_reasons)


async def test_reports_traces_history_and_metrics_are_produced(settings: Settings) -> None:
    runner = EvaluationRunner(settings)
    summary = await runner.run_suite(EvalRunRequest(use_cache=False))
    assert (
        (settings.reports_dir / "eval_report.md").read_text(encoding="utf-8").startswith("# LLMOps")
    )
    saved = json.loads((settings.reports_dir / "eval_report.json").read_text(encoding="utf-8"))
    assert saved["run_id"] == summary.run_id
    assert runner.obs.count_runs() == 1 and len(runner.obs.list_traces(limit=300)) == 55
    metrics = runner.telemetry.render().decode()
    assert 'llmops_eval_runs_total{gate="pass"} 1.0' in metrics
    assert "llmops_last_run_metric" in metrics
    assert runner.render_markdown_report(summary).count("Quality Gate") >= 1
    await runner.aclose()


async def test_async_job_failure_and_cancellation(settings: Settings) -> None:
    runner = EvaluationRunner(settings)
    with pytest.raises(NoCasesSelectedError):
        runner.start_job(EvalRunRequest(case_ids=["MISSING"]))

    # Simulate an unexpected failure inside _run_locked
    async def boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError("boom")

    orig = runner._run_locked
    runner._run_locked = boom  # type: ignore[method-assign]
    job = runner.start_job(EvalRunRequest(case_ids=["QA-EN-01"]))
    with pytest.raises(RunInProgressError):
        runner.start_job(EvalRunRequest(case_ids=["QA-EN-01"]))
    for _ in range(50):
        if job.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.01)
    assert job.status == "failed" and job.error == "boom"

    # Simulate cancelling a still-running job during aclose()
    runner._run_locked = orig  # type: ignore[method-assign]
    release = asyncio.Event()
    orig_invoke = runner.adapter.invoke

    async def hang(*args: Any, **kwargs: Any) -> Any:
        await release.wait()
        return await orig_invoke(*args, **kwargs)

    runner.adapter.invoke = hang  # type: ignore[method-assign]
    job2 = runner.start_job(EvalRunRequest(case_ids=["QA-EN-01"]))
    await asyncio.sleep(0.02)
    assert runner.get_status().active_job_id == job2.job_id
    await runner.aclose()
