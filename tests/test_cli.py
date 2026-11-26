from __future__ import annotations

import json
import runpy
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from app.cli import main

from tests.conftest import DATASET


@pytest.fixture(autouse=True)
def _cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "cli.db"))
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("DATASET_PATH", str(DATASET))
    monkeypatch.chdir(tmp_path)  # no stray .env


def test_successful_run_writes_reports(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--no-cache", "--fail-on-gate"]) == 0
    out = capsys.readouterr()
    assert "Quality Gate" in out.out and "PASS" in out.out
    assert (tmp_path / "reports" / "eval_report.md").exists()
    assert (tmp_path / "reports" / "eval_report.json").exists()


def test_json_output_and_filters(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--json", "--categories", "qa_en", "--concurrency", "2"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["total_cases"] == 12 and data["concurrency"] == 2


def test_gate_failure_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MAX_P95_LATENCY_MS", "0.001")
    assert main(["--no-cache"]) == 0  # failing gate without --fail-on-gate never breaks the build
    assert main(["--no-cache", "--fail-on-gate"]) == 1
    assert "CI QUALITY GATE FAILED" in capsys.readouterr().err


def test_junit_baseline_and_regression(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    baseline = tmp_path / "base" / "main.json"
    junit = tmp_path / "out" / "junit.xml"
    csv_out = tmp_path / "out" / "results.csv"
    assert (
        main(
            [
                "--no-cache",
                "--save-baseline",
                str(baseline),
                "--junit",
                str(junit),
                "--csv",
                str(csv_out),
            ]
        )
        == 0
    )
    assert ET.parse(junit).getroot().get("tests") == "55"  # noqa: S314
    csv_lines = csv_out.read_text(encoding="utf-8").splitlines()
    assert len(csv_lines) == 56 and csv_lines[0].startswith("run_id,case_id,category")

    assert main(["--no-cache", "--baseline", str(baseline), "--fail-on-gate"]) == 0
    assert "No regression" in capsys.readouterr().out

    tampered = json.loads(baseline.read_text(encoding="utf-8"))
    tampered["avg_faithfulness"] = 1.5  # pretend the baseline was far better than today
    baseline.write_text(json.dumps(tampered), encoding="utf-8")
    assert main(["--no-cache", "--baseline", str(baseline), "--fail-on-gate"]) == 1
    assert "REGRESSION" in capsys.readouterr().out


def test_unreadable_baseline_is_a_config_error(tmp_path: Path) -> None:
    assert main(["--baseline", str(tmp_path / "missing.json")]) == 2


def test_validate_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--validate-only"]) == 0
    assert "Dataset OK: 55 cases" in capsys.readouterr().out
    bad = tmp_path / "bad.json"
    bad.write_text("[not json", encoding="utf-8")
    assert main(["--validate-only", "--dataset", str(bad)]) == 2
    assert main(["--dataset", str(bad)]) == 2


@pytest.mark.parametrize(
    "args",
    [["--case-ids", "NOPE"], ["--target-mode", "live"]],
)
def test_configuration_errors_exit_2(args: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(args) == 2
    assert "Configuration error" in capsys.readouterr().err


def test_invalid_environment_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MIN_FAITHFULNESS", "7")
    assert main([]) == 2
    assert "Invalid configuration" in capsys.readouterr().err


def test_github_step_summary_is_appended(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    summary = tmp_path / "step_summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert main(["--categories", "pii_privacy"]) == 0
    assert "LLMOps Automated Evaluation Report" in summary.read_text(encoding="utf-8")


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    from app import __version__

    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0 and __version__ in capsys.readouterr().out


def test_legacy_cli_eval_shim(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.argv", ["cli_eval.py", "--validate-only"])
    with pytest.raises(SystemExit) as shim:
        runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "cli_eval.py"), run_name="__main__"
        )
    assert shim.value.code == 0 and "Dataset OK" in capsys.readouterr().out
