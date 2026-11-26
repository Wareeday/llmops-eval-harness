"""Async response cache: Redis when available, bounded in-memory TTL/LRU otherwise.

Design notes
------------
* Uses ``redis.asyncio`` so cache I/O never blocks the event loop.
* Redis problems never break an evaluation run: the cache degrades to the in-memory
  store and counts the incident in ``stats().errors``.
* The in-memory store is a bounded LRU, so a long-running server cannot grow forever.
* Keys include a *fingerprint* of the target (mode + endpoint) so a response produced by
  the built-in simulator can never be served for a live agent run (and vice versa).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import OrderedDict
from typing import Any

from app.models.schemas import CacheStats

logger = logging.getLogger(__name__)

KEY_PREFIX = "llmops"


class LLMCacheService:
    """Caches normalised agent responses to cut latency and token spend."""

    def __init__(
        self,
        redis_url: str = "",
        ttl_seconds: int = 3600,
        max_memory_entries: int = 10_000,
        client: Any | None = None,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_memory_entries = max_memory_entries
        self.backend = "memory_ttl"
        self.hits = 0
        self.misses = 0
        self.errors = 0
        self._redis_url = redis_url.strip()
        self._redis: Any | None = client
        self._memory: OrderedDict[str, tuple[float, str]] = OrderedDict()
        if client is not None:
            self.backend = "redis"

    # ------------------------------------------------------------------ lifecycle
    async def connect(self) -> None:
        """Connect to Redis (if configured). Falls back to memory on any failure."""
        if self._redis is not None or not self._redis_url:
            return
        try:
            import redis.asyncio as aioredis

            client = aioredis.from_url(
                self._redis_url,
                decode_responses=True,
                socket_timeout=2,
                socket_connect_timeout=2,
            )
            await client.ping()
        except Exception as exc:
            logger.warning("Redis unavailable (%s); using in-memory cache.", type(exc).__name__)
            self._redis = None
            self.backend = "memory_ttl"
            return
        self._redis = client
        self.backend = "redis"
        logger.info("Connected to Redis cache backend.")

    async def aclose(self) -> None:
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception as exc:  # pragma: no cover - best effort on shutdown
                logger.debug("Redis close failed: %s", exc)

    async def ping(self) -> bool:
        if self._redis is None:
            return True
        try:
            await self._redis.ping()
        except Exception:
            return False
        return True

    # ------------------------------------------------------------------ keys
    @staticmethod
    def make_key(namespace: str, query: str, fingerprint: str = "") -> str:
        normalized = " ".join(query.strip().lower().split())
        digest = hashlib.sha256(f"{fingerprint}\x00{normalized}".encode()).hexdigest()[:32]
        return f"{KEY_PREFIX}:{namespace}:{digest}"

    # ------------------------------------------------------------------ operations
    async def get(self, key: str) -> dict[str, Any] | None:
        if self._redis is not None:
            try:
                raw = await self._redis.get(key)
            except Exception as exc:
                self._record_error("GET", exc)
            else:
                if raw is not None:
                    self.hits += 1
                    return json.loads(raw)  # type: ignore[no-any-return]
                self.misses += 1
                return None

        entry = self._memory.get(key)
        if entry is not None:
            expires_at, raw = entry
            if time.time() < expires_at:
                self._memory.move_to_end(key)
                self.hits += 1
                return json.loads(raw)  # type: ignore[no-any-return]
            del self._memory[key]
        self.misses += 1
        return None

    async def set(self, key: str, value: dict[str, Any]) -> None:
        raw = json.dumps(value, ensure_ascii=False)
        if self._redis is not None:
            try:
                await self._redis.set(key, raw, ex=self.ttl_seconds)
            except Exception as exc:
                self._record_error("SET", exc)
            else:
                return
        self._memory[key] = (time.time() + self.ttl_seconds, raw)
        self._memory.move_to_end(key)
        while len(self._memory) > self.max_memory_entries:
            self._memory.popitem(last=False)

    async def flush(self) -> int:
        """Delete every cache entry created by this application. Returns the count."""
        cleared = 0
        if self._redis is not None:
            try:
                batch: list[str] = []
                async for k in self._redis.scan_iter(match=f"{KEY_PREFIX}:*", count=500):
                    batch.append(k)
                    if len(batch) >= 500:
                        cleared += int(await self._redis.delete(*batch))
                        batch.clear()
                if batch:
                    cleared += int(await self._redis.delete(*batch))
            except Exception as exc:
                self._record_error("FLUSH", exc)
        cleared += len(self._memory)
        self._memory.clear()
        self.hits = self.misses = 0
        return cleared

    async def stats(self) -> CacheStats:
        total = self.hits + self.misses
        key_count = 0
        counted = False
        if self._redis is not None:
            try:
                async for _ in self._redis.scan_iter(match=f"{KEY_PREFIX}:*", count=500):
                    key_count += 1
                counted = True
            except Exception as exc:
                self._record_error("STATS", exc)
        if not counted:
            now = time.time()
            key_count = sum(1 for exp, _ in self._memory.values() if exp > now)
        return CacheStats(
            backend=self.backend,
            ttl_seconds=self.ttl_seconds,
            hits=self.hits,
            misses=self.misses,
            hit_rate=round(self.hits / total, 4) if total else 0.0,
            cached_keys=key_count,
            errors=self.errors,
        )

    def _record_error(self, op: str, exc: Exception) -> None:
        self.errors += 1
        logger.warning("Redis %s failed (%s); falling back to memory.", op, type(exc).__name__)
