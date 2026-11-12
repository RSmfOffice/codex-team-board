"""Pure read-model helpers shared by dashboard summary and snapshot.

These functions contain no database access; they turn already-loaded rows into
the derived fields the WebUI consumes. Keeping them in one module guarantees
that ``dashboard_summary`` and ``dashboard_snapshot`` compute identical
semantics from the same inputs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Mapping

CLAIM_WAIT_WARNING_SECONDS = 60
CLAIM_WAIT_CRITICAL_SECONDS = 300


def parse_iso(value: object) -> datetime | None:
    """Parse an ISO timestamp, normalizing naive values to UTC."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def age_seconds(value: object, now: datetime) -> int:
    parsed = parse_iso(value)
    if parsed is None:
        return 0
    return max(0, int((now - parsed).total_seconds()))


def heartbeat_state(age: int, warning_seconds: int, critical_seconds: int) -> str:
    if age >= critical_seconds:
        return "critical"
    if age >= warning_seconds:
        return "warning"
    return "normal"


def claim_wait_state(wait_seconds: int) -> str:
    if wait_seconds >= CLAIM_WAIT_CRITICAL_SECONDS:
        return "critical"
    if wait_seconds >= CLAIM_WAIT_WARNING_SECONDS:
        return "warning"
    return "normal"


def is_historical_task(
    task: Mapping[str, Any],
    archived_project_ids: set[str],
    archived_goal_ids: set[str],
) -> bool:
    if task.get("archived_at"):
        return True
    if str(task.get("project_id")) in archived_project_ids:
        return True
    goal_id = task.get("goal_id")
    return bool(goal_id and str(goal_id) in archived_goal_ids)


def worker_heartbeat_age(
    job: Mapping[str, Any],
    latest_activity: Mapping[str, str],
    now: datetime,
) -> int:
    """Age of a worker job's most recent claim/heartbeat/progress signal."""
    heartbeat_at = job.get("started_at")
    task_id = job.get("task_id")
    if task_id:
        activity_at = latest_activity.get(str(task_id))
        started = parse_iso(heartbeat_at)
        activity = parse_iso(activity_at)
        if activity and (started is None or activity >= started):
            heartbeat_at = activity_at
    return age_seconds(heartbeat_at, now)


def goal_queue_reason(
    goal_status: str,
    *,
    has_dispatchable_ready: bool,
    has_ready: bool,
    has_review: bool,
    has_blocked: bool,
    has_running: bool,
    goal_active_jobs: int,
    goal_active_workers: int,
    total_active_jobs: int,
    system_capacity: int,
    goal_capacity: int,
) -> str:
    if goal_status not in {"planning", "active"}:
        return "none"
    if total_active_jobs >= system_capacity and has_dispatchable_ready:
        return "global_capacity"
    if goal_active_workers >= goal_capacity and has_dispatchable_ready:
        return "goal_capacity"
    if has_review:
        return "review_pending"
    if has_blocked:
        return "blocked"
    if has_dispatchable_ready:
        return "ready"
    if has_ready:
        return "dependency_wait"
    if goal_active_jobs or has_running:
        return "running"
    if goal_status == "planning" or not goal_active_jobs:
        return "waiting_for_lead"
    return "none"


def goal_health(
    goal_status: str,
    *,
    is_historical: bool,
    is_stalled: bool,
    has_active_job: bool,
    has_blocked: bool,
    has_review: bool,
    has_critical_wait: bool,
    has_warning_wait: bool,
    has_running: bool,
) -> str:
    if is_historical:
        return "archived"
    if is_stalled:
        return "stalled"
    if goal_status == "canceled":
        return "canceled"
    if goal_status == "failed" or has_blocked or has_critical_wait:
        return "critical"
    if goal_status == "paused" or has_review or has_warning_wait:
        return "warning"
    if has_active_job or has_running:
        return "active"
    if goal_status == "planning":
        return "planning"
    return "healthy"


def goal_next_action(
    goal_status: str,
    *,
    is_stalled: bool,
    has_blocked: bool,
    has_critical_wait: bool,
    has_review: bool,
    has_active_job: bool,
    has_running: bool,
    has_ready: bool,
) -> str:
    if is_stalled:
        return "inspect_stalled_goal"
    if goal_status == "failed":
        return "inspect_failed_goal"
    if goal_status == "paused":
        return "resume_or_cancel_goal"
    if has_blocked:
        return "resolve_blocked_tasks"
    if has_critical_wait:
        return "assign_ready_tasks"
    if has_review:
        return "review_completed_tasks"
    if has_active_job:
        return "monitor_active_jobs"
    if has_running:
        return "monitor_running_tasks"
    if has_ready:
        return "schedule_ready_tasks"
    if goal_status == "planning":
        return "wait_for_lead"
    if goal_status in {"completed", "canceled"}:
        return "none"
    return "monitor_goal"