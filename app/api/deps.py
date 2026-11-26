"""FastAPI dependencies: runner access and optional API-key authentication."""

from __future__ import annotations

import hmac

from fastapi import HTTPException, Request, status

from app.services.runner import EvaluationRunner


def get_runner(request: Request) -> EvaluationRunner:
    runner: EvaluationRunner = request.app.state.runner
    return runner


async def require_api_key(request: Request) -> None:
    """Enforce ``X-API-Key`` when ``API_KEY`` is configured (constant-time comparison)."""
    expected = request.app.state.settings.api_key.get_secret_value()
    if not expected:
        return
    supplied = request.headers.get("x-api-key", "")
    if not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key.",
            headers={"WWW-Authenticate": "ApiKey"},
        )
