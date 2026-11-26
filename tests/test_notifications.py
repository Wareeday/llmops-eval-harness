from __future__ import annotations

from collections.abc import Callable

import httpx
from app.core.config import Settings
from app.services.notifications import send_alert_webhook

from tests.test_comparison_and_reporting import make_summary

SettingsFactory = Callable[..., Settings]


async def test_webhook_skipped_when_unconfigured_or_passed(
    settings_factory: SettingsFactory,
) -> None:
    cfg = settings_factory()
    assert await send_alert_webhook(cfg, make_summary()) is False

    cfg_url = settings_factory(alert_webhook_url="https://hooks.example/test", alert_on_pass=False)
    assert await send_alert_webhook(cfg_url, make_summary(quality_gate_passed=True)) is False


async def test_webhook_sent_on_failure_and_optional_pass(
    settings_factory: SettingsFactory,
) -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.append(json.loads(request.content))
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cfg = settings_factory(alert_webhook_url="https://hooks.example/test", alert_on_pass=False)
    failed = make_summary(quality_gate_passed=False, quality_gate_reasons=["Faithfulness low"])
    assert await send_alert_webhook(cfg, failed, http_client=client) is True
    assert len(seen) == 1 and "FAIL" in str(seen[0]["text"])

    cfg_always = settings_factory(
        alert_webhook_url="https://hooks.example/test", alert_on_pass=True
    )
    assert await send_alert_webhook(cfg_always, make_summary(), http_client=client) is True
    assert len(seen) == 2 and "PASS" in str(seen[1]["text"])
    await client.aclose()


async def test_webhook_failures_are_non_fatal(settings_factory: SettingsFactory) -> None:
    def boom(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable")

    cfg = settings_factory(alert_webhook_url="https://hooks.example/test", alert_on_pass=True)
    client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    assert await send_alert_webhook(cfg, make_summary(), http_client=client) is False
    await client.aclose()
