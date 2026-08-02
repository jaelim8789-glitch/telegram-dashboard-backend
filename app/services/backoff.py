"""Exponential Backoff Strategy with jitter."""

import random


class BackoffStrategy:
    BASE_DELAY = 1.0
    MAX_DELAY = 60.0
    MULTIPLIER = 2.0
    MAX_ATTEMPTS = 8

    def __init__(self):
        self._attempt: dict[str, int] = {}

    def next_delay(self, account_id: str) -> float:
        attempt = self._attempt.get(account_id, 0)
        delay = min(self.BASE_DELAY * (self.MULTIPLIER ** attempt), self.MAX_DELAY)
        jitter = random.uniform(0.5, 1.0)
        self._attempt[account_id] = attempt + 1
        return delay * jitter

    def reset(self, account_id: str):
        self._attempt.pop(account_id, None)

    def current_attempt(self, account_id: str) -> int:
        return self._attempt.get(account_id, 0)
