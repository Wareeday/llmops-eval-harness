"""Dataset loading, validation and fingerprinting."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from pydantic import ValidationError

from app.models.schemas import EvalTestCase

_ARABIC = re.compile(r"[\u0600-\u06FF]")


class DatasetError(ValueError):
    """Raised when a dataset file cannot be parsed or is structurally invalid."""


def resolve_dataset_path(path: Path) -> Path:
    """Return ``path`` or fall back to the bundled dataset next to the source tree."""
    if path.exists():
        return path
    bundled = Path(__file__).resolve().parents[2] / "datasets" / path.name
    return bundled if bundled.exists() else path


def load_dataset(path: Path) -> list[EvalTestCase]:
    """Load and schema-validate a dataset. Raises :class:`DatasetError` on any problem."""
    resolved = resolve_dataset_path(path)
    if not resolved.exists():
        raise DatasetError(f"Dataset file not found: {path}")
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DatasetError(f"{resolved}: invalid JSON ({exc})") from exc
    if not isinstance(raw, list):
        raise DatasetError(f"{resolved}: top-level JSON value must be a list of test cases")

    cases: list[EvalTestCase] = []
    for index, item in enumerate(raw):
        try:
            cases.append(EvalTestCase.model_validate(item))
        except ValidationError as exc:
            ident = item.get("case_id", f"#{index}") if isinstance(item, dict) else f"#{index}"
            raise DatasetError(f"{resolved}: case {ident} is invalid: {exc.errors()[0]}") from exc

    problems = [p for p in validate_cases(cases) if p.startswith("ERROR")]
    if problems:
        raise DatasetError(f"{resolved}: " + "; ".join(problems))
    return cases


def dataset_fingerprint(path: Path) -> str:
    """SHA-256 of the dataset file - stored with every run for reproducibility."""
    resolved = resolve_dataset_path(path)
    if not resolved.exists():
        return ""
    return hashlib.sha256(resolved.read_bytes()).hexdigest()


def validate_cases(cases: list[EvalTestCase]) -> list[str]:
    """Return human-readable problems. ``ERROR`` entries are fatal, ``WARN`` are advisory."""
    problems: list[str] = []
    seen: set[str] = set()
    for c in cases:
        if c.case_id in seen:
            problems.append(f"ERROR {c.case_id}: duplicate case_id")
        seen.add(c.case_id)

        if c.category == "prompt_injection" and not c.is_attack:
            problems.append(f"ERROR {c.case_id}: prompt_injection cases must set is_attack=true")
        if c.category == "structured_workflow":
            if c.target_type != "workflow_agent":
                problems.append(f"ERROR {c.case_id}: structured_workflow needs workflow_agent")
            if not c.expected_status:
                problems.append(f"ERROR {c.case_id}: structured_workflow needs expected_status")
        elif c.target_type == "workflow_agent":
            problems.append(f"ERROR {c.case_id}: workflow_agent only valid for structured_workflow")
        if c.category == "pii_privacy" and not c.forbidden_patterns:
            problems.append(f"ERROR {c.case_id}: pii_privacy needs forbidden_patterns")
        if c.category in {"qa_en", "qa_ar"} and (
            not c.reference_context or not c.expected_keywords
        ):
            problems.append(f"WARN {c.case_id}: Q&A case without reference_context/keywords")
        if c.language == "ar" and not _ARABIC.search(c.query):
            problems.append(f"WARN {c.case_id}: language=ar but the query has no Arabic script")
        if c.category == "qa_ar" and c.language != "ar":
            problems.append(f"WARN {c.case_id}: qa_ar case should use language=ar")
    return problems
