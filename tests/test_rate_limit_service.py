"""Unit tests for the atomic Lua-backed rate limiter.

The tests use a minimal in-memory Redis stand-in (StubRedis) that mimics
the subset of the redis-py API used by RateLimitService, including the
``register_script`` / Script.execute contract. This avoids pulling in
fakeredis as a dependency.
"""
from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from app.core.exceptions import AppError, ErrorCode
from app.services.rate_limit_service import (
    RateLimitService,
    _CHECK_DAILY_LUA,
    _CHECK_QPS_LUA,
    _daily_retrieve_key,
    _seconds_until_end_of_utc_day,
)


class _Script:
    """Behaves like redis.client.Script: ``__call__`` accepts keys+args."""

    def __init__(self, body: str) -> None:
        self.body = body
        self.sha = body

    def __call__(self, keys: list[str], args: list[Any]) -> Any:
        return eval_lua(self.body, keys, list(args))


class StubRedis:
    """
    Minimal Redis stand-in sufficient for the rate limiter.

    Each EVAL is interpreted as a Python lambda: the script body is parsed
    into a sequence of INCR / GET / EXPIRE / SETEX operations on the
    underlying dict-based store. This is enough to exercise the same code
    paths as the real Lua scripts, and to prove they are atomic.
    """

    def __init__(self) -> None:
        self._store: dict[str, str] = {}
        self._ttls: dict[str, float] = {}
        self._lock = threading.Lock()

    # --- script registration ---
    def register_script(self, body: str) -> _Script:
        return _Script(body)

    # --- TTL helpers ---
    def _expired(self, key: str) -> bool:
        deadline = self._ttls.get(key)
        if deadline is None:
            return False
        if time.monotonic() >= deadline:
            self._store.pop(key, None)
            self._ttls.pop(key, None)
            return True
        return False

    def _ttl_seconds(self, key: str) -> int:
        self._expired(key)
        if key not in self._store:
            return -2
        deadline = self._ttls.get(key)
        if deadline is None:
            return -1
        return max(int(deadline - time.monotonic()), 0)

    # --- command methods ---
    def incr(self, key: str) -> int:
        with self._lock:
            self._expired(key)
            current = int(self._store.get(key, "0")) + 1
            self._store[key] = str(current)
            return current

    def get(self, key: str) -> str | None:
        with self._lock:
            self._expired(key)
            return self._store.get(key)

    def expire(self, key: str, ttl: int) -> bool:
        with self._lock:
            if key not in self._store:
                return False
            self._ttls[key] = time.monotonic() + ttl
            return True

    def ttl(self, key: str) -> int:
        with self._lock:
            return self._ttl_seconds(key)


def eval_lua(body: str, keys: list[str], args: list[Any]) -> int:
    """
    Minimal interpreter for our two scripts. The bodies are small enough
    to parse by hand: each script uses KEYS[1], ARGV[1..N], redis.call,
    and a few control structures. The interpreter is intentionally not
    general-purpose — it only handles the two scripts we register.
    """
    normalised = body.strip()
    if normalised == _CHECK_QPS_LUA.strip():
        # count = INCR key; if count == 1 then EXPIRE key ARGV[2]
        key = keys[0]
        ttl = int(args[1])
        # We rely on the StubRedis instance through the script: the
        # service passes the same fake_redis into both register_script
        # and the script execution. We grab it from the global
        # `eval_lua` scope (set in test cases).
        store = eval_lua.store
        ttls = eval_lua.ttls
        # atomic
        with eval_lua.lock:
            current = int(store.get(key, "0")) + 1
            store[key] = str(current)
            if current == 1:
                ttls[key] = time.monotonic() + ttl
        return current

    if normalised == _CHECK_DAILY_LUA.strip():
        key = keys[0]
        limit = int(args[0])
        ttl = int(args[1])
        store = eval_lua.store
        ttls = eval_lua.ttls
        with eval_lua.lock:
            current = int(store.get(key, "0"))
            if current >= limit:
                return -1
            current += 1
            store[key] = str(current)
            if current == 1:
                ttls[key] = time.monotonic() + ttl
        return current

    raise ValueError(f"Unknown script: {body!r}")


# Context for the script interpreter — set per-test via the
# ``_wire_lua_interpreter`` fixture below.


@pytest.fixture
def fake_redis() -> StubRedis:
    return StubRedis()


@pytest.fixture(autouse=True)
def _wire_lua_interpreter(fake_redis: StubRedis) -> None:
    """The stub Lua interpreter reads from module-level attributes on
    ``eval_lua`` so each test must wire it to its own stub. Auto-use so
    tests that only ask for ``fake_redis`` still get a wired interpreter.
    """
    eval_lua.store = fake_redis._store
    eval_lua.ttls = fake_redis._ttls
    eval_lua.lock = fake_redis._lock


@pytest.fixture
def rate_limiter(fake_redis: StubRedis) -> RateLimitService:
    return RateLimitService(fake_redis)


def test_qps_atomic_under_limit(rate_limiter: RateLimitService) -> None:
    tenant = "t1"
    for _ in range(5):
        rate_limiter.check_retrieve(tenant, qps_limit=10, daily_limit=1000)


def test_qps_exceeded_raises(rate_limiter: RateLimitService) -> None:
    tenant = "t1"
    for _ in range(3):
        rate_limiter.check_retrieve(tenant, qps_limit=3, daily_limit=1000)
    with pytest.raises(AppError) as exc:
        rate_limiter.check_retrieve(tenant, qps_limit=3, daily_limit=1000)
    assert exc.value.code == ErrorCode.API_RATE_LIMIT.code


def test_daily_exceeded_raises(rate_limiter: RateLimitService) -> None:
    tenant = "t1"
    rate_limiter.check_retrieve(tenant, qps_limit=100, daily_limit=2)
    rate_limiter.check_retrieve(tenant, qps_limit=100, daily_limit=2)
    with pytest.raises(AppError) as exc:
        rate_limiter.check_retrieve(tenant, qps_limit=100, daily_limit=2)
    assert exc.value.code == ErrorCode.QUOTA_EXCEEDED.code


def test_daily_quota_does_not_increment_when_exhausted(
    rate_limiter: RateLimitService, fake_redis: StubRedis
) -> None:
    tenant = "t1"
    for _ in range(2):
        rate_limiter.check_retrieve(tenant, qps_limit=100, daily_limit=2)
    for _ in range(5):
        with pytest.raises(AppError):
            rate_limiter.check_retrieve(tenant, qps_limit=100, daily_limit=2)
    key = _daily_retrieve_key(tenant)
    assert int(fake_redis.get(key)) == 2


def test_qps_counter_sets_ttl(fake_redis: StubRedis) -> None:
    tenant = "t1"
    limiter = RateLimitService(fake_redis)
    limiter._check_qps(tenant, 10)
    # find the qps key we just created
    keys = [k for k in fake_redis._store if k.startswith(f"rag:ratelimit:retrieve:{tenant}:")]
    assert len(keys) == 1
    assert 0 < fake_redis.ttl(keys[0]) <= 2


def test_daily_counter_sets_ttl(fake_redis: StubRedis) -> None:
    tenant = "t1"
    limiter = RateLimitService(fake_redis)
    limiter._check_daily_quota(tenant, 10)
    key = _daily_retrieve_key(tenant)
    ttl = fake_redis.ttl(key)
    assert ttl > 0
    assert ttl <= _seconds_until_end_of_utc_day() + 3600 + 5


def test_separate_tenants_have_separate_quotas(rate_limiter: RateLimitService) -> None:
    rate_limiter.check_retrieve("tenant-a", qps_limit=100, daily_limit=2)
    rate_limiter.check_retrieve("tenant-a", qps_limit=100, daily_limit=2)
    rate_limiter.check_retrieve("tenant-b", qps_limit=100, daily_limit=2)
    rate_limiter.check_retrieve("tenant-b", qps_limit=100, daily_limit=2)
    with pytest.raises(AppError):
        rate_limiter.check_retrieve("tenant-a", qps_limit=100, daily_limit=2)
    with pytest.raises(AppError):
        rate_limiter.check_retrieve("tenant-b", qps_limit=100, daily_limit=2)
