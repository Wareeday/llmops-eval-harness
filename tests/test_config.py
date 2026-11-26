from __future__ import annotations

import pytest
from app.core.config import Settings
from pydantic import ValidationError


def test_defaults_are_valid() -> None:
    cfg = Settings(_env_file=None)
    assert cfg.port == 7860
    assert cfg.min_pii_safety == 1.0
    assert cfg.cors_origins == []
    assert cfg.langfuse_configured is False


def test_environment_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIN_FAITHFULNESS", "0.9")
    monkeypatch.setenv("EVAL_CONCURRENCY", "8")
    monkeypatch.setenv("LOG_FORMAT", "json")
    cfg = Settings(_env_file=None)
    assert (cfg.min_faithfulness, cfg.eval_concurrency, cfg.log_format) == (0.9, 8, "json")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("min_faithfulness", 1.5),
        ("port", 70000),
        ("eval_concurrency", 0),
        ("target_qa_endpoint_url", "ftp://agent"),
        ("alert_webhook_url", "ftp://hooks.example"),
        ("target_protocol", "grpc"),
        ("log_level", "LOUD"),
    ],
)
def test_invalid_values_are_rejected(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})  # type: ignore[arg-type]


def test_endpoint_is_normalised() -> None:
    cfg = Settings(_env_file=None, target_qa_endpoint_url="  https://agent.example/qa/  ")
    assert cfg.target_qa_endpoint_url == "https://agent.example/qa"


def test_secrets_never_appear_in_repr() -> None:
    cfg = Settings(
        _env_file=None,
        api_key="top-secret-key",
        redis_url="rediss://default:pw123@host:6379",
        langfuse_secret_key="sk-lf-abc",
        judge_api_key="judge-secret",
        alert_webhook_url="https://hooks.slack.com/services/T0/B0/SECRET99",
    )
    text = repr(cfg) + str(cfg)
    for secret in ("top-secret-key", "pw123", "sk-lf-abc", "judge-secret", "SECRET99"):
        assert secret not in text


def test_cors_origins_parsing() -> None:
    cfg = Settings(_env_file=None, cors_allow_origins=" https://a.com, https://b.com ,,")
    assert cfg.cors_origins == ["https://a.com", "https://b.com"]


def test_legacy_groq_settings_are_honoured() -> None:
    cfg = Settings(_env_file=None, groq_api_key="gk", groq_judge_model="legacy-model")
    assert cfg.effective_judge_api_key == "gk"
    assert cfg.effective_judge_model == "legacy-model"
    cfg2 = Settings(_env_file=None, groq_api_key="gk", judge_api_key="new", judge_model="m2")
    assert (cfg2.effective_judge_api_key, cfg2.effective_judge_model) == ("new", "m2")


def test_git_sha_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_SHA", "abc123")
    assert Settings(_env_file=None).git_sha == "abc123"
