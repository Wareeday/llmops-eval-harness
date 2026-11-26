from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.core.config import Settings
from app.models.schemas import EvalRunRequest
from app.services.observability import LangfuseExporter, ObservabilityService
from app.services.runner import EvaluationRunner

SettingsFactory = Callable[..., Settings]


async def _run(runner: EvaluationRunner, **kw: Any) -> str:
    return (await runner.run_suite(EvalRunRequest(use_cache=False, **kw))).run_id


async def test_roundtrip_and_filtering(settings: Settings) -> None:
    runner = EvaluationRunner(settings)
    r1 = await _run(runner, categories=["pii_privacy"])
    r2 = await _run(runner, categories=["prompt_injection"])
    obs = runner.obs
    assert obs.count_runs() == 2 and obs.ping()
    assert [r["run_id"] for r in obs.list_runs()] == [r2, r1]
    assert obs.get_run(r1) is not None and obs.get_run("nope") is None
    assert len(obs.list_traces(run_id=r1)) == 10 and len(obs.list_traces(limit=5)) == 5
    assert all(not t.langfuse_exported for t in obs.list_traces(run_id=r1))
    await runner.aclose()


async def test_retention_prunes_old_runs_and_their_traces(
    settings_factory: SettingsFactory,
) -> None:
    runner = EvaluationRunner(settings_factory(max_stored_runs=2))
    first = await _run(runner, case_ids=["QA-EN-01"])
    await _run(runner, case_ids=["QA-EN-02"])
    third = await _run(runner, case_ids=["QA-EN-03"])
    assert runner.obs.count_runs() == 2
    assert runner.obs.get_run(first) is None and runner.obs.get_run(third) is not None
    assert runner.obs.list_traces(run_id=first) == []
    await runner.aclose()


class FakeLangfuseV3:
    def __init__(self) -> None:
        self.spans: list[dict[str, Any]] = []
        self.scores: list[dict[str, Any]] = []
        self.flushed = self.closed = False

    def create_trace_id(self, seed: str) -> str:
        return f"tid-{seed}"

    def start_observation(self, **kw: Any) -> Any:
        self.spans.append(kw)
        return type("Span", (), {"end": lambda self: None})()

    def create_score(self, **kw: Any) -> None:
        self.scores.append(kw)

    def flush(self) -> None:
        self.flushed = True

    def shutdown(self) -> None:
        self.closed = True


class FakeLangfuseV2:
    def __init__(self) -> None:
        self.traces: list[Any] = []

    def trace(self, **kw: Any) -> Any:
        scores: list[tuple[str, float]] = []
        t = type(
            "T",
            (),
            {
                "id": "t2",
                "scores": scores,
                "score": lambda self, name, value: scores.append((name, value)),
            },
        )()
        self.traces.append((kw, t))
        return t

    def flush(self) -> None:
        pass


async def test_langfuse_v3_export(settings: Settings) -> None:
    fake = FakeLangfuseV3()
    runner = EvaluationRunner(settings, langfuse_client=fake)
    run_id = await _run(runner, case_ids=["QA-EN-01", "RED-01"])
    assert runner.obs.langfuse_enabled
    assert len(fake.spans) == 2 and len(fake.scores) == 10  # 5 scores per case
    assert fake.spans[0]["trace_context"]["trace_id"].startswith(f"tid-{run_id}")
    assert {s["name"] for s in fake.scores} == {
        "faithfulness",
        "relevancy",
        "pii_safe",
        "injection_safe",
        "passed",
    }
    assert all(t.langfuse_exported for t in runner.obs.list_traces(run_id=run_id))
    assert fake.flushed
    await runner.aclose()
    assert fake.closed


async def test_langfuse_v2_export(settings: Settings) -> None:
    fake = FakeLangfuseV2()
    runner = EvaluationRunner(settings, langfuse_client=fake)
    await _run(runner, case_ids=["QA-EN-01"])
    kw, trace = fake.traces[0]
    assert kw["name"] == "eval:qa_en:QA-EN-01" and len(trace.scores) == 5
    assert LangfuseExporter(fake).api == "v2"
    await runner.aclose()


async def test_langfuse_failures_never_break_a_run(settings: Settings) -> None:
    class Exploding(FakeLangfuseV3):
        def start_observation(self, **kw: Any) -> Any:
            raise RuntimeError("langfuse down")

    runner = EvaluationRunner(settings, langfuse_client=Exploding())
    run_id = await _run(runner, case_ids=["QA-EN-01"])
    assert [t.langfuse_exported for t in runner.obs.list_traces(run_id=run_id)] == [False]
    await runner.aclose()


def test_exporter_absent_without_keys(settings: Settings) -> None:
    assert LangfuseExporter.from_settings(settings) is None
    assert not ObservabilityService(settings).langfuse_enabled


def test_exporter_built_from_keys_with_real_sdk(settings_factory: SettingsFactory) -> None:
    cfg = settings_factory(
        langfuse_public_key="pk-lf-test",
        langfuse_secret_key="sk-lf-test",
        langfuse_host="http://127.0.0.1:9",
    )
    exporter = LangfuseExporter.from_settings(cfg)
    assert exporter is not None and exporter.api in {"v2", "v3"}
