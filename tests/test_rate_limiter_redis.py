"""Unit tests for the distributed (Redis) rate-limiter path.

The Redis backend uses redis-py's sync INCR + EXPIRE fixed-window counter.
These tests mock redis-py so they run without a live Redis, verifying:
  - the INCR/EXPIRE calling contract,
  - over-limit blocking,
  - graceful fallback to in-memory when Redis is unusable.
"""

import pytest
from unittest.mock import MagicMock, patch

from app.core import rate_limiter


@pytest.fixture(autouse=True)
def _no_redis_cache():
    """Force the Redis availability cache to re-probe each test."""
    rate_limiter._redis_usable = None
    rate_limiter._redis_checked_at = 0.0
    yield
    rate_limiter._redis_usable = None
    rate_limiter._redis_checked_at = 0.0


def _fake_redis_client(counts):
    """A fake redis-py client whose incr() yields the given counts in order."""
    client = MagicMock()
    client.incr.side_effect = counts
    return client


def test_redis_path_increments_and_blocks():
    """INCR returns 1..4; the limiter allows up to 3 then blocks the 4th."""
    fake = _fake_redis_client([1, 2, 3, 4])
    fake.ping.return_value = True

    with patch.object(rate_limiter, "_redis_usable_now", return_value=True), \
         patch("redis.Redis.from_url", return_value=fake):
        results = [
            rate_limiter.check_rate_limit("203.0.113.9", "unit", max_attempts=3, window_seconds=60)
            for _ in range(4)
        ]

    assert results == [True, True, True, False]
    assert fake.incr.call_count == 4
    # EXPIRE is set on the first INCR (count == 1) and not again.
    expire_calls = [call for call in fake.expire.call_args_list]
    assert len(expire_calls) == 1
    assert expire_calls[0][0][0] == "rl:unit:203.0.113.9"


def test_redis_path_does_not_expire_after_first():
    """EXPIRE is only called when INCR returns 1 (fresh window)."""
    fake = _fake_redis_client([2, 3])
    fake.ping.return_value = True

    with patch.object(rate_limiter, "_redis_usable_now", return_value=True), \
         patch("redis.Redis.from_url", return_value=fake):
        rate_limiter.check_rate_limit("203.0.113.10", "unit", max_attempts=5, window_seconds=60)
        rate_limiter.check_rate_limit("203.0.113.10", "unit", max_attempts=5, window_seconds=60)

    fake.expire.assert_not_called()


def test_redis_path_returns_retry_after():
    """get_retry_after_seconds reads Redis TTL when Redis is usable."""
    fake = MagicMock()
    fake.ping.return_value = True
    fake.ttl.return_value = 42

    with patch.object(rate_limiter, "_redis_usable_now", return_value=True), \
         patch("redis.Redis.from_url", return_value=fake):
        assert rate_limiter.get_retry_after_seconds("203.0.113.11", "unit", 60) == 42
        fake.ttl.assert_called_once_with("rl:unit:203.0.113.11")


def test_falls_back_to_memory_when_redis_error():
    """When the Redis call raises, check_rate_limit uses the in-memory path."""
    fake = MagicMock()
    fake.ping.return_value = True
    fake.incr.side_effect = RuntimeError("redis exploded")

    with patch.object(rate_limiter, "_redis_usable_now", return_value=True), \
         patch("redis.Redis.from_url", return_value=fake):
        # in-memory window is empty -> allowed
        assert rate_limiter.check_rate_limit("203.0.113.12", "unit", max_attempts=3, window_seconds=60) is True


def test_memory_path_when_redis_unusable():
    """When Redis is not usable at all, the in-memory path still blocks."""
    with patch.object(rate_limiter, "_redis_usable_now", return_value=False):
        results = [
            rate_limiter.check_rate_limit("203.0.113.13", "unit", max_attempts=2, window_seconds=60)
            for _ in range(3)
        ]
    assert results == [True, True, False]
