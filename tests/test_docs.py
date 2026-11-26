"""Keep the documentation honest: every setting must be documented and templated."""

from __future__ import annotations

import re
from pathlib import Path

from app.core.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def test_every_setting_is_documented_and_in_env_example() -> None:
    config_doc = (ROOT / "docs" / "CONFIGURATION.md").read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    documented = set(re.findall(r"^\| `([A-Z0-9_]+)` \|", config_doc, re.MULTILINE))
    templated = set(re.findall(r"^#? ?([A-Z0-9_]+)=", env_example, re.MULTILINE))
    expected = {name.upper() for name in Settings.model_fields}
    assert expected <= documented, f"undocumented: {sorted(expected - documented)}"
    # GIT_SHA is injected by CI/Docker, not something users set in .env
    assert expected - {"GIT_SHA"} <= templated, (
        f"missing in .env.example: {sorted(expected - templated)}"
    )
    assert documented <= expected, f"documented but unknown: {sorted(documented - expected)}"


def test_env_example_loads_cleanly() -> None:
    """The shipped template must itself be a valid configuration."""
    settings = Settings(_env_file=ROOT / ".env.example")
    assert settings.min_pii_safety == 1.0 and settings.eval_concurrency == 4


def test_version_is_consistent() -> None:
    from app import __version__

    assert f"## [{__version__}]" in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
