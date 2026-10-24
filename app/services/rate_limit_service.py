from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import lru_cache
from time import time

import redis

from app.core.config import settings
from app.core.exceptions import AppError, ErrorCode

# ---------------------------------------------------------------------------
# Lua scripts
#
# Each script runs atomically inside Redis so that the check + increment
# cannot be interleaved with another client's request. Returning the
# post-increment count lets the caller reason about the limit in one round
# trip. The QPS script also self-heals the key TTL on the first hit of a
# second so we never have a stale key lying around.
# ---------------------------------------------------------------------------

# KEYS[1] = qps key (per-second, per-tenant)
# ARGV[1] = qps limit
# ARGV[2] = TTL in seconds (>= 2 so the key survives the current second)
# Returns: post-increment count. If > limit, the caller must raise.
_CHECK_QPS_LUA = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
  redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
end
return count
"""

# KEYS[1] = daily key (per-day, per-tenant)
# ARGV[1] = daily limit
# ARGV[2] = TTL in seconds (covers the rest of the UTC day plus a grace
#           window so the key still exists at the boundary)
# Returns: post-increment count, or -1 when the limit is already exhausted
#          (no increment in that case so the quota never goes negative).
_CHECK_DAILY_LUA = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if current >= tonumber(ARGV[1]) then
  return -1
end
local count = redis.call('INCR', KEYS[1])
if count == 1 then
  redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
end
return count
"""


class RateLimitService:
    def __init__(self, redis_client: redis.Redis) -> None:
        self.redis = redis_client
        # Register Lua scripts once so we can use Script.execute() with
        # EVALSHA caching — far cheaper than re-sending the script body on
        # every request.
        self._qps_script = self.redis.register_script(_CHECK_QPS_LUA)
        self._daily_script = self.redis.register_script(_CHECK_DAILY_LUA)

    def check_retrieve(self, tenant_id: str, *, qps_limit: int, daily_limit: int) -> None:
        """Atomically verify the per-second QPS and the daily quota in
        two Redis round-trips. The QPS check increments the counter
        optimistically; the daily check increments only when under limit.

        The atomic increment in the daily check IS the success counter:
        callers no longer need a separate ``record_retrieve_success``
        step. Doing both would double-count every quota and let users
        hit their daily limit in half the expected time.
        """
        self._check_qps(tenant_id, qps_limit)
        self._check_daily_quota(tenant_id, daily_limit)

    def get_daily_retrieve_count(self, tenant_id: str) -> int:
        return int(self.redis.get(_daily_retrieve_key(tenant_id)) or 0)

    def _check_qps(self, tenant_id: str, qps_limit: int) -> None:
        second = int(time())
        key = f"rag:ratelimit:retrieve:{tenant_id}:{second}"
        # The script sets a 2-second TTL on first hit so the key is
        # guaranteed to live across the second boundary, then expires
        # naturally to keep Redis tidy.
        count = int(self._qps_script(keys=[key], args=[qps_limit, 2]))
        if count > qps_limit:
            raise AppError(
                ErrorCode.API_RATE_LIMIT,
                msg="检索请求过于频繁，请稍后再试",
                context={"tenant_id": tenant_id, "qps_limit": qps_limit},
            )

    def _check_daily_quota(self, tenant_id: str, daily_limit: int) -> None:
        daily_key = _daily_retrieve_key(tenant_id)
        ttl = _seconds_until_end_of_utc_day() + 3600
        result = int(self._daily_script(keys=[daily_key], args=[daily_limit, ttl]))
        if result == -1:
            raise AppError(
                ErrorCode.QUOTA_EXCEEDED,
                msg="日检索量已达上限",
                context={"tenant_id": tenant_id, "daily_limit": daily_limit},
            )


def _daily_retrieve_key(tenant_id: str) -> str:
    day = datetime.now(UTC).strftime("%Y%m%d")
    return f"rag:quota:retrieve:daily:{tenant_id}:{day}"


def _seconds_until_end_of_utc_day() -> int:
    now = datetime.now(UTC)
    end = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(int((end - now).total_seconds()), 1)


@lru_cache
def get_rate_limit_service() -> RateLimitService:
    client = redis.from_url(settings.celery_broker_url, decode_responses=True)
    return RateLimitService(client)
