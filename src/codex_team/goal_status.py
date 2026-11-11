from __future__ import annotations


_CANCELED_PREFIXES = (
    "canceled:",
    "cancelled:",
)
_CANCEL_REQUEST_MARKERS = (
    "cancel requested",
    "canceled by user",
    "cancelled by user",
    "requested through mcp",
    "requested through dashboard",
)


def goal_was_canceled(last_error: object) -> bool:
    reason = str(last_error or "").strip().lower()
    if not reason:
        return False
    return reason.startswith(_CANCELED_PREFIXES) or any(
        marker in reason for marker in _CANCEL_REQUEST_MARKERS
    )


def effective_goal_status(status: object, last_error: object) -> str:
    normalized = str(status or "").strip().lower()
    if normalized == "failed" and goal_was_canceled(last_error):
        return "canceled"
    return normalized
