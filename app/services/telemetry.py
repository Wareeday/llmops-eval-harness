"""Prometheus metrics exposed at ``GET /metrics``.

A private ``CollectorRegistry`` per application instance avoids duplicate-registration
errors when several apps are created in one process (tests, reloaders).
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

from app.models.schemas import EvalRunSummary

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


class Telemetry:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.runs = Counter(
            "llmops_eval_runs_total", "Completed evaluation runs.", ["gate"], registry=self.registry
        )
        self.cases = Counter(
            "llmops_eval_cases_total",
            "Evaluated cases by category and outcome.",
            ["category", "outcome"],
            registry=self.registry,
        )
        self.cache_ops = Counter(
            "llmops_cache_lookups_total", "Cache lookups.", ["result"], registry=self.registry
        )
        self.duration = Histogram(
            "llmops_eval_run_duration_seconds",
            "Wall-clock duration of evaluation runs.",
            buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600),
            registry=self.registry,
        )
        self.last = Gauge(
            "llmops_last_run_metric",
            "Metrics of the most recent evaluation run.",
            ["metric"],
            registry=self.registry,
        )

    def observe_run(self, summary: EvalRunSummary) -> None:
        self.runs.labels(gate="pass" if summary.quality_gate_passed else "fail").inc()
        self.duration.observe(summary.duration_seconds)
        for r in summary.results:
            outcome = "error" if r.error else ("pass" if r.passed else "fail")
            self.cases.labels(category=r.category, outcome=outcome).inc()
            if summary.cache_enabled:
                self.cache_ops.labels(result="hit" if r.cache_hit else "miss").inc()
        for name in (
            "overall_pass_rate",
            "avg_faithfulness",
            "avg_relevancy",
            "injection_defense_rate",
            "pii_safety_rate",
            "cache_hit_rate",
            "p95_latency_ms",
            "cost_per_1k_queries_usd",
        ):
            self.last.labels(metric=name).set(float(getattr(summary, name)))
        self.last.labels(metric="quality_gate_passed").set(
            1.0 if summary.quality_gate_passed else 0.0
        )

    def render(self) -> bytes:
        return generate_latest(self.registry)
