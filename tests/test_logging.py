from __future__ import annotations

import json
import logging

import pytest
from app.core.logging import JsonFormatter, configure_logging, request_id_ctx


def test_json_formatter_includes_request_id_extras_and_exceptions() -> None:
    token = request_id_ctx.set("req-42")
    try:
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            record = logging.LogRecord(
                "x", logging.ERROR, __file__, 1, "failed %s", ("job",), sys.exc_info()
            )
        record.duration_ms = 12.5
        payload = json.loads(JsonFormatter().format(record))
    finally:
        request_id_ctx.reset(token)
    assert payload["msg"] == "failed job" and payload["request_id"] == "req-42"
    assert payload["duration_ms"] == 12.5 and "ValueError: boom" in payload["exc"]
    assert payload["level"] == "ERROR" and payload["ts"].endswith("+00:00")


def test_configure_logging_is_idempotent_and_keeps_foreign_handlers() -> None:
    root = logging.getLogger()
    foreign = logging.NullHandler()
    root.addHandler(foreign)
    try:
        configure_logging("INFO", "json")
        configure_logging("DEBUG", "text")
        ours = [h for h in root.handlers if getattr(h, "_llmops", False)]
        assert len(ours) == 1 and foreign in root.handlers
        assert root.level == logging.DEBUG
    finally:
        root.removeHandler(foreign)
        configure_logging("INFO", "text")


def test_request_id_defaults_to_dash(caplog: pytest.LogCaptureFixture) -> None:
    assert request_id_ctx.get() == "-"
