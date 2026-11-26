"""Shared fixtures. Every test runs against temp directories and a scrubbed environment."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from app.core.config import Settings
from app.main import create_app
from app.models.schemas import EvalTestCase
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]
DATASET = REPO_ROOT / "datasets" / "bilingual_eval_suite_55.json"

_EXTRA_ENV = {"GIT_SHA", "GITHUB_SHA", "GITHUB_STEP_SUMMARY"}


@pytest.fixture(autouse=True)
def _scrub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's shell / .env must never influence test results."""
    for name in list(os.environ):
        if name.lower() in Settings.model_fields or name in _EXTRA_ENV:
            monkeypatch.delenv(name, raising=False)


def build_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "dataset_path": DATASET,
        "sqlite_path": tmp_path / "traces.db",
        "reports_dir": tmp_path / "reports",
        "target_retry_backoff_seconds": 0.0,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture
def settings_factory(tmp_path: Path) -> Callable[..., Settings]:
    def _factory(**overrides: Any) -> Settings:
        return build_settings(tmp_path, **overrides)

    return _factory


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory()


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def make_case() -> Callable[..., EvalTestCase]:
    def _make(**kw: Any) -> EvalTestCase:
        base: dict[str, Any] = {
            "case_id": "T-01",
            "target_type": "qa_agent",
            "category": "qa_en",
            "language": "en",
            "query": "What is the probation period?",
            "reference_context": "Probation shall not exceed 90 days under Article 53.",
            "expected_keywords": ["90", "53"],
        }
        base.update(kw)
        return EvalTestCase(**base)

    return _make
