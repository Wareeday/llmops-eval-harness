from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
from app.core.config import Settings
from app.models.schemas import EvalTestCase
from app.services.cache_service import LLMCacheService
from app.services.target_adapter import (
    MAX_OUTPUT_CHARS,
    AgentTargetAdapter,
    TargetConfigError,
)

MakeCase = Callable[..., EvalTestCase]
Handler = Callable[[httpx.Request], httpx.Response]


def make_adapter(
    handler: Handler | None = None, **cfg: object
) -> tuple[AgentTargetAdapter, LLMCacheService]:
    values: dict[str, object] = {
        "target_qa_endpoint_url": "http://agent/qa",
        "target_workflow_endpoint_url": "http://agent/wf",
        "target_retry_backoff_seconds": 0.0,
    }
    values.update(cfg)
    settings = Settings(_env_file=None, **values)  # type: ignore[arg-type]
    cache = LLMCacheService()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler)) if handler else None
    return AgentTargetAdapter(settings, cache, http_client=client), cache


async def test_standalone_invoke_then_cache_hit(make_case: MakeCase) -> None:
    adapter, _ = make_adapter(target_qa_endpoint_url="")
    case = make_case()
    first = await adapter.invoke(case, use_cache=True, target_mode="auto")
    second = await adapter.invoke(case, use_cache=True, target_mode="auto")
    assert first.source == "standalone" and not first.cache_hit and first.prompt_tokens > 0
    assert second.cache_hit and second.prompt_tokens == 0 and second.estimated_cost_usd == 0.0
    assert second.output_text == first.output_text


async def test_use_cache_false_never_reads_or_writes(make_case: MakeCase) -> None:
    adapter, cache = make_adapter(target_qa_endpoint_url="")
    await adapter.invoke(make_case(), use_cache=False)
    assert (await cache.stats()).cached_keys == 0


async def test_live_success_parses_answer_usage_and_sends_auth(make_case: MakeCase) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"answer": "90 days", "usage": {"prompt_tokens": 120, "completion_tokens": 7}}
        )

    adapter, _ = make_adapter(handler, target_bearer_token="tok")
    res = await adapter.invoke(make_case(), use_cache=False)
    assert res.source == "live" and res.output_text == "90 days" and res.error is None
    assert (res.prompt_tokens, res.completion_tokens) == (120, 7)
    assert seen["auth"] == "Bearer tok"
    assert seen["body"] == {"query": "What is the probation period?", "top_k": 4}


async def test_live_workflow_contract(make_case: MakeCase) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["case_id"] == "WF-1"
        return httpx.Response(200, json={"summary": "done", "final_status": "APPROVED"})

    adapter, _ = make_adapter(handler)
    case = make_case(case_id="WF-1", target_type="workflow_agent", category="structured_workflow",
                     expected_status="APPROVED")  # fmt: skip
    res = await adapter.invoke(case, use_cache=False)
    assert (res.output_text, res.status) == ("done", "APPROVED")


async def test_retries_then_succeeds(make_case: MakeCase) -> None:
    calls = {"n": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] < 3 else httpx.Response(200, json={"answer": "ok"})

    adapter, _ = make_adapter(handler, target_max_retries=2)
    res = await adapter.invoke(make_case(), use_cache=False)
    assert res.error is None and res.output_text == "ok" and calls["n"] == 3


async def test_transport_errors_are_retried_then_reported(make_case: MakeCase) -> None:
    calls = {"n": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("refused")

    adapter, cache = make_adapter(handler, target_max_retries=1)
    res = await adapter.invoke(make_case(), use_cache=True)
    assert calls["n"] == 2
    assert res.error and "ConnectError" in res.error and "2 attempt" in res.error
    assert res.output_text == "" and res.status == "ERROR"
    assert (await cache.stats()).cached_keys == 0  # failures are never cached


async def test_no_silent_fallback_to_simulator(make_case: MakeCase) -> None:
    adapter, _ = make_adapter(lambda _: httpx.Response(502), target_max_retries=0)
    res = await adapter.invoke(make_case(), use_cache=False, target_mode="auto")
    assert res.source == "live" and res.error and res.output_text == ""


@pytest.mark.parametrize(
    ("response", "needle"),
    [
        (httpx.Response(400), "HTTP 400"),
        (httpx.Response(200, content=b"<html>not json"), "Invalid JSON"),
        (httpx.Response(200, json=["list"]), "JSON object"),
    ],
)
async def test_non_retryable_failures(
    make_case: MakeCase, response: httpx.Response, needle: str
) -> None:
    calls = {"n": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return response

    adapter, _ = make_adapter(handler, target_max_retries=3)
    res = await adapter.invoke(make_case(), use_cache=False)
    assert res.error and needle in res.error and calls["n"] == 1


async def test_live_mode_requires_endpoint(make_case: MakeCase) -> None:
    adapter, _ = make_adapter(target_qa_endpoint_url="")
    with pytest.raises(TargetConfigError, match="TARGET_QA_ENDPOINT_URL"):
        await adapter.invoke(make_case(), target_mode="live")
    wf = make_case(target_type="workflow_agent", category="structured_workflow")
    adapter2, _ = make_adapter(target_workflow_endpoint_url="")
    with pytest.raises(TargetConfigError, match="TARGET_WORKFLOW_ENDPOINT_URL"):
        adapter2.resolve_source(wf, "live")


async def test_auto_mode_prefers_live_only_when_configured(make_case: MakeCase) -> None:
    configured, _ = make_adapter()
    assert configured.resolve_source(make_case(), "auto") == "live"
    assert configured.resolve_source(make_case(), "standalone") == "standalone"
    unconfigured, _ = make_adapter(target_qa_endpoint_url="")
    assert unconfigured.resolve_source(make_case(), "auto") == "standalone"


async def test_cache_never_mixes_simulator_and_live(make_case: MakeCase) -> None:
    adapter, cache = make_adapter(lambda _: httpx.Response(200, json={"answer": "LIVE ANSWER"}))
    case = make_case()
    sim = await adapter.invoke(case, use_cache=True, target_mode="standalone")
    live = await adapter.invoke(case, use_cache=True, target_mode="live")
    assert not live.cache_hit and live.output_text == "LIVE ANSWER" != sim.output_text
    assert (await cache.stats()).cached_keys == 2


async def test_output_is_truncated(make_case: MakeCase) -> None:
    adapter, _ = make_adapter(
        lambda _: httpx.Response(200, json={"answer": "x" * (MAX_OUTPUT_CHARS * 2)})
    )
    res = await adapter.invoke(make_case(), use_cache=False)
    assert len(res.output_text) == MAX_OUTPUT_CHARS


async def test_cost_uses_configured_pricing(make_case: MakeCase) -> None:
    adapter, _ = make_adapter(price_input_per_1m=1.0, price_output_per_1m=2.0)
    assert adapter.cost(1_000_000, 500_000) == 2.0


async def test_owned_client_is_created_and_closed(make_case: MakeCase) -> None:
    adapter, _ = make_adapter(target_qa_endpoint_url="http://127.0.0.1:1/qa", target_max_retries=0)
    res = await adapter.invoke(make_case(), use_cache=False)
    assert res.error
    await adapter.aclose()


async def test_openai_chat_protocol_qa_and_workflow(make_case: MakeCase) -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if "response_format" in body:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": '{"status": "APPROVED", "summary": "Tax ok"}'}}
                    ],
                    "usage": {"prompt_tokens": 50, "completion_tokens": 12},
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": [{"text": "90 days [Source: law, Page 1]"}]}}],
                "usage": {"prompt_tokens": 40, "completion_tokens": 10},
            },
        )

    adapter, _ = make_adapter(
        handler,
        target_protocol="openai_chat",
        target_model="gpt-4o-mini",
        target_workflow_endpoint_url="",
    )
    qa_res = await adapter.invoke(make_case(), use_cache=False)
    assert qa_res.output_text == "90 days [Source: law, Page 1]" and qa_res.prompt_tokens == 40
    assert seen[0]["model"] == "gpt-4o-mini"

    wf_case = make_case(
        case_id="WF-1",
        target_type="workflow_agent",
        category="structured_workflow",
        expected_status="APPROVED",
    )
    wf_res = await adapter.invoke(wf_case, use_cache=False)
    assert wf_res.status == "APPROVED" and wf_res.output_text == "Tax ok"
    assert "openai_chat" in adapter.fingerprint(wf_case, "live")


async def test_openai_chat_protocol_errors_and_validation(make_case: MakeCase) -> None:
    adapter_no_model, _ = make_adapter(target_protocol="openai_chat", target_model="")
    with pytest.raises(TargetConfigError, match="TARGET_MODEL"):
        adapter_no_model.resolve_source(make_case(), "live")
    with pytest.raises(TargetConfigError, match="TARGET_MODEL"):
        adapter_no_model.resolve_source(make_case(), "auto")

    bad_choices, _ = make_adapter(
        lambda _: httpx.Response(200, json={"choices": []}),
        target_protocol="openai_chat",
        target_model="m",
    )
    res_bad = await bad_choices.invoke(make_case(), use_cache=False)
    assert res_bad.error and "choices" in res_bad.error

    non_json_wf, _ = make_adapter(
        lambda _: httpx.Response(200, json={"choices": [{"message": {"content": "APPROVED ok"}}]}),
        target_protocol="openai_chat",
        target_model="m",
    )
    wf_case = make_case(
        case_id="WF-2",
        target_type="workflow_agent",
        category="structured_workflow",
        expected_status="APPROVED",
    )
    res_plain = await non_json_wf.invoke(wf_case, use_cache=False)
    assert res_plain.status == "COMPLETED" and res_plain.output_text == "APPROVED ok"


def test_retry_delay_respects_retry_after_and_jitter() -> None:
    adapter, _ = make_adapter(target_retry_backoff_seconds=0.5)
    resp = httpx.Response(429, headers={"Retry-After": "3"})
    assert adapter._retry_delay(1, resp) == 3.0
    delay = adapter._retry_delay(2, httpx.Response(503))
    assert 0.8 <= delay <= 1.2
