from __future__ import annotations

import pytest
from app.services.cache_service import LLMCacheService
from fakeredis import FakeAsyncRedis


def test_key_normalisation_and_isolation() -> None:
    k = LLMCacheService.make_key
    assert k("qa", "  What IS  this? ") == k("qa", "what is this?")
    assert k("qa", "x", "live|http://a") != k("qa", "x", "standalone|c1")
    assert k("qa", "x") != k("workflow", "x")
    assert k("qa", "x").startswith("llmops:qa:")


async def test_memory_roundtrip_stats_and_flush() -> None:
    cache = LLMCacheService(ttl_seconds=60)
    key = cache.make_key("qa", "hello")
    assert await cache.get(key) is None
    await cache.set(key, {"output_text": "مرحبا", "status": "QA_OK"})
    assert await cache.get(key) == {"output_text": "مرحبا", "status": "QA_OK"}
    stats = await cache.stats()
    assert (stats.hits, stats.misses, stats.hit_rate, stats.cached_keys) == (1, 1, 0.5, 1)
    assert await cache.flush() == 1
    assert (await cache.stats()).cached_keys == 0


async def test_memory_ttl_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr("app.services.cache_service.time.time", lambda: now[0])
    cache = LLMCacheService(ttl_seconds=10)
    await cache.set("k", {"v": 1})
    now[0] += 5
    assert await cache.get("k") == {"v": 1}
    now[0] += 6
    assert await cache.get("k") is None
    assert (await cache.stats()).cached_keys == 0


async def test_memory_lru_is_bounded() -> None:
    cache = LLMCacheService(max_memory_entries=2)
    await cache.set("a", {"v": 1})
    await cache.set("b", {"v": 2})
    assert await cache.get("a") == {"v": 1}  # touch "a" so "b" is the LRU entry
    await cache.set("c", {"v": 3})
    assert await cache.get("b") is None
    assert await cache.get("a") == {"v": 1} and await cache.get("c") == {"v": 3}


async def test_redis_backend_roundtrip() -> None:
    cache = LLMCacheService(client=FakeAsyncRedis(decode_responses=True), ttl_seconds=60)
    assert cache.backend == "redis"
    await cache.set("llmops:qa:abc", {"output_text": "x", "status": "QA_OK"})
    assert await cache.get("llmops:qa:abc") == {"output_text": "x", "status": "QA_OK"}
    assert (await cache.stats()).cached_keys == 1
    assert await cache.ping()
    assert await cache.flush() == 1
    assert await cache.get("llmops:qa:abc") is None
    await cache.aclose()


class _BrokenRedis:
    async def get(self, *_: object) -> None:
        raise ConnectionError("down")

    set = get

    def scan_iter(self, *_: object, **__: object) -> object:
        raise ConnectionError("down")

    async def ping(self) -> None:
        raise ConnectionError("down")


async def test_redis_failures_degrade_to_memory() -> None:
    cache = LLMCacheService(client=_BrokenRedis())
    await cache.set("k", {"v": 1})  # Redis SET fails -> value is kept in memory instead
    assert await cache.get("k") == {"v": 1}  # Redis GET fails -> served from memory
    stats = await cache.stats()
    assert stats.errors >= 2
    assert await cache.flush() >= 1
    assert not await cache.ping()


async def test_connect_falls_back_when_redis_unreachable() -> None:
    cache = LLMCacheService(redis_url="redis://127.0.0.1:1/0")
    await cache.connect()
    assert cache.backend == "memory_ttl"
    await cache.set("k", {"v": 1})
    assert await cache.get("k") == {"v": 1}


async def test_connect_is_noop_without_url() -> None:
    cache = LLMCacheService()
    await cache.connect()
    assert cache.backend == "memory_ttl" and await cache.ping()
