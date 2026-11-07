from __future__ import annotations

import os


DEFAULT_GLOBAL_JOB_CAPACITY = 16
DEFAULT_GOAL_WORKER_CAPACITY = 3
MAX_GLOBAL_JOB_CAPACITY = 32
MAX_GOAL_WORKER_CAPACITY = 8
DEFAULT_WORKER_TIMEOUT_SECONDS = 2 * 60 * 60
DEFAULT_LEAD_TIMEOUT_SECONDS = 45 * 60
DEFAULT_HEARTBEAT_WARNING_SECONDS = 5 * 60
DEFAULT_HEARTBEAT_CRITICAL_SECONDS = 15 * 60


def env_int(
    name: str,
    default: int,
    *,
    minimum: int = 1,
    maximum: int = 86_400,
) -> int:
    """Read a bounded integer scheduler setting from the environment."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, min(value, maximum))
