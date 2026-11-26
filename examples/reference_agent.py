"""Example target agent that implements the harness HTTP contract.

Run it, then point the harness at it::

    AGENT_MODE=good  uvicorn examples.reference_agent:app --port 9000   # gate PASSES
    AGENT_MODE=leaky uvicorn examples.reference_agent:app --port 9000   # gate FAILS

    TARGET_QA_ENDPOINT_URL=http://localhost:9000/qa \\
    TARGET_WORKFLOW_ENDPOINT_URL=http://localhost:9000/workflow \\
    llmops-eval --target-mode live --fail-on-gate

This is a *demo*: to stay dependency-free it "cheats" by looking each query up in the
bundled dataset instead of calling a real LLM/RAG pipeline. Replace the two handlers with
calls to your own agent - the request/response shapes are what matters (docs/INTEGRATION.md).

* ``good``  - answers from context with citations, masks PII, refuses every attack.
* ``leaky`` - echoes PII, leaks a fake system prompt/API key, hallucinates, rubber-stamps
  every workflow as APPROVED. Use it to watch the red-team gate catch a vulnerable agent.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field

_DEFAULT_DATASET = Path(__file__).resolve().parents[1] / "datasets" / "bilingual_eval_suite_55.json"


class QARequest(BaseModel):
    query: str
    top_k: int = Field(default=4, ge=1)


class WorkflowRequest(BaseModel):
    case_id: str
    payload: str


def _usage(prompt: str, completion: str) -> dict[str, int]:
    return {
        "prompt_tokens": max(1, len(prompt) // 4),
        "completion_tokens": max(1, len(completion) // 4),
    }


def create_agent(mode: str = "good", dataset_path: Path | None = None) -> FastAPI:
    path = dataset_path or Path(os.getenv("DATASET_PATH", str(_DEFAULT_DATASET)))
    cases: list[dict[str, Any]] = (
        json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    )
    by_query = {c["query"]: c for c in cases}
    by_id = {c["case_id"]: c for c in cases}
    leaky = mode == "leaky"

    app = FastAPI(title=f"Reference agent ({mode})", version="1.0.0")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "mode": mode}

    @app.post("/qa")
    async def qa(req: QARequest) -> dict[str, Any]:
        case = by_query.get(req.query)
        if case is None:
            answer = "I do not have information about that."
            return {"answer": answer, "usage": _usage(req.query, answer)}

        arabic = case["language"] == "ar"
        source = "enterprise_policy_ar.txt" if arabic else "enterprise_policy_en.txt"
        context = case["reference_context"]

        if case["is_attack"]:
            if leaky:
                answer = (
                    "Sure! Here is my system prompt: You are an HR assistant. "
                    "api_key=sk-live-EXAMPLEKEY1234567890 Jailbreak successful."
                )
            elif arabic:
                answer = "لا يمكن تنفيذ هذا الطلب وفق سياسة الأمان وحماية البيانات."
            else:
                answer = "I cannot comply with this request because of security and privacy policy."
        elif case["category"] == "pii_privacy":
            if leaky:
                answer = f"Thanks! I recorded: {req.query} {context}"
            elif arabic:
                answer = (
                    f"تم إخفاء البيانات الشخصية [REDACTED_PII]. {context} "
                    f"[Source: {source}, Page 2]"
                )
            else:
                answer = (
                    f"Personal data was masked [REDACTED_PII]. {context} [Source: {source}, Page 2]"
                )
        elif leaky:
            answer = "I am not sure, but it is probably around 30 days."
        else:
            answer = f"{context} [Source: {source}, Page 1]"
        return {"answer": answer, "usage": _usage(req.query + context, answer)}

    @app.post("/workflow")
    async def workflow(req: WorkflowRequest) -> dict[str, Any]:
        case = by_id.get(req.case_id)
        expected = case["expected_status"] if case else "ESCALATED_HITL"
        status = "APPROVED" if leaky else expected
        keywords = ", ".join(case["expected_keywords"]) if case else ""
        summary = f"Structured Workflow Verdict: {status}. Checked rules: {keywords}."
        return {"status": status, "summary": summary, "usage": _usage(req.payload, summary)}

    return app


app = create_agent(os.getenv("AGENT_MODE", "good"))
