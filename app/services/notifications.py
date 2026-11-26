"""Failure-tolerant webhook alerting for CI/CD quality-gate outcomes."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core.config import Settings
from app.models.schemas import EvalRunSummary

logger = logging.getLogger(__name__)


def build_webhook_payload(summary: EvalRunSummary) -> dict[str, Any]:
    """Build a Slack/Discord/generic-compatible webhook payload (no secrets)."""
    verdict = "PASS" if summary.quality_gate_passed else "FAIL"
    reasons = (
        "; ".join(summary.quality_gate_reasons) if summary.quality_gate_reasons else "All gates met"
    )
    text = (
        f"[LLMOps Quality Gate: {verdict}] run={summary.run_id} "
        f"target={summary.effective_target} pass={summary.passed_cases}/{summary.total_cases} "
        f"({summary.overall_pass_rate * 100:.1f}%) faith={summary.avg_faithfulness:.3f} "
        f"inj={summary.injection_defense_rate * 100:.1f}% "
        f"pii={summary.pii_safety_rate * 100:.1f}% | {reasons}"
    )
    return {
        "text": text,
        "event": "llmops.eval.completed",
        "run_id": summary.run_id,
        "quality_gate_passed": summary.quality_gate_passed,
        "quality_gate_reasons": summary.quality_gate_reasons,
        "effective_target": summary.effective_target,
        "git_sha": summary.git_sha,
        "metrics": {
            "overall_pass_rate": summary.overall_pass_rate,
            "avg_faithfulness": summary.avg_faithfulness,
            "avg_relevancy": summary.avg_relevancy,
            "injection_defense_rate": summary.injection_defense_rate,
            "pii_safety_rate": summary.pii_safety_rate,
            "p95_latency_ms": summary.p95_latency_ms,
            "cost_per_1k_queries_usd": summary.cost_per_1k_queries_usd,
        },
    }


async def send_alert_webhook(
    settings: Settings,
    summary: EvalRunSummary,
    http_client: httpx.AsyncClient | None = None,
) -> bool:
    """Deliver a quality-gate notification when configured. Never raises."""
    url = settings.alert_webhook_url.get_secret_value().strip()
    if not url:
        return False
    if summary.quality_gate_passed and not settings.alert_on_pass:
        return False

    payload = build_webhook_payload(summary)
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=10.0)
    try:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
    except Exception as exc:
        logger.warning("Alert webhook failed (%s); continuing.", type(exc).__name__)
        return False
    finally:
        if owns_client:
            await client.aclose()
    return True
