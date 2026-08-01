"""Metrics Collector — external observability. Not coupled to SessionManager."""

from dataclasses import dataclass, field
import time


@dataclass
class MetricsCollector:
    connected_accounts: int = 0
    reconnect_count: int = 0
    reconnect_success: int = 0
    reconnect_failure: int = 0
    failed_sessions: int = 0
    flood_wait_count: int = 0
    ws_connections: int = 0
    event_count: int = 0
    _reconnect_times: list[float] = field(default_factory=list)

    def record_reconnect(self, duration_seconds: float, success: bool):
        self.reconnect_count += 1
        self._reconnect_times.append(duration_seconds)
        if len(self._reconnect_times) > 1000:
            self._reconnect_times = self._reconnect_times[-500:]
        if success:
            self.reconnect_success += 1
        else:
            self.reconnect_failure += 1

    def record_flood_wait(self):
        self.flood_wait_count += 1

    def record_event(self):
        self.event_count += 1

    def update_connected(self, count: int):
        self.connected_accounts = count

    def update_ws_connections(self, count: int):
        self.ws_connections = count

    def to_dict(self) -> dict:
        avg_time = 0
        if self._reconnect_times:
            avg_time = sum(self._reconnect_times) / len(self._reconnect_times)
        return {
            "connected_accounts": self.connected_accounts,
            "reconnect_total": self.reconnect_count,
            "reconnect_success": self.reconnect_success,
            "reconnect_failure": self.reconnect_failure,
            "failed_sessions": self.failed_sessions,
            "flood_wait_count": self.flood_wait_count,
            "ws_connections": self.ws_connections,
            "event_count": self.event_count,
            "avg_reconnect_seconds": round(avg_time, 3),
        }
