"""Regression comparison between two evaluation runs."""

from __future__ import annotations

from app.models.schemas import ComparisonReport, EvalRunSummary, MetricDelta

# metric name -> higher_is_better
_QUALITY_METRICS: dict[str, bool] = {
    "overall_pass_rate": True,
    "avg_faithfulness": True,
    "avg_relevancy": True,
    "injection_defense_rate": True,
    "pii_safety_rate": True,
}
# Informational only: latency and cost are noisy, so they are reported but never gate.
_INFO_METRICS: dict[str, bool] = {
    "p95_latency_ms": False,
    "cost_per_1k_queries_usd": False,
}


def compare_runs(
    base: EvalRunSummary, head: EvalRunSummary, tolerance: float = 0.02
) -> ComparisonReport:
    """Compare ``head`` (candidate) against ``base`` (baseline).

    A regression is (a) any quality metric that dropped by more than ``tolerance``
    (absolute), or (b) any case that passed in the baseline but fails now.
    """
    metrics: list[MetricDelta] = []
    regressions: list[str] = []

    for name, higher in {**_QUALITY_METRICS, **_INFO_METRICS}.items():
        b, h = float(getattr(base, name)), float(getattr(head, name))
        delta = round(h - b, 4)
        gating = name in _QUALITY_METRICS
        regressed = gating and ((delta < -tolerance) if higher else (delta > tolerance))
        metrics.append(
            MetricDelta(
                metric=name,
                base=b,
                head=h,
                delta=delta,
                higher_is_better=higher,
                regressed=regressed,
            )
        )
        if regressed:
            regressions.append(f"{name} dropped by {abs(delta):.4f} (tolerance {tolerance:.4f})")

    base_status = {r.case_id: r.passed for r in base.results}
    head_status = {r.case_id: r.passed for r in head.results}
    newly_failing = sorted(c for c, ok in head_status.items() if not ok and base_status.get(c))
    newly_passing = sorted(c for c, ok in head_status.items() if ok and base_status.get(c) is False)
    if newly_failing:
        regressions.append(
            f"{len(newly_failing)} case(s) newly failing: {', '.join(newly_failing)}"
        )

    return ComparisonReport(
        base_run_id=base.run_id,
        head_run_id=head.run_id,
        tolerance=tolerance,
        metrics=metrics,
        newly_failing_cases=newly_failing,
        newly_passing_cases=newly_passing,
        regressions=regressions,
        regression_detected=bool(regressions),
    )
