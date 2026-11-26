"""Target adapter: calls a live HTTP agent *or* the built-in reference simulator.

Key behaviours
--------------
* **No silent fallback.** If a live agent is selected and fails (timeout, 5xx, bad JSON) the
  case is reported as an *error* and the quality gate fails. Earlier versions quietly
  substituted simulator output, which could make a broken production agent look healthy.
* Retries with exponential back-off on transport errors, HTTP 429 and 5xx.
* A pooled ``httpx.AsyncClient`` (keep-alive) with optional ``Authorization: Bearer`` header.
* Real token usage is read from the agent's ``usage`` field when present; otherwise it is
  estimated from text length.
* The cache key contains a *fingerprint* of the target so simulator and live responses can
  never be mixed up.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import Settings
from app.models.schemas import EvalTestCase, TargetMode, TargetSource
from app.services.cache_service import LLMCacheService

logger = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 20_000
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class TargetConfigError(ValueError):
    """The requested target mode cannot be satisfied with the current configuration."""


class TargetCallError(RuntimeError):
    """A live target call failed after all retries."""


@dataclass
class TargetResponse:
    output_text: str
    status: str
    cache_hit: bool
    latency_ms: float
    prompt_tokens: int
    completion_tokens: int
    estimated_cost_usd: float
    source: TargetSource = "standalone"
    error: str | None = None


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~3.8 characters per token, language-agnostic)."""
    return max(1, int(len(text) / 3.8))


class AgentTargetAdapter:
    """Dispatches evaluation cases to a configured HTTP agent or the built-in simulator."""

    def __init__(
        self,
        settings: Settings,
        cache: LLMCacheService,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.cache = cache
        self._client = http_client
        self._owns_client = http_client is None

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------ routing
    def _endpoint_for(self, case: EvalTestCase) -> str:
        if case.target_type == "qa_agent":
            return self.settings.target_qa_endpoint_url
        if self.settings.target_workflow_endpoint_url:
            return self.settings.target_workflow_endpoint_url
        if self.settings.target_protocol == "openai_chat":
            return self.settings.target_qa_endpoint_url
        return ""

    def resolve_source(self, case: EvalTestCase, target_mode: TargetMode) -> TargetSource:
        endpoint = self._endpoint_for(case)
        if target_mode == "standalone":
            return "standalone"
        if target_mode == "live":
            if not endpoint:
                env = (
                    "TARGET_QA_ENDPOINT_URL"
                    if case.target_type == "qa_agent"
                    or self.settings.target_protocol == "openai_chat"
                    else "TARGET_WORKFLOW_ENDPOINT_URL"
                )
                raise TargetConfigError(
                    f"target_mode=live requires {env} for '{case.target_type}' cases."
                )
            if (
                self.settings.target_protocol == "openai_chat"
                and not self.settings.target_model.strip()
            ):
                raise TargetConfigError("target_protocol=openai_chat requires TARGET_MODEL.")
            return "live"
        if endpoint:
            if (
                self.settings.target_protocol == "openai_chat"
                and not self.settings.target_model.strip()
            ):
                raise TargetConfigError("target_protocol=openai_chat requires TARGET_MODEL.")
            return "live"
        return "standalone"

    def fingerprint(self, case: EvalTestCase, source: TargetSource) -> str:
        if source == "live":
            if self.settings.target_protocol == "openai_chat":
                return (
                    f"live|openai_chat|{self._endpoint_for(case)}|"
                    f"{self.settings.target_model.strip()}"
                )
            return f"live|{self._endpoint_for(case)}"
        # The simulator's answer depends on the whole case, not just the query text.
        return f"standalone|{case.case_id}"

    def cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return round(
            prompt_tokens / 1_000_000 * self.settings.price_input_per_1m
            + completion_tokens / 1_000_000 * self.settings.price_output_per_1m,
            6,
        )

    # ------------------------------------------------------------------ main entry point
    async def invoke(
        self, case: EvalTestCase, use_cache: bool = True, target_mode: TargetMode = "auto"
    ) -> TargetResponse:
        t0 = time.perf_counter()
        source = self.resolve_source(case, target_mode)
        key = self.cache.make_key(case.target_type, case.query, self.fingerprint(case, source))

        if use_cache:
            cached = await self.cache.get(key)
            if cached is not None:
                return TargetResponse(
                    output_text=cached["output_text"],
                    status=cached.get("status", ""),
                    cache_hit=True,
                    latency_ms=_elapsed_ms(t0),
                    prompt_tokens=0,
                    completion_tokens=0,
                    estimated_cost_usd=0.0,
                    source=source,
                )

        usage: dict[str, int] | None = None
        try:
            if source == "live":
                output_text, status, usage = await self._call_live(case)
            else:
                output_text, status = await self._run_standalone(case)
        except TargetCallError as exc:
            logger.warning("Live target failed for %s: %s", case.case_id, exc)
            return TargetResponse(
                output_text="",
                status="ERROR",
                cache_hit=False,
                latency_ms=_elapsed_ms(t0),
                prompt_tokens=0,
                completion_tokens=0,
                estimated_cost_usd=0.0,
                source=source,
                error=str(exc),
            )

        output_text = output_text[:MAX_OUTPUT_CHARS]
        p_toks = (usage or {}).get("prompt") or estimate_tokens(
            case.query + " " + case.reference_context
        )
        c_toks = (usage or {}).get("completion") or estimate_tokens(output_text)
        if use_cache:
            await self.cache.set(key, {"output_text": output_text, "status": status})
        return TargetResponse(
            output_text=output_text,
            status=status,
            cache_hit=False,
            latency_ms=_elapsed_ms(t0),
            prompt_tokens=p_toks,
            completion_tokens=c_toks,
            estimated_cost_usd=self.cost(p_toks, c_toks),
            source=source,
        )

    # ------------------------------------------------------------------ live HTTP agent
    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.settings.target_timeout_seconds),
                limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
            )
        return self._client

    def _auth_headers(self) -> dict[str, str]:
        token = self.settings.target_bearer_token.get_secret_value().strip()
        return {"Authorization": f"Bearer {token}"} if token else {}

    def _build_request_body(self, case: EvalTestCase) -> dict[str, Any]:
        if self.settings.target_protocol == "openai_chat":
            sys_prompt = self.settings.target_system_prompt
            if case.target_type == "workflow_agent":
                sys_prompt = (
                    f"{sys_prompt}\n"
                    "Evaluate the JSON workflow payload and reply with JSON only: "
                    '{"status": "APPROVED" | "REJECTED" | "ESCALATED_HITL", '
                    '"summary": "<concise decision rationale>"}.'
                )
            body: dict[str, Any] = {
                "model": self.settings.target_model.strip(),
                "temperature": self.settings.target_temperature,
                "messages": [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": case.query},
                ],
            }
            if case.target_type == "workflow_agent":
                body["response_format"] = {"type": "json_object"}
            return body
        return (
            {"query": case.query, "top_k": 4}
            if case.target_type == "qa_agent"
            else {"case_id": case.case_id, "payload": case.query}
        )

    def _retry_delay(self, attempt: int, response: httpx.Response | None = None) -> float:
        base = self.settings.target_retry_backoff_seconds
        if base <= 0:
            return 0.0
        if response is not None:
            retry_after = response.headers.get("retry-after", "").strip()
            if retry_after.isdigit():
                return min(float(retry_after), 30.0)
        return float(base * (2 ** (attempt - 1)) * random.uniform(0.8, 1.2))

    async def _call_live(self, case: EvalTestCase) -> tuple[str, str, dict[str, int] | None]:
        url = self._endpoint_for(case)
        body = self._build_request_body(case)
        client = self._get_client()
        attempts = self.settings.target_max_retries + 1
        last_error = "unknown error"
        last_resp: httpx.Response | None = None
        for attempt in range(1, attempts + 1):
            last_resp = None
            try:
                resp = await client.post(url, json=body, headers=self._auth_headers())
                if resp.status_code in _RETRYABLE_STATUS:
                    last_error = f"HTTP {resp.status_code}"
                    last_resp = resp
                else:
                    resp.raise_for_status()
                    return self._parse_live(case, resp.json(), self.settings.target_protocol)
            except httpx.TransportError as exc:
                last_error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            except httpx.HTTPStatusError as exc:
                raise TargetCallError(f"HTTP {exc.response.status_code} from target") from exc
            except ValueError as exc:  # invalid JSON body
                raise TargetCallError(f"Invalid JSON from target: {exc}") from exc
            if attempt < attempts:
                delay = self._retry_delay(attempt, last_resp)
                if delay > 0:
                    await asyncio.sleep(delay)
        raise TargetCallError(f"{last_error} (after {attempts} attempt(s))")

    @staticmethod
    def _parse_live(
        case: EvalTestCase, data: Any, protocol: str = "http_contract"
    ) -> tuple[str, str, dict[str, int] | None]:
        if not isinstance(data, dict):
            raise TargetCallError("Target response must be a JSON object.")
        usage_raw = data.get("usage")
        usage: dict[str, int] | None = None
        if isinstance(usage_raw, dict):
            usage = {
                "prompt": int(usage_raw.get("prompt_tokens") or usage_raw.get("input_tokens") or 0),
                "completion": int(
                    usage_raw.get("completion_tokens") or usage_raw.get("output_tokens") or 0
                ),
            }
        if protocol == "openai_chat":
            choices = data.get("choices")
            if not isinstance(choices, list) or not choices:
                raise TargetCallError("OpenAI-compatible response missing 'choices'.")
            msg = choices[0].get("message") if isinstance(choices[0], dict) else None
            content_raw = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content_raw, list):
                content = "".join(
                    str(part.get("text", "")) if isinstance(part, dict) else str(part)
                    for part in content_raw
                )
            else:
                content = str(content_raw or "")
            if case.target_type == "qa_agent":
                return content, "QA_OK", usage
            try:
                parsed = json.loads(content)
            except ValueError:
                return content, "COMPLETED", usage
            if isinstance(parsed, dict):
                summary = str(parsed.get("summary") or parsed.get("output") or content)
                status = str(parsed.get("status") or parsed.get("final_status") or "COMPLETED")
                return summary, status, usage
            return content, "COMPLETED", usage
        if case.target_type == "qa_agent":
            return str(data.get("answer") or data.get("output") or ""), "QA_OK", usage
        summary = data.get("summary") or data.get("output") or str(data)
        status = data.get("status") or data.get("final_status") or "COMPLETED"
        return str(summary), str(status), usage

    # ------------------------------------------------------------------ built-in simulator
    @staticmethod
    async def _run_standalone(case: EvalTestCase) -> tuple[str, str]:
        """Deterministic reference agent used to self-test the harness offline / in CI.

        It is an *oracle*: it replays each case's reference context, so it scores ~100% by
        construction. Its purpose is to prove the harness itself works - never to claim the
        quality of a real agent. Point TARGET_*_ENDPOINT_URL at a real agent for that.
        """
        await asyncio.sleep(0.012)

        if case.is_attack:
            if case.language == "ar":
                return (
                    "لا يمكن تنفيذ هذا الطلب وفق سياسة الأمان وحماية البيانات. "
                    "تم حظر محاولة تجاوز التعليمات تلقائياً.",
                    "BLOCKED",
                )
            return (
                "I cannot comply with this request. Under enterprise security and data privacy "
                "policy, attempts to override instructions or expose system prompts/PII are "
                "restricted.",
                "BLOCKED",
            )

        if case.target_type == "workflow_agent":
            status = case.expected_status or "APPROVED"
            return (
                f"Structured Workflow Verdict: {status}. "
                f"Checked rules: {', '.join(case.expected_keywords)}. "
                "Tax arithmetic, schema fields, and approval threshold validation completed.",
                status,
            )

        source_file = (
            "enterprise_policy_ar.txt" if case.language == "ar" else "enterprise_policy_en.txt"
        )
        if case.category == "pii_privacy":
            prefix = (
                "تم إخفاء البيانات الشخصية الحساسة تلقائياً [REDACTED_PII] قبل المعالجة. "
                if case.language == "ar"
                else "Sensitive personal data was automatically masked [REDACTED_PII] under "
                "enterprise privacy policy. "
            )
            return (
                f"{prefix}{case.reference_context} [Source: {source_file}, Page 2]",
                "PII_REDACTED",
            )
        return f"{case.reference_context} [Source: {source_file}, Page 1]", "QA_OK"


def _elapsed_ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000.0, 2)
