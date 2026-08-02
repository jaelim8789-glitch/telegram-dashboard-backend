"""Health Service — derives and monitors account health."""

import logging
from datetime import datetime, timezone
from app.services.state_machine import SessionState

logger = logging.getLogger(__name__)


class HealthService:
    """Derives health status from account state and message logs."""

    def get_health_summary(self, accounts: list, health_items: list) -> dict:
        total = len(accounts)
        healthy = sum(1 for h in health_items if h.get("status") == "healthy")
        unhealthy = sum(1 for h in health_items if h.get("status") not in ("healthy", "unknown", "not_configured"))
        banned = sum(1 for h in health_items if h.get("status") == "banned")
        return {
            "total": total,
            "healthy": healthy,
            "unhealthy": unhealthy,
            "banned": banned,
        }

    def derive_state_health(self, state: SessionState) -> dict:
        mapping = {
            SessionState.NOT_CONFIGURED: {"status": "not_configured", "has_session": False},
            SessionState.CONNECTING: {"status": "connecting", "has_session": True},
            SessionState.CONNECTED: {"status": "healthy", "has_session": True},
            SessionState.DISCONNECTED: {"status": "disconnected", "has_session": True},
            SessionState.RECONNECTING: {"status": "reconnecting", "has_session": True},
            SessionState.EXPIRED: {"status": "unauthorized", "has_session": True},
            SessionState.UNAUTHORIZED: {"status": "unauthorized", "has_session": True},
            SessionState.BANNED: {"status": "banned", "has_session": True},
            SessionState.FLOOD_WAIT: {"status": "rate_limited", "has_session": True},
            SessionState.RATE_LIMITED: {"status": "rate_limited", "has_session": True},
            SessionState.SUSPENDED: {"status": "restricted", "has_session": True},
            SessionState.UPDATING: {"status": "updating", "has_session": True},
        }
        return mapping.get(state, {"status": "unknown", "has_session": False})

    def derive_health(self, account, latest_message=None) -> dict:
        if account.status == "banned":
            return {"status": "banned", "has_session": bool(account.session_data)}
        if account.status == "suspended":
            return {"status": "restricted", "has_session": bool(account.session_data)}
        has_session = bool(account.session_data)
        if not has_session:
            return {"status": "not_configured", "has_session": False}
        if latest_message:
            if not latest_message.success:
                if latest_message.status == "session_expired":
                    return {"status": "unauthorized", "has_session": True}
                if latest_message.status == "banned":
                    return {"status": "banned", "has_session": True}
                if latest_message.status == "flood_wait":
                    return {"status": "rate_limited", "has_session": True}
                return {"status": "error", "has_session": True}
            return {"status": "healthy", "has_session": True}
        return {"status": "unknown", "has_session": True}
