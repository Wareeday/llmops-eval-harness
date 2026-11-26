"""Human- and CI-friendly report renderers (Markdown, JUnit XML, CSV)."""

from __future__ import annotations

import csv
import io
from xml.etree import ElementTree as ET

from app.models.schemas import ComparisonReport, EvalCaseResult, EvalRunSummary

_TARGET_LABEL = {
    "standalone": (
        "built-in reference agent (**self-test** - validates the harness, not your agent)"
    ),
    "live": "live HTTP agent",
    "mixed": "mixed (live + built-in reference agent)",
}


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")[:160]


def render_markdown(summary: EvalRunSummary, comparison: ComparisonReport | None = None) -> str:
    verdict = "PASS ✅" if summary.quality_gate_passed else "FAIL ❌"
    lines = [
        f"# LLMOps Automated Evaluation Report (`{summary.run_id}`)",
        "",
        f"- **CI/CD Quality Gate**: **{verdict}**",
        f"- **Target**: {_TARGET_LABEL.get(summary.effective_target, summary.effective_target)}",
        f"- **Timestamp (UTC)**: `{summary.timestamp}`",
        f"- **Version / Commit**: `{summary.app_version or '-'}` / "
        f"`{(summary.git_sha or '-')[:12]}`",
        f"- **Dataset SHA-256**: `{(summary.dataset_sha256 or '-')[:16]}`",
        f"- **Cache**: `{summary.cache_backend}` (enabled: `{summary.cache_enabled}`, "
        f"hit rate: **{summary.cache_hit_rate * 100:.1f}%**)",
        f"- **Duration**: {summary.duration_seconds:.2f}s at concurrency {summary.concurrency}",
        "",
    ]
    if summary.quality_gate_reasons:
        lines += ["## Quality Gate Failures", ""]
        lines += [f"- ❌ {reason}" for reason in summary.quality_gate_reasons]
        lines.append("")

    lines += [
        "## Executive Metrics",
        "",
        "| Metric | Value |",
        "| :--- | :--- |",
        f"| Overall Pass Rate | **{summary.passed_cases}/{summary.total_cases} "
        f"({summary.overall_pass_rate * 100:.1f}%)** |",
        f"| Avg Faithfulness Score | **{summary.avg_faithfulness:.3f}** |",
        f"| Avg Answer Relevancy | **{summary.avg_relevancy:.3f}** |",
        f"| Prompt-Injection Defense | **{summary.injection_defense_rate * 100:.1f}%** |",
        f"| PII Zero-Leak Safety Rate | **{summary.pii_safety_rate * 100:.1f}%** |",
        f"| Target Errors | **{summary.error_cases}** |",
        f"| P50 / P95 Latency | **{summary.p50_latency_ms} ms / {summary.p95_latency_ms} ms** |",
        f"| Total Tokens / Est. Cost per 1k Queries | "
        f"**{summary.total_tokens} tokens / ${summary.cost_per_1k_queries_usd:.4f}** |",
        "",
        "## Category Breakdown",
        "",
        "| Category | Passed | Pass Rate | Avg Faithfulness | Avg Latency (ms) |",
        "| :--- | :--- | :--- | :--- | :--- |",
    ]
    lines += [
        f"| `{c.category}` | {c.passed_cases}/{c.total_cases} | {c.pass_rate * 100:.1f}% | "
        f"{c.avg_faithfulness:.3f} | {c.avg_latency_ms} ms |"
        for c in summary.category_breakdown
    ]

    failed = [r for r in summary.results if not r.passed]
    if failed:
        lines += [
            "",
            f"## Failing Cases ({len(failed)})",
            "",
            "| Case | Category | Notes |",
            "| :--- | :--- | :--- |",
        ]
        lines += [f"| `{r.case_id}` | `{r.category}` | {_cell(r.notes)} |" for r in failed]

    if comparison is not None:
        lines += ["", render_comparison_markdown(comparison).rstrip()]
    return "\n".join(lines) + "\n"


def render_comparison_markdown(report: ComparisonReport) -> str:
    status = "REGRESSION ❌" if report.regression_detected else "No regression ✅"
    lines = [
        f"## Regression Check vs `{report.base_run_id}` - {status}",
        "",
        f"Tolerance: {report.tolerance:.3f} (absolute) on quality metrics.",
        "",
        "| Metric | Baseline | Current | Δ |",
        "| :--- | ---: | ---: | ---: |",
    ]
    for m in report.metrics:
        flag = " ❌" if m.regressed else ""
        lines.append(f"| {m.metric} | {m.base:.4f} | {m.head:.4f} | {m.delta:+.4f}{flag} |")
    if report.newly_failing_cases:
        lines += [
            "",
            "**Newly failing:** " + ", ".join(f"`{c}`" for c in report.newly_failing_cases),
        ]
    if report.newly_passing_cases:
        lines += [
            "",
            "**Newly passing:** " + ", ".join(f"`{c}`" for c in report.newly_passing_cases),
        ]
    return "\n".join(lines) + "\n"


def render_junit(summary: EvalRunSummary) -> str:
    """JUnit XML (one <testsuite> per category) - understood by every CI system."""
    root = ET.Element(
        "testsuites",
        name=f"llmops-eval-{summary.run_id}",
        tests=str(summary.total_cases),
        failures=str(sum(1 for r in summary.results if not r.passed and not r.error)),
        errors=str(summary.error_cases),
        time=f"{summary.duration_seconds:.3f}",
    )
    by_cat: dict[str, list[EvalCaseResult]] = {}
    for r in summary.results:
        by_cat.setdefault(r.category, []).append(r)
    for category, items in sorted(by_cat.items()):
        suite = ET.SubElement(
            root,
            "testsuite",
            name=category,
            tests=str(len(items)),
            failures=str(sum(1 for r in items if not r.passed and not r.error)),
            errors=str(sum(1 for r in items if r.error)),
        )
        for r in items:
            case = ET.SubElement(
                suite,
                "testcase",
                classname=category,
                name=r.case_id,
                time=f"{r.latency_ms / 1000:.4f}",
            )
            if r.error:
                ET.SubElement(case, "error", message=r.error[:300], type="TargetError")
            elif not r.passed:
                failure = ET.SubElement(case, "failure", message=r.notes[:300], type="EvalFailure")
                failure.text = (
                    f"faithfulness={r.faithfulness_score} relevancy={r.relevancy_score} "
                    f"injection_safe={r.injection_safe} pii_safe={r.pii_safe}\n{r.notes}"
                )
    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def render_csv(summary: EvalRunSummary) -> str:
    """RFC-4180 CSV export of per-case metrics for spreadsheets and audit trails."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "run_id",
            "case_id",
            "category",
            "target_type",
            "language",
            "passed",
            "faithfulness_score",
            "relevancy_score",
            "injection_safe",
            "pii_safe",
            "cache_hit",
            "target_source",
            "latency_ms",
            "prompt_tokens",
            "completion_tokens",
            "estimated_cost_usd",
            "actual_status",
            "error",
            "notes",
        ]
    )
    for r in summary.results:
        writer.writerow(
            [
                summary.run_id,
                r.case_id,
                r.category,
                r.target_type,
                r.language,
                r.passed,
                r.faithfulness_score,
                r.relevancy_score,
                r.injection_safe,
                r.pii_safe,
                r.cache_hit,
                r.target_source,
                r.latency_ms,
                r.prompt_tokens,
                r.completion_tokens,
                r.estimated_cost_usd,
                r.actual_status,
                r.error or "",
                r.notes,
            ]
        )
    return buf.getvalue()
