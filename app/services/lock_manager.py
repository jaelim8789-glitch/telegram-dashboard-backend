"""Per-account Lock Manager for concurrent access control."""

import asyncio


class LockManager:
    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = {}

    def get_lock(self, account_id: str) -> asyncio.Lock:
        if account_id not in self._locks:
            self._locks[account_id] = asyncio.Lock()
        return self._locks[account_id]

    def remove_lock(self, account_id: str):
        self._locks.pop(account_id, None)
