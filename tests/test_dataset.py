from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest
from app.models.schemas import EvalTestCase
from app.services.dataset import (
    DatasetError,
    dataset_fingerprint,
    load_dataset,
    validate_cases,
)

from tests.conftest import DATASET


def test_bundled_dataset_is_valid_and_balanced() -> None:
    cases = load_dataset(DATASET)
    assert len(cases) == 55
    assert len({c.case_id for c in cases}) == 55
    assert Counter(c.category for c in cases) == {
        "qa_en": 12,
        "qa_ar": 12,
        "pii_privacy": 10,
        "prompt_injection": 10,
        "structured_workflow": 11,
    }
    assert validate_cases(cases) == []


def test_fingerprint_is_stable_and_changes_with_content(tmp_path: Path) -> None:
    assert dataset_fingerprint(DATASET) == dataset_fingerprint(DATASET)
    other = tmp_path / "d.json"
    other.write_text("[]", encoding="utf-8")
    assert dataset_fingerprint(other) != dataset_fingerprint(DATASET)
    assert dataset_fingerprint(tmp_path / "missing.json") == ""


def _write(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "suite.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("{not json", "invalid JSON"),
        ({"a": 1}, "must be a list"),
        (
            [
                {
                    "case_id": "X",
                    "target_type": "qa_agent",
                    "category": "qa_en",
                    "language": "en",
                    "query": "q",
                    "expected_keyword": ["typo"],
                }
            ],
            "invalid",
        ),
    ],
)
def test_load_errors(tmp_path: Path, payload: object, message: str) -> None:
    with pytest.raises(DatasetError, match=message):
        load_dataset(_write(tmp_path, payload))


def test_missing_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(DatasetError, match="not found"):
        load_dataset(tmp_path / "nope-unique-name.json")


def test_duplicate_ids_are_fatal(tmp_path: Path) -> None:
    case = {
        "case_id": "A",
        "target_type": "qa_agent",
        "category": "qa_en",
        "language": "en",
        "query": "q",
        "reference_context": "c",
        "expected_keywords": ["c"],
    }
    with pytest.raises(DatasetError, match="duplicate"):
        load_dataset(_write(tmp_path, [case, case]))


def test_validator_flags_inconsistent_cases() -> None:
    def mk(**kw: object) -> EvalTestCase:
        base = {
            "case_id": "X",
            "target_type": "qa_agent",
            "category": "qa_en",
            "language": "en",
            "query": "q",
            "reference_context": "c",
            "expected_keywords": ["c"],
        }
        return EvalTestCase(**{**base, **kw})  # type: ignore[arg-type]

    problems = validate_cases(
        [
            mk(case_id="1", category="prompt_injection", is_attack=False),
            mk(case_id="2", category="structured_workflow", target_type="workflow_agent"),
            mk(case_id="3", category="pii_privacy"),
            mk(case_id="4", category="qa_en", target_type="workflow_agent"),
            mk(case_id="5", language="ar", query="english only"),
            mk(case_id="6", category="qa_ar", language="en", reference_context=""),
        ]
    )
    text = "\n".join(problems)
    for needle in (
        "1: prompt_injection",
        "2: structured_workflow needs expected_status",
        "3: pii_privacy needs forbidden_patterns",
        "4: workflow_agent only",
        "5: language=ar",
        "6: qa_ar",
        "6: Q&A case without",
    ):
        assert needle in text
