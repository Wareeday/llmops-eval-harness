from __future__ import annotations

import csv
import io
import xml.etree.ElementTree as ET
from typing import Any

from app.models.schemas import EvalCaseResult, EvalRunSummary
from app.services.comparison import compare_runs
from app.services.reporting import (
    render_comparison_markdown,
    render_csv,
    render_junit,
    render_markdown,
)


def _result(case_id: str, passed: bool = True, **kw: Any) -> EvalCaseResult:
    base: dict[str, Any] = {
        "case_id": case_id,
        "target_type": "qa_agent",
        "category": "qa_en",
        "language": "en",
        "query": "q",
        "actual_output": "a",
        "passed": passed,
        "faithfulness_score": 1.0 if passed else 0.2,
        "relevancy_score": 1.0,
        "latency_ms": 12.0,
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "estimated_cost_usd": 0.0001,
        "notes": "ok" if passed else "bad | pipe\nnewline",
    }
    base.update(kw)
    return EvalCaseResult(**base)


def make_summary(
    run_id: str = "run_a", results: list[EvalCaseResult] | None = None, **kw: Any
) -> EvalRunSummary:
    results = results if results is not None else [_result("A"), _result("B")]
    base: dict[str, Any] = {
        "run_id": run_id,
        "timestamp": "2026-01-01T00:00:00+00:00",
        "target_mode": "auto",
        "cache_enabled": False,
        "cache_backend": "memory_ttl",
        "total_cases": len(results),
        "passed_cases": sum(r.passed for r in results),
        "overall_pass_rate": 1.0,
        "avg_faithfulness": 1.0,
        "avg_relevancy": 1.0,
        "injection_defense_rate": 1.0,
        "pii_safety_rate": 1.0,
        "cache_hit_rate": 0.0,
        "p50_latency_ms": 12.0,
        "p95_latency_ms": 13.0,
        "avg_latency_ms": 12.0,
        "total_tokens": 30,
        "total_cost_usd": 0.0002,
        "cost_per_1k_queries_usd": 0.1,
        "quality_gate_passed": True,
        "results": results,
    }
    base.update(kw)
    return EvalRunSummary(**base)


def test_identical_runs_have_no_regression() -> None:
    report = compare_runs(make_summary("base"), make_summary("head"))
    assert not report.regression_detected and report.regressions == []
    assert report.base_run_id == "base" and report.head_run_id == "head"


def test_metric_drop_beyond_tolerance_is_a_regression() -> None:
    head = make_summary("head", avg_faithfulness=0.90)
    assert compare_runs(make_summary(), head, tolerance=0.02).regression_detected
    assert not compare_runs(make_summary(), head, tolerance=0.15).regression_detected


def test_newly_failing_and_passing_cases() -> None:
    base = make_summary(results=[_result("A"), _result("B", passed=False)])
    head = make_summary("head", results=[_result("A", passed=False), _result("B")])
    report = compare_runs(base, head)
    assert report.newly_failing_cases == ["A"] and report.newly_passing_cases == ["B"]
    assert report.regression_detected and "newly failing" in report.regressions[0]


def test_latency_and_cost_are_informational_only() -> None:
    head = make_summary("head", p95_latency_ms=500.0, cost_per_1k_queries_usd=9.0)
    report = compare_runs(make_summary(), head)
    assert not report.regression_detected
    assert {m.metric: m.delta for m in report.metrics}["p95_latency_ms"] == 487.0


def test_markdown_lists_failures_and_escapes_tables() -> None:
    summary = make_summary(
        results=[_result("A"), _result("B", passed=False)],
        quality_gate_passed=False,
        quality_gate_reasons=["Faithfulness 0.60 < threshold 0.80"],
        effective_target="standalone",
    )
    md = render_markdown(summary, compare_runs(make_summary(), summary))
    assert "LLMOps Automated Evaluation Report" in md and "FAIL" in md
    assert "self-test" in md and "Failing Cases (1)" in md
    assert "bad \\| pipe newline" in md  # pipes escaped, newlines flattened
    assert "Regression Check" in md and "Faithfulness 0.60" in md


def test_comparison_markdown_marks_regressions() -> None:
    head = make_summary(
        "h", avg_faithfulness=0.5, results=[_result("A", passed=False), _result("B")]
    )
    md = render_comparison_markdown(compare_runs(make_summary(), head))
    assert "REGRESSION" in md and "Newly failing" in md and "❌" in md


def test_junit_structure() -> None:
    summary = make_summary(
        results=[
            _result("A"),
            _result("B", passed=False),
            _result("C", passed=False, error="timeout"),
        ],
        error_cases=1,
        duration_seconds=1.5,
    )
    root = ET.fromstring(render_junit(summary))  # noqa: S314
    assert root.get("tests") == "3" and root.get("failures") == "1" and root.get("errors") == "1"
    suite = root.find("testsuite")
    assert suite is not None and suite.get("name") == "qa_en"
    assert len(suite.findall("testcase")) == 3
    assert len(suite.findall(".//failure")) == 1 and len(suite.findall(".//error")) == 1


def test_csv_export_structure() -> None:
    summary = make_summary(results=[_result("A"), _result("B", passed=False, error="timeout")])
    rows = list(csv.reader(io.StringIO(render_csv(summary))))
    assert len(rows) == 3
    assert rows[0][:2] == ["run_id", "case_id"]
    assert rows[1][1:3] == ["A", "qa_en"]
    assert rows[2][-2] == "timeout" and rows[2][-1] == "bad | pipe\nnewline"
