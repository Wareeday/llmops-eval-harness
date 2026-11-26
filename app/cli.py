"""Command-line interface: run the evaluation suite as a CI/CD quality gate.

Examples::

    llmops-eval --no-cache --fail-on-gate
    llmops-eval --target-mode live --fail-on-gate --junit reports/junit.xml
    llmops-eval --baseline baselines/main.json --tolerance 0.02 --fail-on-gate
    llmops-eval --save-baseline baselines/main.json
    llmops-eval --validate-only --dataset my_suite.json

Exit codes: ``0`` success, ``1`` quality gate / regression failed, ``2`` configuration error.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from pydantic import ValidationError

from app import __version__
from app.core.config import Settings
from app.core.logging import configure_logging
from app.models.schemas import CATEGORIES, EvalRunRequest, EvalRunSummary
from app.services.comparison import compare_runs
from app.services.dataset import DatasetError, load_dataset, validate_cases
from app.services.reporting import render_csv, render_junit, render_markdown
from app.services.runner import EvaluationRunner, NoCasesSelectedError
from app.services.target_adapter import TargetConfigError


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="llmops-eval",
        description="Run the bilingual LLMOps evaluation suite and enforce the quality gate.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--no-cache", action="store_true", help="disable caching (cold benchmark)")
    p.add_argument("--target-mode", choices=["auto", "standalone", "live"], default="auto")
    p.add_argument("--categories", nargs="+", choices=CATEGORIES, help="run only these categories")
    p.add_argument("--case-ids", nargs="+", help="run only these case IDs")
    p.add_argument("--dataset", type=Path, help="path to a dataset JSON file")
    p.add_argument("--concurrency", type=int, help="parallel cases (default: EVAL_CONCURRENCY)")
    p.add_argument("--output-dir", type=Path, help="where to write eval_report.{md,json}")
    p.add_argument("--junit", type=Path, help="also write a JUnit XML report to this path")
    p.add_argument("--csv", type=Path, help="also write a CSV results report to this path")
    p.add_argument("--baseline", type=Path, help="baseline eval_report.json to compare against")
    p.add_argument(
        "--tolerance", type=float, default=0.02, help="allowed metric drop (default 0.02)"
    )
    p.add_argument("--save-baseline", type=Path, help="write this run's JSON as a new baseline")
    p.add_argument("--json", action="store_true", dest="as_json", help="print JSON, not Markdown")
    p.add_argument("--validate-only", action="store_true", help="validate the dataset and exit")
    p.add_argument(
        "--fail-on-gate", action="store_true", help="exit 1 if the gate/regression fails"
    )
    return p


def _load_baseline(path: Path) -> EvalRunSummary:
    return EvalRunSummary.model_validate(json.loads(path.read_text(encoding="utf-8")))


def _validate_only(settings: Settings) -> int:
    try:
        cases = load_dataset(settings.dataset_path)
    except DatasetError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 2
    problems = validate_cases(cases)
    for line in problems:
        print(line)
    print(f"Dataset OK: {len(cases)} cases, {len(problems)} warning(s).")
    return 0


def _write_outputs(args: argparse.Namespace, summary: EvalRunSummary, markdown: str) -> None:
    """Write the optional JUnit / CSV / baseline files and the GitHub step summary (sync I/O)."""
    if args.junit:
        args.junit.parent.mkdir(parents=True, exist_ok=True)
        args.junit.write_text(render_junit(summary), encoding="utf-8")
    if args.csv:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        args.csv.write_text(render_csv(summary), encoding="utf-8")
    if args.save_baseline:
        args.save_baseline.parent.mkdir(parents=True, exist_ok=True)
        args.save_baseline.write_text(
            json.dumps(summary.model_dump(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
    step_summary = os.getenv("GITHUB_STEP_SUMMARY")
    if step_summary:
        with Path(step_summary).open("a", encoding="utf-8") as fh:
            fh.write(markdown)


async def _amain(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = Settings()
    except ValidationError as exc:
        print(f"Invalid configuration:\n{exc}", file=sys.stderr)
        return 2

    overrides: dict[str, object] = {}
    if args.dataset:
        overrides["dataset_path"] = args.dataset
    if args.output_dir:
        overrides["reports_dir"] = args.output_dir
    if overrides:
        settings = settings.model_copy(update=overrides)
    configure_logging("WARNING" if args.as_json else settings.log_level, settings.log_format)

    if args.validate_only:
        return _validate_only(settings)

    settings.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    settings.reports_dir.mkdir(parents=True, exist_ok=True)

    try:
        runner = EvaluationRunner(settings=settings)
    except DatasetError as exc:
        print(f"Dataset error: {exc}", file=sys.stderr)
        return 2

    await runner.startup()
    try:
        summary = await runner.run_suite(
            EvalRunRequest(
                use_cache=not args.no_cache,
                categories=args.categories,
                case_ids=args.case_ids,
                target_mode=args.target_mode,
                concurrency=args.concurrency,
            )
        )
    except (NoCasesSelectedError, TargetConfigError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    finally:
        await runner.aclose()

    comparison = None
    if args.baseline:
        try:
            comparison = compare_runs(_load_baseline(args.baseline), summary, args.tolerance)
        except (OSError, ValueError) as exc:
            print(f"Cannot read baseline {args.baseline}: {exc}", file=sys.stderr)
            return 2

    markdown = render_markdown(summary, comparison)
    print(
        json.dumps(summary.model_dump(), indent=2, ensure_ascii=False) if args.as_json else markdown
    )

    _write_outputs(args, summary, markdown)
    print(f"Reports written to: {settings.reports_dir.resolve()}", file=sys.stderr)

    failed = (not summary.quality_gate_passed) or bool(
        comparison and comparison.regression_detected
    )
    if failed and args.fail_on_gate:
        reasons = summary.quality_gate_reasons + (comparison.regressions if comparison else [])
        print("CI QUALITY GATE FAILED: " + " | ".join(reasons), file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_amain(argv))


if __name__ == "__main__":
    raise SystemExit(main())
