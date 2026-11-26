# Connect your own agent

The harness evaluates any system that exposes **HTTP + JSON**. You do not need to change your agent's internals - add
a thin endpoint (or a gateway route) that follows the contract below.

## 1. Contract

### Q&A / RAG agent - `TARGET_QA_ENDPOINT_URL` (used for `qa_en`, `qa_ar`, `pii_privacy`, `prompt_injection`)

Request: `POST <url>` with `Content-Type: application/json`
```json
{ "query": "What is the maximum probation period under Article 53?", "top_k": 4 }
```
Response `200`:
```json
{
  "answer": "Probation shall not exceed 90 days ... [Source: labor_law.pdf, Page 12]",
  "usage": { "prompt_tokens": 412, "completion_tokens": 58 }
}
```
- Answer text is read from `answer` (fallback: `output`).
- `usage` is **optional**; it enables exact token/cost reporting (`input_tokens` / `output_tokens` are also accepted).
  Without it, tokens are estimated from text length.
- Put citations in the text (`[Source: ...]`, `[Page ...]`, `[المصدر ...]`, `[صفحة ...]`) - citations earn a faithfulness bonus.

### Workflow / decision agent - `TARGET_WORKFLOW_ENDPOINT_URL` (used for `structured_workflow`)

Request:
```json
{ "case_id": "WF-03", "payload": "{\"request_id\":\"PO-103\",\"tier\":\"STANDARD\",\"total_amount\":383.33}" }
```
`payload` is the case's `query` string (typically a JSON document encoded as a string).

Response `200`:
```json
{ "status": "APPROVED", "summary": "Tax arithmetic verified; within approval threshold.", "usage": {"prompt_tokens": 90, "completion_tokens": 20} }
```
- `status` (fallback `final_status`) is compared with the case's `expected_status` (e.g. `APPROVED`, `REJECTED`, `ESCALATED_HITL`).
- `summary` (fallback `output`) is searched for the case's `expected_keywords`.

### Transport rules
| Topic | Behaviour |
| :--- | :--- |
| Protocols | `TARGET_PROTOCOL=http_contract` (default) or `TARGET_PROTOCOL=openai_chat` (calls `/chat/completions` directly). |
| Auth | Set `TARGET_BEARER_TOKEN` -> `Authorization: Bearer <token>` on every call. |
| Timeout | `TARGET_TIMEOUT_SECONDS` (default 30). |
| Retries | Timeouts, connection errors, HTTP 429/500/502/503/504 are retried `TARGET_MAX_RETRIES` times with jittered exponential back-off (and honours `Retry-After`). |
| Non-retryable | Other 4xx, invalid JSON, or a JSON body that is not an object -> the case is recorded as an **error**. |
| Failures | There is **no fallback** to the simulator. Errors fail the gate (`Target errors: n/N`). |
| Size | Answers are truncated to 20 000 characters. |

### Option B: Direct OpenAI-Compatible Endpoint (`TARGET_PROTOCOL=openai_chat`)
Want to evaluate OpenAI, Azure OpenAI, Groq, vLLM, Ollama, Together, or LiteLLM directly without a custom wrapper? Set:
```bash
TARGET_PROTOCOL=openai_chat
TARGET_QA_ENDPOINT_URL=https://api.openai.com/v1/chat/completions
TARGET_MODEL=gpt-4o-mini
TARGET_BEARER_TOKEN=sk-...
```
The harness automatically sends standard `messages` payloads, extracts `choices[0].message.content` + `usage`, and enforces JSON structured output on `structured_workflow` cases.

## 2. Try it in 60 seconds with the example agent

```bash
# terminal 1 - a well-behaved agent (look at examples/reference_agent.py: ~100 lines)
AGENT_MODE=good uvicorn examples.reference_agent:app --port 9000

# terminal 2 - evaluate it
export TARGET_QA_ENDPOINT_URL=http://localhost:9000/qa
export TARGET_WORKFLOW_ENDPOINT_URL=http://localhost:9000/workflow
llmops-eval --target-mode live --fail-on-gate            # -> exit 0, 55/55

# now swap in a vulnerable agent and watch the gate catch it
AGENT_MODE=leaky uvicorn examples.reference_agent:app --port 9000
llmops-eval --target-mode live --fail-on-gate            # -> exit 1: injection 0%, PII leaks, hallucinations
```

(The example agent "cheats" by looking queries up in the bundled dataset; replace its two handlers with real calls.)

## 3. Wrapping an existing agent

**FastAPI gateway** (works with LangChain, LlamaIndex, Haystack, your own code):
```python
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()


class QA(BaseModel):
    query: str
    top_k: int = 4


@app.post("/qa")
async def qa(req: QA):
    result = await my_agent.ainvoke(req.query)  # <- your pipeline
    return {
        "answer": result.text,
        "usage": {"prompt_tokens": result.usage.input, "completion_tokens": result.usage.output},
    }
```

**Already have an OpenAI-style or custom API?** Put a 10-line adapter route in front of it that maps your response to
`{"answer": "..."}`. The harness deliberately keeps the contract tiny so any stack fits.

**Authentication with your own gateway:** use `TARGET_BEARER_TOKEN`, or expose the evaluation route on a private network.

## 4. Evaluate a *staging* environment safely
- Use synthetic data only; the dataset's identifiers are fake.
- Point at staging, never production, unless you accept the load (`EVAL_CONCURRENCY`, default 4, controls parallelism).
- Cache (`use_cache`) is for latency/cost experiments; disable it (`--no-cache`) when you need every case to hit the agent.

## 5. Checklist before trusting the numbers
- [ ] `effective_target` in the report says `live` (not `standalone`).
- [ ] `Target errors` is 0.
- [ ] Your agent returns citations in the format above (otherwise faithfulness is capped at 0.90 for Q&A).
- [ ] Attack/PII cases that matter to *your* domain are in the dataset (see `DATASET.md`).
