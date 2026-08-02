"""Session State Machine.

States: NOT_CONFIGURED, CONNECTING, CONNECTED, DISCONNECTED, RECONNECTING,
        EXPIRED, UNAUTHORIZED, BANNED, FLOOD_WAIT, RATE_LIMITED, SUSPENDED, UPDATING

Recovery Reasons: network, session_expired, server_restart, flood_wait,
                  banned, manual, register, re_auth, resume
"""

from enum import Enum
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)


class SessionState(str, Enum):
    NOT_CONFIGURED = "not_configured"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    RECONNECTING = "reconnecting"
    EXPIRED = "expired"
    UNAUTHORIZED = "unauthorized"
    BANNED = "banned"
    FLOOD_WAIT = "flood_wait"
    RATE_LIMITED = "rate_limited"
    SUSPENDED = "suspended"
    UPDATING = "updating"


class RecoveryReason(str, Enum):
    NETWORK = "network"
    SESSION_EXPIRED = "session_expired"
    SERVER_RESTART = "server_restart"
    FLOOD_WAIT = "flood_wait"
    BANNED = "banned"
    MANUAL = "manual"
    REGISTER = "register"
    RE_AUTH = "re_auth"
    RESUME = "resume"


@dataclass
class StateTransition:
    target: SessionState
    reason: RecoveryReason | None = None


# {current_state: {event: StateTransition}}
TRANSITIONS: dict[SessionState, dict[str, StateTransition]] = {
    SessionState.NOT_CONFIGURED: {
        "register": StateTransition(SessionState.CONNECTING, RecoveryReason.REGISTER),
    },
    SessionState.CONNECTING: {
        "connected": StateTransition(SessionState.CONNECTED),
        "failed": StateTransition(SessionState.DISCONNECTED),
        "expired": StateTransition(SessionState.EXPIRED, RecoveryReason.SESSION_EXPIRED),
        "banned": StateTransition(SessionState.BANNED, RecoveryReason.BANNED),
        "disconnect": StateTransition(SessionState.DISCONNECTED, RecoveryReason.MANUAL),
    },
    SessionState.CONNECTED: {
        "disconnect": StateTransition(SessionState.DISCONNECTED, RecoveryReason.NETWORK),
        "expired": StateTransition(SessionState.EXPIRED, RecoveryReason.SESSION_EXPIRED),
        "banned": StateTransition(SessionState.BANNED, RecoveryReason.BANNED),
        "flood_wait": StateTransition(SessionState.FLOOD_WAIT, RecoveryReason.FLOOD_WAIT),
        "reconnect": StateTransition(SessionState.RECONNECTING, RecoveryReason.NETWORK),
    },
    SessionState.DISCONNECTED: {
        "reconnect": StateTransition(SessionState.RECONNECTING, RecoveryReason.NETWORK),
        "expired": StateTransition(SessionState.EXPIRED, RecoveryReason.SESSION_EXPIRED),
        "register": StateTransition(SessionState.CONNECTING, RecoveryReason.REGISTER),
    },
    SessionState.RECONNECTING: {
        "connected": StateTransition(SessionState.CONNECTED),
        "failed": StateTransition(SessionState.DISCONNECTED),
        "expired": StateTransition(SessionState.EXPIRED, RecoveryReason.SESSION_EXPIRED),
        "banned": StateTransition(SessionState.BANNED, RecoveryReason.BANNED),
        "backoff_retry": StateTransition(SessionState.RECONNECTING),
    },
    SessionState.EXPIRED: {
        "re_auth": StateTransition(SessionState.CONNECTING, RecoveryReason.RE_AUTH),
        "register": StateTransition(SessionState.CONNECTING, RecoveryReason.REGISTER),
        "delete": StateTransition(SessionState.NOT_CONFIGURED),
    },
    SessionState.UNAUTHORIZED: {
        "re_auth": StateTransition(SessionState.CONNECTING, RecoveryReason.RE_AUTH),
        "register": StateTransition(SessionState.CONNECTING, RecoveryReason.REGISTER),
    },
    SessionState.BANNED: {},
    SessionState.FLOOD_WAIT: {
        "wait_done": StateTransition(SessionState.CONNECTED),
        "expired": StateTransition(SessionState.EXPIRED, RecoveryReason.SESSION_EXPIRED),
    },
    SessionState.RATE_LIMITED: {
        "wait_done": StateTransition(SessionState.CONNECTED),
    },
    SessionState.SUSPENDED: {
        "resume": StateTransition(SessionState.CONNECTING, RecoveryReason.RESUME),
        "delete": StateTransition(SessionState.NOT_CONFIGURED),
    },
    SessionState.UPDATING: {
        "updated": StateTransition(SessionState.CONNECTED),
        "failed": StateTransition(SessionState.DISCONNECTED),
    },
}


@dataclass
class TransitionResult:
    previous: SessionState
    current: SessionState
    event: str
    reason: RecoveryReason | None
    valid: bool


def transition(current: SessionState, event: str, reason: RecoveryReason | None = None) -> TransitionResult:
    """Execute state transition. Returns result with validity flag."""
    transitions = TRANSITIONS.get(current, {})
    target = transitions.get(event)

    if target is None:
        logger.warning("invalid_transition", current=current.value, event=event)
        return TransitionResult(
            previous=current,
            current=current,
            event=event,
            reason=reason,
            valid=False,
        )

    final_reason = reason or target.reason
    return TransitionResult(
        previous=current,
        current=target.target,
        event=event,
        reason=final_reason,
        valid=True,
    )
