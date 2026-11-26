"""End-to-end: the harness evaluating a *live* HTTP agent (in-process ASGI, no network)."""

from __future__ import annotations

from collections.abc import Callable

import httpx
from app.core.config import Settings
from app.models.schemas import EvalRunRequest
from app.services.runner import EvaluationRunner
from examples.reference_agent import create_agent


def _runner(factory: Callable[..., Settings], mode: str) -> EvaluationRunner:
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_agent(mode)), base_url="http://agent"
    )
    cfg = factory(
        target_qa_endpoint_url="http://agent/qa",
        target_workflow_endpoint_url="http://agent/workflow",
    )
    return EvaluationRunner(cfg, target_client=client)


async def test_good_agent_passes_the_gate(settings_factory: Callable[..., Settings]) -> None:
    runner = _runner(settings_factory, "good")
    summary = await runner.run_suite(EvalRunRequest(use_cache=False, target_mode="live"))
    await runner.aclose()
    assert summary.effective_target == "live" and summary.error_cases == 0
    assert summary.passed_cases == 55 and summary.quality_gate_passed
    assert summary.total_tokens > 0  # real `usage` reported by the agent


async def test_leaky_agent_is_caught_by_the_red_team_gate(
    settings_factory: Callable[..., Settings],
) -> None:
    runner = _runner(settings_factory, "leaky")
    summary = await runner.run_suite(EvalRunRequest(use_cache=False, target_mode="live"))
    await runner.aclose()
    assert not summary.quality_gate_passed
    assert summary.injection_defense_rate == 0.0
    assert summary.pii_safety_rate < 1.0
    reasons = " | ".join(summary.quality_gate_reasons)
    assert "Prompt-Injection" in reasons and "PII" in reasons and "Faithfulness" in reasons
    by_cat = {c.category: c for c in summary.category_breakdown}
    assert by_cat["structured_workflow"].pass_rate < 1.0  # rubber-stamping caught


async def test_auto_mode_uses_live_agent_when_configured(
    settings_factory: Callable[..., Settings],
) -> None:
    runner = _runner(settings_factory, "good")
    summary = await runner.run_suite(
        EvalRunRequest(use_cache=True, target_mode="auto", case_ids=["QA-EN-01"])
    )
    await runner.aclose()
    assert summary.effective_target == "live"
