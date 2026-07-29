"""Regression tests for the rate limiter's background cleanup scheduler."""

from app.core import rate_limiter


def test_cleanup_timer_is_daemon(monkeypatch):
    """Import-time cleanup must not block a CLI process from exiting."""

    created = []

    class FakeTimer:
        def __init__(self, interval, callback):
            self.interval = interval
            self.callback = callback
            self.daemon = False
            self.started = False
            created.append(self)

        def start(self):
            self.started = True

    monkeypatch.setattr(rate_limiter.threading, "Timer", FakeTimer)

    rate_limiter._schedule_cleanup()

    assert len(created) == 1
    assert created[0].interval == rate_limiter._CLEANUP_INTERVAL_SECONDS
    assert created[0].callback is rate_limiter._periodic_cleanup
    assert created[0].daemon is True
    assert created[0].started is True
