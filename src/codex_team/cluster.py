from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Settings
from .db import connect, connect_readonly, utc_now
from .processes import pid_is_running
from .scheduling import (
    DEFAULT_GLOBAL_JOB_CAPACITY,
    DEFAULT_GOAL_WORKER_CAPACITY,
    DEFAULT_HEARTBEAT_CRITICAL_SECONDS,
    DEFAULT_HEARTBEAT_WARNING_SECONDS,
    DEFAULT_LEAD_TIMEOUT_SECONDS,
    DEFAULT_WORKER_TIMEOUT_SECONDS,
    env_int,
)
from .service import TeamError, TeamService
from .worktrees import WorktreeManager

TERMINAL_GOAL_STATUSES = frozenset({"completed", "failed"})
ACTIVE_JOB_STATUSES = frozenset({"starting", "running"})
TERMINAL_TASK_STATUSES = frozenset({"done", "canceled"})
GOAL_STATUS_TRANSITIONS = {
    "planning": frozenset({"active", "paused", "failed"}),
    "active": frozenset({"paused", "completed", "failed"}),
    "paused": frozenset({"active", "failed"}),
    "completed": frozenset(),
    "failed": frozenset(),
}
WORKER_TIMEOUT_SECONDS = DEFAULT_WORKER_TIMEOUT_SECONDS
LEAD_TIMEOUT_SECONDS = DEFAULT_LEAD_TIMEOUT_SECONDS
FAIRNESS_DEFERRED_INTERVAL_SECONDS = 60
WORKER_ACTIVITY_EVENT_TYPES = ("claimed", "heartbeat", "progress")
_CLAIM_TOKEN_RE = re.compile(
    r'(?P<prefix>(?:\\?"claim_token\\?"\s*:\s*\\?"))(?P<token>[^"\\]+)(?P<suffix>\\?")'
)


class ClusterError(TeamError):
    pass


class ClusterManager:
    def __init__(
        self,
        settings: Settings,
        *,
        team: TeamService | None = None,
        worktrees: WorktreeManager | None = None,
        agent_command: list[str] | None = None,
        max_workers: int | None = None,
        max_jobs: int | None = None,
        lead_cooldown_seconds: float = 8.0,
    ) -> None:
        self.settings = settings
        self.team = team or TeamService(settings)
        self.worktrees = worktrees or WorktreeManager(self.team)
        self.agent_command = agent_command or _default_codex_command()
        configured_workers = (
            env_int(
                "CODEX_TEAM_CLUSTER_MAX_WORKERS",
                DEFAULT_GOAL_WORKER_CAPACITY,
            )
            if max_workers is None
            else int(max_workers)
        )
        configured_jobs = (
            env_int("CODEX_TEAM_CLUSTER_MAX_JOBS", DEFAULT_GLOBAL_JOB_CAPACITY)
            if max_jobs is None
            else int(max_jobs)
        )
        self.max_workers = max(1, min(configured_workers, 8))
        self.max_jobs = max(1, min(configured_jobs, 16))
        self.worker_timeout_seconds = env_int(
            "CODEX_TEAM_WORKER_TIMEOUT_SECONDS",
            DEFAULT_WORKER_TIMEOUT_SECONDS,
        )
        self.lead_timeout_seconds = env_int(
            "CODEX_TEAM_LEAD_TIMEOUT_SECONDS",
            DEFAULT_LEAD_TIMEOUT_SECONDS,
        )
        self.heartbeat_warning_seconds = env_int(
            "CODEX_TEAM_HEARTBEAT_WARNING_SECONDS",
            DEFAULT_HEARTBEAT_WARNING_SECONDS,
        )
        self.heartbeat_critical_seconds = max(
            self.heartbeat_warning_seconds,
            env_int(
                "CODEX_TEAM_HEARTBEAT_CRITICAL_SECONDS",
                DEFAULT_HEARTBEAT_CRITICAL_SECONDS,
            ),
        )
        self.lead_cooldown_seconds = max(0.0, float(lead_cooldown_seconds))
        self.processes: dict[str, subprocess.Popen[str]] = {}
        self.log_threads: dict[str, threading.Thread] = {}
        self.settings.ensure()
        (self.settings.logs_dir / "cluster").mkdir(parents=True, exist_ok=True)

    def create_goal(
        self,
        repo: str | Path,
        *,
        objective: str,
        autonomy: str = "auto",
        max_worker_attempts: int = 3,
        max_lead_attempts: int = 5,
    ) -> dict[str, Any]:
        objective = objective.strip()
        if not objective:
            raise ClusterError("objective must not be empty")
        if autonomy not in {"auto", "supervised"}:
            raise ClusterError("autonomy must be auto or supervised")
        project = self.team.init_project(repo)
        goal_id = self._new_id("G")
        now = utc_now()
        with self.team.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                """
                SELECT goal_id FROM cluster_goals
                WHERE project_id = ? AND status IN ('planning', 'active')
                  AND archived_at IS NULL
                """,
                (project["project_id"],),
            ).fetchone()
            if active:
                raise ClusterError(f"project already has active goal: {active['goal_id']}")
            connection.execute(
                """
                INSERT INTO cluster_goals(
                    goal_id, project_id, objective, status, autonomy,
                    max_worker_attempts, max_lead_attempts, created_at, updated_at
                ) VALUES (?, ?, ?, 'planning', ?, ?, ?, ?, ?)
                """,
                (
                    goal_id,
                    project["project_id"],
                    objective,
                    autonomy,
                    max(1, min(int(max_worker_attempts), 10)),
                    max(1, min(int(max_lead_attempts), 20)),
                    now,
                    now,
                ),
            )
            self._event(connection, goal_id, "goal_created", "teamd", {"objective": objective})
        return self.get_goal(goal_id)

    def ensure_goal(
        self,
        repo: str | Path,
        *,
        objective: str,
        autonomy: str = "auto",
        max_worker_attempts: int = 3,
        max_lead_attempts: int = 5,
    ) -> dict[str, Any]:
        """Return a project's unfinished goal, creating one when none exists."""
        objective = objective.strip()
        if not objective:
            raise ClusterError("objective must not be empty")
        if autonomy not in {"auto", "supervised"}:
            raise ClusterError("autonomy must be auto or supervised")
        project = self.team.init_project(repo)
        now = utc_now()
        created = False
        with self.team.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                """
                SELECT goal_id, status, objective
                FROM cluster_goals
                WHERE project_id = ?
                  AND status IN ('planning', 'active', 'paused')
                  AND archived_at IS NULL
                ORDER BY
                    CASE status
                        WHEN 'active' THEN 0
                        WHEN 'planning' THEN 1
                        ELSE 2
                    END,
                    updated_at DESC
                LIMIT 1
                """,
                (project["project_id"],),
            ).fetchone()
            if active:
                goal_id = str(active["goal_id"])
                self._event(
                    connection,
                    goal_id,
                    "goal_reused",
                    "lead",
                    {
                        "status": active["status"],
                        "existing_objective": active["objective"],
                        "requested_objective": objective,
                    },
                )
            else:
                goal_id = self._new_id("G")
                created = True
                connection.execute(
                    """
                    INSERT INTO cluster_goals(
                        goal_id, project_id, objective, status, autonomy,
                        max_worker_attempts, max_lead_attempts, created_at, updated_at
                    ) VALUES (?, ?, ?, 'planning', ?, ?, ?, ?, ?)
                    """,
                    (
                        goal_id,
                        project["project_id"],
                        objective,
                        autonomy,
                        max(1, min(int(max_worker_attempts), 10)),
                        max(1, min(int(max_lead_attempts), 20)),
                        now,
                        now,
                    ),
                )
                self._event(
                    connection,
                    goal_id,
                    "goal_created",
                    "lead",
                    {"objective": objective, "via": "ensure_goal"},
                )
        goal = self.get_goal(goal_id)
        return {
            "goal": goal,
            "created": created,
            "reused": not created,
            "requires_resume": goal["status"] == "paused",
        }

    def get_goal(self, goal_id: str) -> dict[str, Any]:
        with self.team.connection(write=False) as connection:
            row = connection.execute(
                """
                SELECT g.*, p.name AS project_name, p.root_path
                FROM cluster_goals g JOIN projects p ON p.project_id = g.project_id
                WHERE g.goal_id = ?
                """,
                (goal_id,),
            ).fetchone()
            if row is None:
                raise ClusterError(f"cluster goal not found: {goal_id}")
            result = dict(row)
            result["jobs"] = [
                dict(item)
                for item in connection.execute(
                    """
                    SELECT * FROM cluster_jobs
                    WHERE goal_id = ? ORDER BY started_at DESC LIMIT 30
                    """,
                    (goal_id,),
                ).fetchall()
            ]
            result["events"] = self._goal_events(connection, goal_id, limit=50)
            result["tasks"] = self.team.list_tasks(
                result["root_path"],
                goal_id=goal_id,
                include_done=True,
            )
            return result

    def list_goals(
        self,
        statuses: list[str] | None = None,
        *,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        with self.team.connection(write=False) as connection:
            archive_filter = "" if include_archived else "AND g.archived_at IS NULL"
            if statuses:
                placeholders = ",".join("?" for _ in statuses)
                rows = connection.execute(
                    f"""
                    SELECT g.*, p.name AS project_name, p.root_path
                    FROM cluster_goals g JOIN projects p ON p.project_id = g.project_id
                    WHERE g.status IN ({placeholders})
                    {archive_filter}
                    ORDER BY g.updated_at DESC
                    """,
                    statuses,
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT g.*, p.name AS project_name, p.root_path
                    FROM cluster_goals g JOIN projects p ON p.project_id = g.project_id
                    WHERE 1 = 1
                    {archive_filter}
                    ORDER BY g.updated_at DESC
                    """
                ).fetchall()
            return [dict(row) for row in rows]

    def set_goal_status(self, goal_id: str, status: str, *, reason: str = "") -> dict[str, Any]:
        if status not in {"planning", "active", "paused", "completed", "failed"}:
            raise ClusterError("invalid goal status")
        completed_at = utc_now() if status in TERMINAL_GOAL_STATUSES else None
        with self.team.connection() as connection:
            row = connection.execute(
                "SELECT * FROM cluster_goals WHERE goal_id = ?",
                (goal_id,),
            ).fetchone()
            if row is None:
                raise ClusterError(f"cluster goal not found: {goal_id}")
            if row["archived_at"]:
                raise ClusterError("archived goal cannot change status; restore it first")
            current_status = row["status"]
            if (
                status != current_status
                and status not in GOAL_STATUS_TRANSITIONS[current_status]
            ):
                raise ClusterError(
                    f"invalid goal transition: {current_status} -> {status}"
                )
            if status == "active":
                competing = connection.execute(
                    """
                    SELECT goal_id FROM cluster_goals
                    WHERE project_id = ? AND goal_id != ?
                      AND status IN ('planning', 'active')
                    LIMIT 1
                    """,
                    (row["project_id"], goal_id),
                ).fetchone()
                if competing:
                    raise ClusterError(
                        f"project already has active goal: {competing['goal_id']}"
                    )
            connection.execute(
                """
                UPDATE cluster_goals
                SET status = ?, updated_at = ?, completed_at = ?, last_error = ?
                WHERE goal_id = ?
                """,
                (status, utc_now(), completed_at, reason, goal_id),
            )
            self._event(
                connection,
                goal_id,
                "goal_status",
                "lead",
                {"status": status, "reason": reason},
            )
        return self.get_goal(goal_id)

    def cancel_goal(self, goal_id: str, *, reason: str = "canceled by user") -> dict[str, Any]:
        goal = self.get_goal(goal_id)
        if goal["status"] in TERMINAL_GOAL_STATUSES:
            raise ClusterError(f"cannot cancel terminal goal: {goal['status']}")
        with self.team.connection(write=False) as connection:
            active_jobs = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT job_id, pid FROM cluster_jobs
                    WHERE goal_id = ? AND status IN ('starting', 'running')
                    """,
                    (goal_id,),
                ).fetchall()
            ]
            tasks = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT task_id, status FROM tasks
                    WHERE goal_id = ? AND status NOT IN ('done', 'canceled')
                    """,
                    (goal_id,),
                ).fetchall()
            ]
        for job in active_jobs:
            self.processes.pop(job["job_id"], None)
            pid = int(job.get("pid") or 0)
            if pid and pid_is_running(pid):
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
        now = utc_now()
        with self.team.connection() as connection:
            for job in active_jobs:
                connection.execute(
                    """
                    UPDATE cluster_jobs
                    SET status = 'interrupted', finished_at = ?, exit_code = -9,
                        error = ?
                    WHERE job_id = ?
                    """,
                    (now, reason, job["job_id"]),
                )
            for task in tasks:
                connection.execute(
                    """
                    UPDATE tasks
                    SET status = 'blocked', blocker = ?, claimed_by = NULL,
                        claim_token = NULL, lease_until = NULL,
                        updated_at = ?, version = version + 1
                    WHERE task_id = ?
                    """,
                    (f"goal canceled: {reason}", now, task["task_id"]),
                )
                self.team._event(
                    connection,
                    task["task_id"],
                    "goal_canceled",
                    "teamd",
                    {"goal_id": goal_id, "reason": reason},
                )
        return self.set_goal_status(goal_id, "failed", reason=reason)

    def set_goal_archived(
        self,
        goal_id: str,
        archived: bool,
        *,
        actor: str = "human",
    ) -> dict[str, Any]:
        with self.team.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            goal = connection.execute(
                "SELECT * FROM cluster_goals WHERE goal_id = ?",
                (goal_id,),
            ).fetchone()
            if goal is None:
                raise ClusterError(f"cluster goal not found: {goal_id}")
            if archived:
                if goal["status"] not in TERMINAL_GOAL_STATUSES:
                    raise ClusterError(
                        "only completed or failed cluster goals can be archived"
                    )
                active_job = connection.execute(
                    """
                    SELECT job_id FROM cluster_jobs
                    WHERE goal_id = ? AND status IN ('starting', 'running')
                    LIMIT 1
                    """,
                    (goal_id,),
                ).fetchone()
                if active_job:
                    raise ClusterError(
                        f"goal has an active job: {active_job['job_id']}"
                    )
            archived_at = utc_now() if archived else None
            connection.execute(
                """
                UPDATE cluster_goals
                SET archived_at = ?, updated_at = ?
                WHERE goal_id = ?
                """,
                (archived_at, utc_now(), goal_id),
            )
            self._event(
                connection,
                goal_id,
                "goal_archived" if archived else "goal_restored",
                actor,
                {"archived": archived},
            )
        return self.get_goal(goal_id)

    def tick(self) -> dict[str, Any]:
        self._reconcile_jobs()
        self._check_worker_health()
        goals = self.list_goals(["planning", "active"])
        schedulable: list[dict[str, Any]] = []
        for goal in goals:
            if self._prepare_goal(goal):
                schedulable.append(goal)
        scheduled = self._schedule_goals(schedulable)
        return {"tick_at": utc_now(), "scheduled": scheduled, "goals": len(goals)}

    def _prepare_goal(self, goal: dict[str, Any]) -> bool:
        goal_id = goal["goal_id"]
        root = Path(goal["root_path"])
        tasks = self.team.list_tasks(root, goal_id=goal_id, include_done=True)
        active_workers = self._active_worker_jobs(goal_id)
        active_lead = self._active_lead_job(goal_id)
        if (
            tasks
            and all(item["status"] in TERMINAL_TASK_STATUSES for item in tasks)
            and not active_workers
            and not active_lead
        ):
            self.set_goal_status(goal_id, "completed", reason="all tasks completed")
            return False
        if (
            goal["autonomy"] == "supervised"
            and any(item["status"] == "review" for item in tasks)
        ):
            self.set_goal_status(
                goal_id,
                "paused",
                reason="waiting for human review in supervised mode",
            )
            return False

        for task in tasks:
            if task["status"] == "running" and not active_workers.get(task["task_id"]):
                if task.get("lease_expired"):
                    self._recover_task(
                        task["task_id"],
                        goal_id,
                        reason="worker lease expired without an active job",
                    )
        return True

    def _schedule_goals(self, goals: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not goals:
            return []

        scheduled: list[dict[str, Any]] = []
        blocked_goals: set[str] = set()
        max_passes = self.max_jobs + len(goals) * 2
        active_jobs = self._active_job_count()
        for _ in range(max(2, max_passes)):
            candidates = [
                goal for goal in goals if goal["goal_id"] not in blocked_goals
            ]
            if not candidates:
                break
            changed = False
            for goal in self._fair_goal_order(candidates):
                if active_jobs >= self.max_jobs:
                    self._record_global_full_deferrals(candidates)
                    return scheduled
                tasks = self.team.list_tasks(
                    goal["root_path"],
                    goal_id=goal["goal_id"],
                    include_done=True,
                )
                job, goal_changed, keep_goal = self._dispatch_next_job(goal, tasks)
                if job is not None:
                    scheduled.append(job)
                    active_jobs += 1
                    changed = True
                elif goal_changed:
                    changed = True
                if not keep_goal:
                    blocked_goals.add(goal["goal_id"])
            if not changed:
                break
        return scheduled

    def _dispatch_next_job(
        self,
        goal: dict[str, Any],
        tasks: list[dict[str, Any]],
    ) -> tuple[dict[str, Any] | None, bool, bool]:
        goal_id = goal["goal_id"]
        active_workers = self._active_worker_jobs(goal_id)
        candidates = self._ready_worker_candidates(goal, tasks, active_workers)
        if len(active_workers) < self.max_workers and candidates:
            task = candidates[0]
            try:
                return self._dispatch_worker(goal, task), True, True
            except Exception as exc:
                self.team.block_task(
                    task["task_id"],
                    actor="teamd",
                    reason=f"automatic dispatch failed: {exc}",
                )
                with self.team.connection() as connection:
                    self._event(
                        connection,
                        goal_id,
                        "dispatch_failed",
                        "teamd",
                        {"task_id": task["task_id"], "error": str(exc)},
                    )
                return None, True, True

        active_lead = self._active_lead_job(goal_id)
        needs_lead = (
            not tasks
            or any(item["status"] in {"review", "blocked"} for item in tasks)
        )
        if (
            needs_lead
            and not active_workers
            and not active_lead
            and not self._lead_cooldown_active(goal_id)
        ):
            try:
                return self._dispatch_lead(goal, tasks), True, True
            except Exception as exc:
                with self.team.connection() as connection:
                    connection.execute(
                        """
                        UPDATE cluster_goals
                        SET status = 'failed', last_error = ?, updated_at = ?
                        WHERE goal_id = ?
                        """,
                        (f"lead dispatch failed: {exc}", utc_now(), goal_id),
                    )
                    self._event(
                        connection,
                        goal_id,
                        "lead_dispatch_failed",
                        "teamd",
                        {"error": str(exc)},
                    )
                return None, True, False
        return None, False, True

    def _ready_worker_candidates(
        self,
        goal: dict[str, Any],
        tasks: list[dict[str, Any]],
        active_workers: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        active_task_ids = set(active_workers)
        candidates = [
            task
            for task in tasks
            if task["status"] == "ready"
            and not task.get("blocked_by")
            and task["task_id"] not in active_task_ids
            and self._retry_allowed(task["task_id"], goal)
        ]
        candidates.sort(key=lambda item: (int(item["priority"]), item["created_at"]))
        return candidates

    def _fair_goal_order(
        self,
        goals: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        goal_ids = [str(goal["goal_id"]) for goal in goals]
        placeholders = ",".join("?" for _ in goal_ids)
        with self.team.connection(write=False) as connection:
            rows = connection.execute(
                f"""
                SELECT g.goal_id, g.created_at,
                       COALESCE(MAX(j.started_at), g.created_at) AS last_started_at,
                       COALESCE(SUM(
                           CASE WHEN j.status IN ('starting', 'running') THEN 1 ELSE 0 END
                       ), 0) AS active_jobs
                FROM cluster_goals g
                LEFT JOIN cluster_jobs j ON j.goal_id = g.goal_id
                WHERE g.goal_id IN ({placeholders})
                GROUP BY g.goal_id, g.created_at
                """,
                goal_ids,
            ).fetchall()
        fairness = {str(row["goal_id"]): dict(row) for row in rows}
        return sorted(
            goals,
            key=lambda goal: (
                fairness.get(str(goal["goal_id"]), {}).get(
                    "last_started_at",
                    goal["created_at"],
                ),
                int(
                    fairness.get(str(goal["goal_id"]), {}).get(
                        "active_jobs",
                        0,
                    )
                ),
                str(goal["goal_id"]),
            ),
        )

    def _active_job_count(self) -> int:
        with self.team.connection(write=False) as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS active_jobs
                FROM cluster_jobs
                WHERE status IN ('starting', 'running')
                """
            ).fetchone()
        return int(row["active_jobs"])

    def _record_global_full_deferrals(self, goals: list[dict[str, Any]]) -> None:
        reason = "global capacity full"
        for goal in goals:
            tasks = self.team.list_tasks(
                goal["root_path"],
                goal_id=goal["goal_id"],
                include_done=True,
            )
            active_workers = self._active_worker_jobs(goal["goal_id"])
            ready_tasks = len(
                self._ready_worker_candidates(goal, tasks, active_workers)
            )
            if ready_tasks:
                self._record_fairness_deferred(
                    goal["goal_id"],
                    reason=reason,
                    ready_tasks=ready_tasks,
                )

    def _record_fairness_deferred(
        self,
        goal_id: str,
        *,
        reason: str,
        ready_tasks: int,
    ) -> None:
        now = datetime.now(UTC)
        with self.team.connection() as connection:
            rows = connection.execute(
                """
                SELECT data_json, created_at
                FROM cluster_events
                WHERE goal_id = ? AND event_type = 'fairness_deferred'
                ORDER BY event_id DESC
                LIMIT 100
                """,
                (goal_id,),
            ).fetchall()
            for row in rows:
                try:
                    existing_reason = json.loads(row["data_json"]).get("reason")
                except (TypeError, ValueError):
                    existing_reason = None
                created = _parse_iso(row["created_at"])
                if (
                    existing_reason == reason
                    and created is not None
                    and (now - created).total_seconds()
                    < FAIRNESS_DEFERRED_INTERVAL_SECONDS
                ):
                    return
            self._event(
                connection,
                goal_id,
                "fairness_deferred",
                "teamd",
                {
                    "reason": reason,
                    "ready_tasks": ready_tasks,
                    "capacity": self.max_jobs,
                },
            )

    def _check_worker_health(self) -> None:
        now = datetime.now(UTC)
        with self.team.connection(write=False) as connection:
            jobs = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT job_id, goal_id, task_id, started_at
                    FROM cluster_jobs
                    WHERE kind = 'worker'
                      AND status IN ('starting', 'running')
                      AND task_id IS NOT NULL
                    ORDER BY started_at
                    """
                ).fetchall()
            ]
            activities: dict[str, str] = {}
            for job in jobs:
                activity_placeholders = ",".join(
                    "?" for _ in WORKER_ACTIVITY_EVENT_TYPES
                )
                row = connection.execute(
                    f"""
                    SELECT MAX(created_at) AS last_activity_at
                    FROM task_events
                    WHERE task_id = ?
                      AND event_type IN ({activity_placeholders})
                    """,
                    (job["task_id"], *WORKER_ACTIVITY_EVENT_TYPES),
                ).fetchone()
                if row and row["last_activity_at"]:
                    activities[str(job["task_id"])] = str(row["last_activity_at"])

        for job in jobs:
            started_at = _parse_iso(job.get("started_at"))
            event_activity = _parse_iso(activities.get(str(job["task_id"])))
            last_activity = event_activity or started_at
            if last_activity is None:
                continue
            if started_at is not None and last_activity < started_at:
                last_activity = started_at
            idle_seconds = (now - last_activity).total_seconds()
            if idle_seconds >= self.heartbeat_critical_seconds:
                severity = "critical"
                event_type = "worker_heartbeat_critical"
            elif idle_seconds >= self.heartbeat_warning_seconds:
                severity = "warning"
                event_type = "worker_heartbeat_warning"
            else:
                continue
            if self._worker_health_event_exists(
                job,
                event_type,
                last_activity,
            ):
                continue
            with self.team.connection() as connection:
                self._event(
                    connection,
                    job["goal_id"],
                    event_type,
                    "teamd",
                    {
                        "job_id": job["job_id"],
                        "task_id": job["task_id"],
                        "severity": severity,
                        "idle_seconds": int(idle_seconds),
                        "last_activity_at": last_activity.isoformat().replace(
                            "+00:00",
                            "Z",
                        ),
                        "warning_seconds": self.heartbeat_warning_seconds,
                        "critical_seconds": self.heartbeat_critical_seconds,
                        "action": "alert",
                    },
                )

    def _worker_health_event_exists(
        self,
        job: dict[str, Any],
        event_type: str,
        last_activity: datetime,
    ) -> bool:
        with self.team.connection(write=False) as connection:
            rows = connection.execute(
                """
                SELECT data_json
                FROM cluster_events
                WHERE goal_id = ? AND event_type = ?
                ORDER BY event_id DESC
                LIMIT 100
                """,
                (job["goal_id"], event_type),
            ).fetchall()
        for row in rows:
            try:
                data = json.loads(row["data_json"])
            except (TypeError, ValueError):
                continue
            if (
                data.get("job_id") == job["job_id"]
                and data.get("last_activity_at")
                == last_activity.isoformat().replace("+00:00", "Z")
            ):
                return True
        return False

    def _dispatch_worker(self, goal: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
        role = str(task["owner_role"])
        if role not in self.team.ROLE_FILES:
            raise ClusterError(f"unsupported worker role: {role}")
        if task["kind"] == "write":
            current = self.team.get_task(task["task_id"])
            if not current.get("worktree_path"):
                self.worktrees.create(
                    task["task_id"],
                    repo=goal["root_path"],
                    actor="teamd",
                )
                task = self.team.get_task(task["task_id"])
            if not task.get("worktree_path"):
                raise ClusterError("write task has no worktree")
            cwd = Path(task["worktree_path"])
        else:
            cwd = Path(goal["root_path"])
        role_data = self.team.role_prompt(role, goal["root_path"])
        actor = f"auto-{role}-{task['task_id'].lower()}"
        prompt = f"""{role_data["prompt"]}

# Automated assignment

- Goal ID: `{goal["goal_id"]}`
- Task ID: `{task["task_id"]}`
- Repository: `{goal["root_path"]}`
- Worktree: `{task.get("worktree_path") or "(none)"}`
- Actor name: `{actor}`

Use the `codexTeamBoard` MCP tools. Do not spawn subagents or another team.
First call `team_claim_task` for this task with actor `{actor}`.
Work only on this task. Keep the claim token private. For jobs that may exceed
four minutes, call `team_heartbeat` at least every three minutes. If your context
approaches its limit, commit safe progress, call `team_block_task` with the reason
`worker-context-near-limit`, and stop. Before exiting, either call
`team_submit_task` with commit SHA, result, changed files, risks, tests and
review focus, or call `team_block_task` with a precise blocker.
"""
        return self._start_job(
            goal,
            task_id=task["task_id"],
            kind="worker",
            role=role,
            cwd=cwd,
            prompt=prompt,
            sandbox_mode=self._worker_sandbox_mode(task),
        )

    @staticmethod
    def _worker_sandbox_mode(task: dict[str, Any]) -> str:
        return (
            "danger-full-access"
            if task.get("kind") == "write"
            else "read-only"
        )

    def _dispatch_lead(
        self,
        goal: dict[str, Any],
        tasks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if int(goal["lead_ticks"]) >= 20:
            self.set_goal_status(goal["goal_id"], "failed", reason="lead tick limit exceeded")
            raise ClusterError("lead tick limit exceeded")
        board = {
            "goal": {
                "goal_id": goal["goal_id"],
                "objective": goal["objective"],
                "autonomy": goal["autonomy"],
            },
            "tasks": tasks,
        }
        prompt = f"""You are the managed Lead for an automated Codex work cluster.
Do not spawn subagents; the deterministic teamd dispatcher will launch Workers.
You may use shell tools and codexTeamBoard MCP tools.

Goal:
{goal["objective"]}

Current board:
```json
{json.dumps(board, ensure_ascii=False, indent=2)}
```

Rules:
1. If there are no tasks, decompose the goal into small, non-overlapping tasks with
   `goal_id = "{goal["goal_id"]}"`, acceptance criteria, dependencies, and owner roles.
   Use kind write for code changes and team_worker; use read for team_explorer.
2. Ensure `AGENTS.md` and `.codex` team files are committed before write worktrees.
3. For review tasks, inspect the diff and `team_get_task_events`, run tests, merge the
   task branch into its recorded target branch, then call `team_review_task` with the
   real merge_commit. Do not approve failed or dirty work.
4. For blocked tasks, resolve the blocker with a concrete action; unblock, cancel, or
   create a replacement task. Never loop without progress.
5. In supervised mode, do not merge or mark review tasks done; report that human review
   is required and stop.
6. Do not mark a task done without a real merge commit and passing integration tests.
7. Return a concise progress summary and the next concrete board state.
"""
        thread_id = goal.get("lead_thread_id")
        return self._start_job(
            goal,
            task_id=None,
            kind="lead",
            role="lead",
            cwd=Path(goal["root_path"]),
            prompt=prompt,
            resume_thread_id=thread_id,
            sandbox_mode=(
                "danger-full-access"
                if goal["autonomy"] == "auto"
                else "workspace-write"
            ),
        )

    def _start_job(
        self,
        goal: dict[str, Any],
        *,
        task_id: str | None,
        kind: str,
        role: str,
        cwd: Path,
        prompt: str,
        resume_thread_id: str | None = None,
        sandbox_mode: str = "workspace-write",
    ) -> dict[str, Any]:
        if kind not in {"lead", "worker"}:
            raise ClusterError("invalid cluster job kind")
        if sandbox_mode not in {
            "read-only",
            "workspace-write",
            "danger-full-access",
        }:
            raise ClusterError(f"invalid sandbox mode: {sandbox_mode}")
        job_id = self._new_id("J")
        goal_dir = self.settings.logs_dir / "cluster" / goal["goal_id"]
        goal_dir.mkdir(parents=True, exist_ok=True)
        log_path = goal_dir / f"{job_id}.jsonl"
        output_path = goal_dir / f"{job_id}.last.md"
        attempt = self._next_attempt(goal["goal_id"], task_id, kind)
        with self.team.connection() as connection:
            connection.execute(
                """
                INSERT INTO cluster_jobs(
                    job_id, goal_id, task_id, kind, role, status,
                    attempt, log_path, output_path, started_at
                ) VALUES (?, ?, ?, ?, ?, 'starting', ?, ?, ?, ?)
                """,
                (
                    job_id,
                    goal["goal_id"],
                    task_id,
                    kind,
                    role,
                    attempt,
                    str(log_path),
                    str(output_path),
                    utc_now(),
                ),
            )
            self._event(
                connection,
                goal["goal_id"],
                "job_started",
                "teamd",
                {
                    "job_id": job_id,
                    "task_id": task_id,
                    "kind": kind,
                    "role": role,
                    "attempt": attempt,
                    "resume_thread_id": resume_thread_id,
                    "sandbox_mode": sandbox_mode,
                },
            )
        command = self._build_command(
            cwd,
            output_path,
            resume_thread_id=resume_thread_id,
            sandbox_mode=sandbox_mode,
        )
        environment = os.environ.copy()
        environment.update(
            {
                "CODEX_TEAM_GOAL_ID": goal["goal_id"],
                "CODEX_TEAM_JOB_ID": job_id,
                "CODEX_TEAM_TASK_ID": task_id or "",
                "CODEX_TEAM_ROLE": role,
                "CODEX_TEAM_REPO_ROOT": goal["root_path"],
                "PYTHONUTF8": "1",
            }
        )
        creationflags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
        try:
            with log_path.open("a", encoding="utf-8") as log_stream:
                log_stream.write(
                    f"# command={json.dumps(command, ensure_ascii=False)}\n"
                )
            process = subprocess.Popen(
                command,
                cwd=str(cwd),
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
                close_fds=True,
            )
            if process.stdout is None:
                raise ClusterError(f"could not capture {kind} output")
            pump = threading.Thread(
                target=self._pump_log,
                args=(process.stdout, log_path),
                name=f"cluster-log-{job_id}",
                daemon=True,
            )
            pump.start()
            self.log_threads[job_id] = pump
            if process.stdin:
                process.stdin.write(prompt)
                process.stdin.close()
        except Exception as exc:
            with self.team.connection() as connection:
                connection.execute(
                    """
                    UPDATE cluster_jobs
                    SET status = 'failed', finished_at = ?, error = ?
                    WHERE job_id = ?
                    """,
                    (utc_now(), str(exc), job_id),
                )
                self._event(
                    connection,
                    goal["goal_id"],
                    "job_start_failed",
                    "teamd",
                    {"job_id": job_id, "error": str(exc)},
                )
            raise ClusterError(f"could not start {kind} process: {exc}") from exc
        self.processes[job_id] = process
        with self.team.connection() as connection:
            connection.execute(
                """
                UPDATE cluster_jobs
                SET status = 'running', pid = ?, thread_id = ?
                WHERE job_id = ?
                """,
                (process.pid, resume_thread_id, job_id),
            )
            connection.execute(
                """
                UPDATE cluster_goals
                SET status = CASE WHEN status = 'planning' THEN 'active' ELSE status END,
                    updated_at = ?
                WHERE goal_id = ?
                """,
                (utc_now(), goal["goal_id"]),
            )
        return {
            "job_id": job_id,
            "goal_id": goal["goal_id"],
            "task_id": task_id,
            "kind": kind,
            "role": role,
            "pid": process.pid,
            "attempt": attempt,
            "log_path": str(log_path),
        }

    @staticmethod
    def _pump_log(stream: Any, log_path: Path) -> None:
        try:
            with log_path.open("a", encoding="utf-8") as log_stream:
                for line in stream:
                    log_stream.write(_redact_sensitive_log(line))
                    log_stream.flush()
        finally:
            stream.close()

    def _build_command(
        self,
        cwd: Path,
        output_path: Path,
        *,
        resume_thread_id: str | None,
        sandbox_mode: str,
    ) -> list[str]:
        common = ["--json", "--disable", "multi_agent", "-o", str(output_path)]
        if resume_thread_id:
            return [
                *self.agent_command,
                "exec",
                "resume",
                resume_thread_id,
                *common,
                "-c",
                f'sandbox_mode="{sandbox_mode}"',
                "-",
            ]
        return [
            *self.agent_command,
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            sandbox_mode,
            "-C",
            str(cwd),
            *common,
            "-",
        ]

    def _reconcile_jobs(self) -> None:
        with self.team.connection(write=False) as connection:
            rows = connection.execute(
                """
                SELECT j.*, g.objective, g.autonomy, g.max_worker_attempts,
                       g.max_lead_attempts, g.lead_thread_id, g.lead_failures,
                       g.lead_ticks, p.root_path
                FROM cluster_jobs j
                JOIN cluster_goals g ON g.goal_id = j.goal_id
                JOIN projects p ON p.project_id = g.project_id
                WHERE j.status IN ('starting', 'running')
                ORDER BY j.started_at
                """
            ).fetchall()
        for row in rows:
            job = dict(row)
            process = self.processes.get(job["job_id"])
            if self._job_timed_out(job):
                if process is not None and process.poll() is None:
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                self._finish_job(job, -9, forced_error="job timeout")
                continue
            exit_code = process.poll() if process is not None else None
            if process is not None and exit_code is None:
                continue
            if process is None and job.get("pid") and pid_is_running(int(job["pid"])):
                continue
            self._finish_job(job, exit_code)

    def _finish_job(
        self,
        job: dict[str, Any],
        exit_code: int | None,
        *,
        forced_error: str = "",
    ) -> None:
        pump = self.log_threads.pop(job["job_id"], None)
        if pump is not None:
            pump.join(timeout=5)
        for path_key in ("log_path", "output_path"):
            path_value = job.get(path_key)
            if path_value:
                _redact_sensitive_file(Path(path_value))
        parsed = _parse_codex_log(Path(job["log_path"]))
        thread_id = job.get("thread_id") or parsed.get("thread_id")
        process_ok = (
            not forced_error
            and (
                exit_code == 0
                if exit_code is not None
                else bool(parsed.get("completed"))
            )
        )
        process_error = forced_error or parsed.get("error", "")
        if job["kind"] == "worker":
            task = self.team.get_task(job["task_id"])
            worker_ok = task["status"] in {"review", "done", "blocked"}
            status = "completed" if process_ok and worker_ok else "failed"
            error = ""
            if not worker_ok:
                error = "worker exited without submitting or blocking its task"
            elif task["status"] == "blocked" and process_error:
                error = process_error
            with self.team.connection() as connection:
                connection.execute(
                    """
                    UPDATE cluster_jobs
                    SET status = ?, thread_id = ?, finished_at = ?, exit_code = ?, error = ?
                    WHERE job_id = ?
                    """,
                    (status, thread_id, utc_now(), exit_code, error, job["job_id"]),
                )
                self._event(
                    connection,
                    job["goal_id"],
                    "worker_finished",
                    "teamd",
                    {
                        "job_id": job["job_id"],
                        "task_id": job["task_id"],
                        "status": status,
                        "exit_code": exit_code,
                        "thread_id": thread_id,
                        "error": error or process_error,
                    },
                )
            self.processes.pop(job["job_id"], None)
            if not worker_ok or not process_ok:
                self._handle_worker_failure(job, error or process_error or "worker failure")
            return

        status = "completed" if process_ok else "failed"
        error = "" if process_ok else process_error or (
            f"lead process exited with code {exit_code}" if exit_code is not None else "lead process lost"
        )
        with self.team.connection() as connection:
            connection.execute(
                """
                UPDATE cluster_jobs
                SET status = ?, thread_id = ?, finished_at = ?, exit_code = ?, error = ?
                WHERE job_id = ?
                """,
                (status, thread_id, utc_now(), exit_code, error, job["job_id"]),
            )
            if process_ok:
                connection.execute(
                    """
                    UPDATE cluster_goals
                    SET lead_thread_id = COALESCE(?, lead_thread_id),
                        lead_ticks = lead_ticks + 1,
                        lead_failures = 0,
                        last_error = '',
                        updated_at = ?
                    WHERE goal_id = ?
                    """,
                    (thread_id, utc_now(), job["goal_id"]),
                )
            else:
                failures = int(job.get("lead_failures") or 0) + 1
                reset_thread = failures >= 2
                connection.execute(
                    """
                    UPDATE cluster_goals
                    SET lead_thread_id = CASE
                            WHEN ? THEN NULL
                            ELSE COALESCE(lead_thread_id, ?)
                        END,
                        lead_generation = lead_generation + CASE WHEN ? THEN 1 ELSE 0 END,
                        lead_failures = ?, last_error = ?, updated_at = ?
                    WHERE goal_id = ?
                    """,
                    (
                        int(reset_thread),
                        thread_id,
                        int(reset_thread),
                        failures,
                        error,
                        utc_now(),
                        job["goal_id"],
                    ),
                )
                if failures >= int(job.get("max_lead_attempts") or 5):
                    connection.execute(
                        """
                        UPDATE cluster_goals
                        SET status = 'failed', completed_at = ?, updated_at = ?
                        WHERE goal_id = ?
                        """,
                        (utc_now(), utc_now(), job["goal_id"]),
                    )
            self._event(
                connection,
                job["goal_id"],
                "lead_finished",
                "teamd",
                {
                    "job_id": job["job_id"],
                    "status": status,
                    "exit_code": exit_code,
                    "thread_id": thread_id,
                    "error": error,
                },
            )
        self.processes.pop(job["job_id"], None)

    def _job_timed_out(self, job: dict[str, Any]) -> bool:
        started = _parse_iso(job.get("started_at"))
        if started is None:
            return True
        timeout = (
            self.lead_timeout_seconds
            if job.get("kind") == "lead"
            else self.worker_timeout_seconds
        )
        return (datetime.now(UTC) - started).total_seconds() > timeout

    def _handle_worker_failure(self, job: dict[str, Any], reason: str) -> None:
        task = self.team.get_task(job["task_id"])
        if task["status"] in {"review", "done"}:
            return
        if task["status"] == "blocked":
            return
        if int(job["attempt"]) >= int(job.get("max_worker_attempts") or 3):
            self.team.block_task(
                task["task_id"],
                actor="teamd",
                reason=f"automatic worker retries exhausted: {reason}",
            )
            return
        self._recover_task(
            task["task_id"],
            job["goal_id"],
            reason=reason,
            job_id=job["job_id"],
            attempt=int(job["attempt"]),
        )

    def _recover_task(
        self,
        task_id: str,
        goal_id: str,
        *,
        reason: str,
        job_id: str | None = None,
        attempt: int = 0,
    ) -> None:
        task = self.team.get_task(task_id)
        if task["status"] in TERMINAL_TASK_STATUSES or task["status"] == "review":
            return
        with self.team.connection() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET status = 'ready', claimed_by = NULL, claim_token = NULL,
                    lease_until = NULL, blocker = ?, updated_at = ?,
                    version = version + 1
                WHERE task_id = ?
                """,
                (reason, utc_now(), task_id),
            )
            self._event(
                connection,
                goal_id,
                "worker_recovered",
                "teamd",
                {
                    "task_id": task_id,
                    "job_id": job_id,
                    "attempt": attempt,
                    "reason": reason,
                },
            )

    def _active_worker_jobs(self, goal_id: str) -> dict[str, dict[str, Any]]:
        with self.team.connection(write=False) as connection:
            rows = connection.execute(
                """
                SELECT * FROM cluster_jobs
                WHERE goal_id = ? AND kind = 'worker'
                  AND status IN ('starting', 'running')
                """,
                (goal_id,),
            ).fetchall()
        return {str(row["task_id"]): dict(row) for row in rows}

    def _active_lead_job(self, goal_id: str) -> dict[str, Any] | None:
        with self.team.connection(write=False) as connection:
            row = connection.execute(
                """
                SELECT * FROM cluster_jobs
                WHERE goal_id = ? AND kind = 'lead'
                  AND status IN ('starting', 'running')
                ORDER BY started_at DESC LIMIT 1
                """,
                (goal_id,),
            ).fetchone()
        return dict(row) if row else None

    def _lead_cooldown_active(self, goal_id: str) -> bool:
        with self.team.connection(write=False) as connection:
            row = connection.execute(
                """
                SELECT finished_at FROM cluster_jobs
                WHERE goal_id = ? AND kind = 'lead' AND finished_at IS NOT NULL
                ORDER BY finished_at DESC LIMIT 1
                """,
                (goal_id,),
            ).fetchone()
        if not row:
            return False
        finished = _parse_iso(row["finished_at"])
        return bool(
            finished
            and (datetime.now(UTC) - finished).total_seconds()
            < self.lead_cooldown_seconds
        )

    def _retry_allowed(self, task_id: str, goal: dict[str, Any]) -> bool:
        with self.team.connection(write=False) as connection:
            row = connection.execute(
                """
                SELECT attempt, finished_at FROM cluster_jobs
                WHERE task_id = ? AND kind = 'worker'
                ORDER BY attempt DESC, started_at DESC LIMIT 1
                """,
                (task_id,),
            ).fetchone()
        if not row or int(row["attempt"]) < int(goal["max_worker_attempts"]):
            return True
        return False

    def _next_attempt(self, goal_id: str, task_id: str | None, kind: str) -> int:
        with self.team.connection(write=False) as connection:
            if task_id:
                row = connection.execute(
                    """
                    SELECT COALESCE(MAX(attempt), 0) + 1 AS next_attempt
                    FROM cluster_jobs WHERE task_id = ? AND kind = 'worker'
                    """,
                    (task_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT COALESCE(MAX(attempt), 0) + 1 AS next_attempt
                    FROM cluster_jobs WHERE goal_id = ? AND kind = 'lead'
                    """,
                    (goal_id,),
                ).fetchone()
        return int(row["next_attempt"])

    @staticmethod
    def _new_id(prefix: str) -> str:
        return (
            datetime.now(UTC).strftime(f"{prefix}-%Y%m%d-")
            + secrets.token_hex(3).upper()
        )

    @staticmethod
    def _event(
        connection: Any,
        goal_id: str,
        event_type: str,
        actor: str,
        data: dict[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO cluster_events(goal_id, event_type, actor, data_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                goal_id,
                event_type,
                actor,
                json.dumps(data, ensure_ascii=False, sort_keys=True),
                utc_now(),
            ),
        )

    @staticmethod
    def _goal_events(
        connection: Any,
        goal_id: str,
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT event_id, event_type, actor, data_json, created_at
            FROM cluster_events WHERE goal_id = ?
            ORDER BY event_id DESC LIMIT ?
            """,
            (goal_id, max(1, min(limit, 500))),
        ).fetchall()
        result = []
        for row in reversed(rows):
            item = dict(row)
            try:
                item["data"] = json.loads(item.pop("data_json"))
            except (TypeError, ValueError):
                item["data"] = {}
                item.pop("data_json", None)
            result.append(item)
        return result


def _default_codex_command() -> list[str]:
    configured = os.environ.get("CODEX_TEAM_CODEX_COMMAND", "").strip()
    if configured:
        return [configured]
    executable = shutil.which("codex.exe") or shutil.which("codex")
    if not executable:
        known = Path.home() / "AppData/Local/Programs/OpenAI/Codex/bin/codex.exe"
        executable = str(known) if known.is_file() else "codex"
    return [str(executable)]


def _redact_sensitive_log(text: str) -> str:
    return _CLAIM_TOKEN_RE.sub(
        lambda match: (
            match.group("prefix")
            + "[REDACTED]"
            + match.group("suffix")
        ),
        text,
    )


def _redact_sensitive_file(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        original = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    redacted = _redact_sensitive_log(original)
    if redacted == original:
        return False
    temporary = path.with_name(path.name + ".redacted.tmp")
    temporary.write_text(redacted, encoding="utf-8")
    temporary.replace(path)
    return True


def _parse_codex_log(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "thread_id": None,
        "completed": False,
        "error": "",
    }
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                if not line.startswith("{"):
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                event_type = item.get("type")
                if event_type == "thread.started":
                    result["thread_id"] = item.get("thread_id")
                elif event_type == "turn.completed":
                    result["completed"] = True
                elif event_type in {"turn.failed", "error"}:
                    error = item.get("error") or item.get("message")
                    if isinstance(error, dict):
                        error = error.get("message") or json.dumps(error, ensure_ascii=False)
                    result["error"] = str(error or event_type)
    except OSError:
        result["error"] = "log unreadable"
    return result


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def daemon_paths(settings: Settings) -> tuple[Path, Path, Path]:
    base = settings.logs_dir
    return (
        base / "clusterd.lock",
        base / "clusterd.json",
        base / "clusterd.log",
    )


def daemon_status(settings: Settings) -> dict[str, Any]:
    lock_path, state_path, log_path = daemon_paths(settings)
    pid = 0
    try:
        pid = int(json.loads(lock_path.read_text(encoding="utf-8")).get("pid", 0))
    except (OSError, ValueError, TypeError):
        pass
    state: dict[str, Any] = {}
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        pass
    return {
        "running": bool(pid and pid_is_running(pid)),
        "pid": pid or None,
        "lock_path": str(lock_path),
        "state_path": str(state_path),
        "log_path": str(log_path),
        "state": state,
    }


def ensure_daemon(settings: Settings) -> dict[str, Any]:
    current = daemon_status(settings)
    if current["running"]:
        return current
    lock_path, state_path, log_path = daemon_paths(settings)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    package_root = settings.project_root / "src"
    environment = os.environ.copy()
    previous_pythonpath = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = (
        str(package_root)
        if not previous_pythonpath
        else os.pathsep.join([str(package_root), previous_pythonpath])
    )
    environment["PYTHONUTF8"] = "1"
    creationflags = (
        getattr(subprocess, "CREATE_NO_WINDOW", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    )
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(f"\n[{utc_now()}] starting cluster daemon\n")
        stream.flush()
        process = subprocess.Popen(
            [
                sys.executable,
                "-u",
                "-m",
                "codex_team.clusterd",
                "--interval",
                "5",
            ],
            cwd=str(settings.project_root),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            close_fds=True,
        )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        current = daemon_status(settings)
        if current["running"]:
            return current
        if process.poll() is not None:
            break
        time.sleep(0.1)
    raise ClusterError(f"cluster daemon failed to start; see {log_path}")


def stop_daemon(settings: Settings) -> dict[str, Any]:
    current = daemon_status(settings)
    pid = int(current.get("pid") or 0)
    if pid and pid_is_running(pid):
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
    lock_path, _, _ = daemon_paths(settings)
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass
    return {"ok": True, "stopped_pid": pid or None}
