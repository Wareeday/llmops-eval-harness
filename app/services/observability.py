"""Observability store: SQLite run history + optional Langfuse export.

* SQLite (WAL mode, indexed) is the always-on, zero-dependency source of truth. It works
  offline, in CI and in air-gapped environments.
* Langfuse is an optional exporter. Both SDK generations are supported: the OpenTelemetry
  based SDK (>= 3, ``start_observation`` / ``create_score``) and the legacy 2.x SDK
  (``trace().score()``). The correct API is auto-detected at runtime.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from typing import Any

from app.core.config import Settings
from app.models.schemas import EvalCaseResult, EvalRunSummary, TraceRecord

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2


class LangfuseExporter:
    """Thin, failure-tolerant wrapper around the Langfuse SDK (v2 and v3+)."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self.api = "v3" if hasattr(client, "start_observation") else "v2"

    @classmethod
    def from_settings(cls, settings: Settings) -> LangfuseExporter | None:
        if not settings.langfuse_configured:
            return None
        try:
            from langfuse import Langfuse

            client = Langfuse(
                public_key=settings.langfuse_public_key.get_secret_value(),
                secret_key=settings.langfuse_secret_key.get_secret_value(),
                host=settings.langfuse_host,
            )
        except Exception as exc:
            logger.warning("Langfuse unavailable (%s); traces stay in SQLite only.", exc)
            return None
        exporter = cls(client)
        logger.info(
            "Langfuse exporter enabled (sdk=%s, host=%s).", exporter.api, settings.langfuse_host
        )
        return exporter

    def export_case(self, run_id: str, result: EvalCaseResult) -> str | None:
        """Send one case as a trace with scores. Returns the Langfuse trace id or ``None``."""
        name = f"eval:{result.category}:{result.case_id}"
        metadata = {
            "run_id": run_id,
            "category": result.category,
            "language": result.language,
            "latency_ms": result.latency_ms,
            "tokens": result.prompt_tokens + result.completion_tokens,
            "cost_usd": result.estimated_cost_usd,
            "target_source": result.target_source,
            "passed": result.passed,
        }
        io = {
            "input": {"query": result.query, "cache_hit": result.cache_hit},
            "output": {"response": result.actual_output[:1000], "notes": result.notes},
        }
        scores: list[tuple[str, float, str]] = [
            ("faithfulness", result.faithfulness_score, "NUMERIC"),
            ("relevancy", result.relevancy_score, "NUMERIC"),
            ("pii_safe", 1.0 if result.pii_safe else 0.0, "BOOLEAN"),
            ("injection_safe", 1.0 if result.injection_safe else 0.0, "BOOLEAN"),
            ("passed", 1.0 if result.passed else 0.0, "BOOLEAN"),
        ]
        try:
            if self.api == "v3":
                trace_id = self._client.create_trace_id(seed=f"{run_id}:{result.case_id}")
                span = self._client.start_observation(
                    name=name, as_type="span", trace_context={"trace_id": trace_id},
                    metadata=metadata, **io,
                )  # fmt: skip
                span.end()
                for score_name, value, data_type in scores:
                    self._client.create_score(
                        name=score_name, value=value, trace_id=trace_id, data_type=data_type
                    )
                return str(trace_id)
            trace = self._client.trace(name=name, metadata=metadata, **io)
            for score_name, value, _ in scores:
                trace.score(name=score_name, value=value)
            return str(getattr(trace, "id", "") or name)
        except Exception as exc:
            logger.warning("Langfuse export skipped for %s: %s", result.case_id, exc)
            return None

    def flush(self) -> None:
        try:
            self._client.flush()
        except Exception as exc:  # pragma: no cover - network dependent
            logger.warning("Langfuse flush failed: %s", exc)

    def shutdown(self) -> None:
        try:
            self._client.shutdown() if hasattr(self._client, "shutdown") else self._client.flush()
        except Exception as exc:  # pragma: no cover - network dependent
            logger.debug("Langfuse shutdown failed: %s", exc)


class ObservabilityService:
    """Records evaluation runs and per-case traces (SQLite + optional Langfuse)."""

    def __init__(self, settings: Settings, langfuse_client: Any | None = None) -> None:
        self.sqlite_path = settings.sqlite_path
        self.max_stored_runs = settings.max_stored_runs
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self._exporter: LangfuseExporter | None = (
            LangfuseExporter(langfuse_client)
            if langfuse_client is not None
            else LangfuseExporter.from_settings(settings)
        )
        self._init_db()

    @property
    def langfuse_enabled(self) -> bool:
        return self._exporter is not None

    # ------------------------------------------------------------------ sqlite plumbing
    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(str(self.sqlite_path), timeout=10)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 10000")
            with conn:  # commit / rollback
                yield conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS eval_runs (
                    run_id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    overall_pass_rate REAL NOT NULL,
                    avg_faithfulness REAL NOT NULL,
                    injection_defense_rate REAL NOT NULL,
                    cache_hit_rate REAL NOT NULL,
                    p95_latency_ms REAL NOT NULL,
                    quality_gate_passed INTEGER NOT NULL,
                    summary_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS traces (
                    trace_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    cache_hit INTEGER NOT NULL,
                    latency_ms REAL NOT NULL,
                    faithfulness REAL NOT NULL,
                    pii_safe INTEGER NOT NULL,
                    injection_safe INTEGER NOT NULL,
                    tokens INTEGER NOT NULL,
                    cost_usd REAL NOT NULL,
                    langfuse_exported INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_runs_ts ON eval_runs(timestamp DESC);
                CREATE INDEX IF NOT EXISTS idx_traces_run ON traces(run_id);
                CREATE INDEX IF NOT EXISTS idx_traces_created ON traces(created_at DESC);
                """
            )
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def ping(self) -> bool:
        try:
            with self._connect() as conn:
                conn.execute("SELECT 1").fetchone()
        except sqlite3.Error:
            return False
        return True

    # ------------------------------------------------------------------ writes
    def record_traces(self, run_id: str, results: list[EvalCaseResult]) -> list[TraceRecord]:
        """Persist every case of a run in one transaction (and export to Langfuse)."""
        now = datetime.now(timezone.utc).isoformat()
        records: list[TraceRecord] = []
        for res in results:
            lf_id = self._exporter.export_case(run_id, res) if self._exporter else None
            records.append(
                TraceRecord(
                    trace_id=f"tr_{uuid.uuid4().hex[:12]}",
                    run_id=run_id,
                    case_id=res.case_id,
                    category=res.category,
                    cache_hit=res.cache_hit,
                    latency_ms=res.latency_ms,
                    faithfulness=res.faithfulness_score,
                    pii_safe=res.pii_safe,
                    injection_safe=res.injection_safe,
                    tokens=res.prompt_tokens + res.completion_tokens,
                    cost_usd=res.estimated_cost_usd,
                    langfuse_exported=lf_id is not None,
                    created_at=now,
                )
            )
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO traces VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        r.trace_id, r.run_id, r.case_id, r.category, int(r.cache_hit),
                        r.latency_ms, r.faithfulness, int(r.pii_safe), int(r.injection_safe),
                        r.tokens, r.cost_usd, int(r.langfuse_exported), r.created_at,
                    )
                    for r in records
                ],
            )  # fmt: skip
        return records

    def save_run(self, summary: EvalRunSummary) -> None:
        if self._exporter:
            self._exporter.flush()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO eval_runs VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    summary.run_id, summary.timestamp, summary.overall_pass_rate,
                    summary.avg_faithfulness, summary.injection_defense_rate,
                    summary.cache_hit_rate, summary.p95_latency_ms,
                    int(summary.quality_gate_passed),
                    json.dumps(summary.model_dump(), ensure_ascii=False),
                ),
            )  # fmt: skip
            # Retention: keep only the newest N runs and their traces.
            conn.execute(
                "DELETE FROM eval_runs WHERE run_id NOT IN "
                "(SELECT run_id FROM eval_runs ORDER BY timestamp DESC LIMIT ?)",
                (self.max_stored_runs,),
            )
            conn.execute("DELETE FROM traces WHERE run_id NOT IN (SELECT run_id FROM eval_runs)")

    # ------------------------------------------------------------------ reads
    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT run_id, timestamp, overall_pass_rate, avg_faithfulness,
                       injection_defense_rate, cache_hit_rate, p95_latency_ms, quality_gate_passed
                FROM eval_runs ORDER BY timestamp DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def count_runs(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM eval_runs").fetchone()[0])

    def get_run(self, run_id: str) -> EvalRunSummary | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT summary_json FROM eval_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return EvalRunSummary.model_validate(json.loads(row["summary_json"])) if row else None

    def list_traces(self, limit: int = 100, run_id: str | None = None) -> list[TraceRecord]:
        query = "SELECT * FROM traces"
        params: list[Any] = []
        if run_id:
            query += " WHERE run_id = ?"
            params.append(run_id)
        query += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            TraceRecord(
                trace_id=r["trace_id"], run_id=r["run_id"], case_id=r["case_id"],
                category=r["category"], cache_hit=bool(r["cache_hit"]),
                latency_ms=float(r["latency_ms"]), faithfulness=float(r["faithfulness"]),
                pii_safe=bool(r["pii_safe"]), injection_safe=bool(r["injection_safe"]),
                tokens=int(r["tokens"]), cost_usd=float(r["cost_usd"]),
                langfuse_exported=bool(r["langfuse_exported"]), created_at=r["created_at"],
            )
            for r in rows
        ]  # fmt: skip

    def shutdown(self) -> None:
        if self._exporter:
            self._exporter.shutdown()
