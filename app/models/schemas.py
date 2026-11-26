"""Pydantic v2 schemas for evaluation cases, results, runs, caching and tracing."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Category = Literal["qa_en", "qa_ar", "pii_privacy", "prompt_injection", "structured_workflow"]
TargetType = Literal["qa_agent", "workflow_agent"]
TargetMode = Literal["auto", "standalone", "live"]
TargetSource = Literal["standalone", "live"]

CATEGORIES: tuple[str, ...] = (
    "qa_en",
    "qa_ar",
    "pii_privacy",
    "prompt_injection",
    "structured_workflow",
)


class EvalTestCase(BaseModel):
    """One test case of a benchmark suite (unknown keys are rejected to catch typos)."""

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(..., min_length=1)
    target_type: TargetType
    category: Category
    language: Literal["ar", "en", "mixed"]
    query: str = Field(..., min_length=1)
    reference_context: str = ""
    expected_keywords: list[str] = Field(default_factory=list)
    expected_status: str = ""
    forbidden_patterns: list[str] = Field(default_factory=list)
    is_attack: bool = False


class EvalCaseResult(BaseModel):
    """Per-case evaluation metrics and trace output."""

    case_id: str
    target_type: str
    category: str
    language: str
    query: str
    actual_output: str
    actual_status: str = ""
    passed: bool
    faithfulness_score: float = Field(..., ge=0.0, le=1.0)
    relevancy_score: float = Field(..., ge=0.0, le=1.0)
    injection_safe: bool = True
    pii_safe: bool = True
    cache_hit: bool = False
    target_source: TargetSource = "standalone"
    error: str | None = None
    latency_ms: float
    prompt_tokens: int
    completion_tokens: int
    estimated_cost_usd: float
    notes: str = ""


class CategoryMetrics(BaseModel):
    category: str
    total_cases: int
    passed_cases: int
    pass_rate: float
    avg_faithfulness: float
    avg_latency_ms: float


class EvalRunRequest(BaseModel):
    """Parameters to launch an evaluation run."""

    use_cache: bool = Field(default=True, description="Enable Redis/in-memory response caching")
    categories: list[Category] | None = Field(
        default=None, description="Optional filter: only run these categories"
    )
    case_ids: list[str] | None = Field(default=None, description="Optional filter: only these IDs")
    target_mode: TargetMode = "auto"
    concurrency: int | None = Field(
        default=None, ge=1, le=64, description="Override EVAL_CONCURRENCY for this run"
    )


class EvalRunSummary(BaseModel):
    """Aggregated benchmark report including the quality-gate verdict."""

    run_id: str
    timestamp: str
    app_version: str = ""
    git_sha: str = ""
    dataset_sha256: str = ""
    target_mode: str
    effective_target: Literal["standalone", "live", "mixed"] = "standalone"
    cache_enabled: bool
    cache_backend: str
    total_cases: int
    passed_cases: int
    error_cases: int = 0
    overall_pass_rate: float
    avg_faithfulness: float
    avg_relevancy: float
    injection_defense_rate: float
    pii_safety_rate: float
    cache_hit_rate: float
    p50_latency_ms: float
    p95_latency_ms: float
    avg_latency_ms: float
    duration_seconds: float = 0.0
    concurrency: int = 1
    total_tokens: int
    total_cost_usd: float
    cost_per_1k_queries_usd: float
    quality_gate_passed: bool
    quality_gate_reasons: list[str] = Field(default_factory=list)
    category_breakdown: list[CategoryMetrics] = Field(default_factory=list)
    results: list[EvalCaseResult] = Field(default_factory=list)


class MetricDelta(BaseModel):
    metric: str
    base: float
    head: float
    delta: float
    higher_is_better: bool
    regressed: bool


class ComparisonReport(BaseModel):
    """Regression diff between a baseline run and a candidate run."""

    base_run_id: str
    head_run_id: str
    tolerance: float
    metrics: list[MetricDelta]
    newly_failing_cases: list[str] = Field(default_factory=list)
    newly_passing_cases: list[str] = Field(default_factory=list)
    regressions: list[str] = Field(default_factory=list)
    regression_detected: bool = False


class CacheTestRequest(BaseModel):
    query: str = Field(..., min_length=2, max_length=2000)
    use_cache: bool = True


class CacheStats(BaseModel):
    backend: str
    ttl_seconds: int
    hits: int
    misses: int
    hit_rate: float
    cached_keys: int
    errors: int = 0


class TraceRecord(BaseModel):
    trace_id: str
    run_id: str
    case_id: str
    category: str
    cache_hit: bool
    latency_ms: float
    faithfulness: float
    pii_safe: bool
    injection_safe: bool
    tokens: int
    cost_usd: float
    langfuse_exported: bool
    created_at: str


class HealthResponse(BaseModel):
    status: str
    version: str
    environment: str = ""
    cache_backend: str
    langfuse_enabled: bool
    dataset_cases_loaded: int
    qa_endpoint_configured: bool
    workflow_endpoint_configured: bool
    auth_enabled: bool = False
    total_eval_runs: int


class ReadinessResponse(BaseModel):
    ready: bool
    checks: dict[str, bool]


JobState = Literal["queued", "running", "completed", "failed"]


class EvalJobStatus(BaseModel):
    """Status and optional result of an asynchronously executed evaluation job."""

    job_id: str
    status: JobState
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    completed_cases: int = 0
    total_cases: int = 0
    run_id: str | None = None
    error: str | None = None
    summary: EvalRunSummary | None = None


class RunnerStatusResponse(BaseModel):
    """Live execution status for the single-process runner."""

    is_running: bool
    active_job_id: str | None = None
    completed_cases: int = 0
    total_cases: int = 0
