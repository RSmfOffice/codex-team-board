from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from .agent_pool import AgentPool, AgentPoolError
from .app_server import AppServerEvents, AppServerRuntime
from .config import Settings
from .db import connect, connect_readonly, utc_now
from .processes import pid_is_running
from .redaction import redact_sensitive_text
from .scheduling import (
    DEFAULT_GLOBAL_JOB_CAPACITY,
    DEFAULT_GOAL_WORKER_CAPACITY,
    DEFAULT_HEARTBEAT_CRITICAL_SECONDS,
    DEFAULT_HEARTBEAT_WARNING_SECONDS,
    DEFAULT_LEAD_TIMEOUT_SECONDS,
    DEFAULT_WORKER_TIMEOUT_SECONDS,
    MAX_GLOBAL_JOB_CAPACITY,
    MAX_GOAL_WORKER_CAPACITY,
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
RUNTIME_MODES = frozenset({"exec", "app_server"})
LEAD_DEVELOPER_INSTRUCTIONS = """You are the managed Lead for an automated
Codex work cluster. Do not spawn subagents; the deterministic teamd dispatcher
launches Workers. You may use shell tools and codexTeamBoard MCP tools.

Use the board returned in each turn as the current source of truth. If there are
no tasks, decompose the objective into small, non-overlapping tasks with a goal
ID, acceptance criteria, dependencies, and owner roles. Use kind write for code
changes and team_worker; use read for team_explorer.

For review tasks, inspect the diff and team_get_task_events, run tests, merge the
task branch into its recorded target branch, then call team_review_task with the
real merge_commit. Do not approve failed or dirty work.

For blocked tasks, resolve the blocker with a concrete action; unblock, cancel,
or create a replacement task. Never loop without progress. In supervised mode,
do not merge or mark review tasks done; report that human review is required.
Do not mark a task done without a real merge commit and passing integration
tests. Return a concise progress summary and the next concrete board state.
"""


def _stable_developer_instructions(
    goal: Mapping[str, Any],
    role: str,
    instructions: str,
) -> str:
    """Prefix role instructions with stable, cache-friendly thread identity."""

    workstream = str(goal.get("workstream_key") or "").strip()
    if not workstream or workstream == str(goal.get("goal_id") or ""):
        workstream = "default (goal-scoped)"
    prefix = f"""## Stable Team Context

- Project ID: `{goal.get("project_id") or "unknown"}`
- Project root: `{goal.get("root_path") or "unknown"}`
- Role: `{role}`
- Workstream: `{workstream}`

This identity is stable for the Agent thread. The current goal, assignment,
board state, and task-specific instructions arrive in the turn input.
"""
    return f"{prefix.rstrip()}\n\n{instructions.strip()}"


def _runtime_mode(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    if not normalized:
        normalized = "exec"
    if normalized not in RUNTIME_MODES:
        raise ValueError("CODEX_TEAM_AGENT_RUNTIME must be exec or app_server")
    return normalized


def _optional_text(value: str | None, name: str, *, limit: int = 200) -> str:
    normalized = str(value or "").strip()
    if len(normalized) > limit:
        raise ClusterError(f"{name} must not exceed {limit} characters")
    return normalized


class _ClusterAppServerEvents(AppServerEvents):
    def __init__(self, manager: ClusterManager) -> None:
        self.manager = manager

    def on_thread_status_changed(
        self,
        thread_id: str,
        status: Mapping[str, Any],
    ) -> None:
        self.manager._app_thread_status(thread_id, status)

    def on_turn_started(
        self,
        thread_id: str,
        turn_id: str,
        turn: Mapping[str, Any],
    ) -> None:
        self.manager._app_turn_started(thread_id, turn_id, turn)

    def on_turn_completed(
        self,
        thread_id: str,
        turn_id: str,
        turn: Mapping[str, Any],
    ) -> None:
        self.manager._app_turn_completed(thread_id, turn_id, turn)

    def on_token_usage_updated(
        self,
        thread_id: str,
        turn_id: str,
        usage: Any,
    ) -> None:
        self.manager._app_token_usage(thread_id, turn_id, usage)

    def on_error(
        self,
        thread_id: str,
        turn_id: str,
        error: Mapping[str, Any],
        will_retry: bool,
    ) -> None:
        self.manager._app_error(
            thread_id,
            turn_id,
            error,
            will_retry,
        )

    def on_thread_closed(self, thread_id: str) -> None:
        self.manager._app_thread_closed(thread_id)


class ClusterError(TeamError):
    pass


class _AppServerJobStartError(ClusterError):
    """An App Server job could not start after its one runtime restart."""


class ClusterManager:
    def __init__(
        self,
        settings: Settings,
        *,
        team: TeamService | None = None,
        worktrees: WorktreeManager | None = None,
        agent_command: list[str] | None = None,
        runtime: AppServerRuntime | None = None,
        agent_pool: AgentPool | None = None,
        runtime_mode: str | None = None,
        max_workers: int | None = None,
        max_jobs: int | None = None,
        lead_cooldown_seconds: float = 8.0,
    ) -> None:
        self.settings = settings
        self.agent_pool = agent_pool or AgentPool(settings)
        self.team = team or TeamService(settings, agent_pool=self.agent_pool)
        self.worktrees = worktrees or WorktreeManager(self.team)
        self.agent_command = agent_command or _default_codex_command()
        configured_runtime = os.environ.get("CODEX_TEAM_AGENT_RUNTIME")
        self.agent_runtime = _runtime_mode(
            runtime_mode
            if runtime_mode is not None
            else (
                configured_runtime
                if configured_runtime is not None
                else ("app_server" if runtime is not None else "exec")
            )
        )
        self.model = _optional_text(
            os.environ.get("CODEX_TEAM_MODEL"),
            "CODEX_TEAM_MODEL",
        )
        self.model_provider = _optional_text(
            os.environ.get("CODEX_TEAM_MODEL_PROVIDER"),
            "CODEX_TEAM_MODEL_PROVIDER",
        )
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
        self.max_workers = max(1, min(configured_workers, MAX_GOAL_WORKER_CAPACITY))
        self.max_jobs = max(1, min(configured_jobs, MAX_GLOBAL_JOB_CAPACITY))
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
        self.app_turns: dict[str, dict[str, Any]] = {}
        self.app_thread_jobs: dict[str, str] = {}
        self.app_turn_jobs: dict[str, str] = {}
        self._app_state_lock = threading.RLock()
        self._runtime_fallback_reason = ""
        self.runtime = runtime
        if self.runtime is None and self.agent_runtime == "app_server":
            self.runtime = AppServerRuntime(
                [*self.agent_command, "app-server", "--listen", "stdio://"],
                stderr_log_path=self.settings.logs_dir / "app-server.stderr.log",
                event_handler=_ClusterAppServerEvents(self),
                env={
                    "CODEX_TEAM_HOME": str(self.settings.home),
                    "PYTHONUTF8": "1",
                },
            )
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
        workstream_key: str | None = None,
        continuity_reason: str = "",
    ) -> dict[str, Any]:
        objective = objective.strip()
        if not objective:
            raise ClusterError("objective must not be empty")
        if autonomy not in {"auto", "supervised"}:
            raise ClusterError("autonomy must be auto or supervised")
        requested_workstream = _optional_text(workstream_key, "workstream_key")
        continuity_reason = _optional_text(
            continuity_reason,
            "continuity_reason",
            limit=2000,
        )
        project = self.team.init_project(repo)
        goal_id = self._new_id("G")
        effective_workstream = requested_workstream or goal_id
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
                    max_worker_attempts, max_lead_attempts, workstream_key,
                    continuity_reason, created_at, updated_at
                ) VALUES (?, ?, ?, 'planning', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    goal_id,
                    project["project_id"],
                    objective,
                    autonomy,
                    max(1, min(int(max_worker_attempts), 10)),
                    max(1, min(int(max_lead_attempts), 20)),
                    effective_workstream,
                    continuity_reason,
                    now,
                    now,
                ),
            )
            self._event(
                connection,
                goal_id,
                "goal_created",
                "teamd",
                {
                    "objective": objective,
                    "workstream_key": effective_workstream,
                    "continuity_reason": continuity_reason,
                },
            )
        return self.get_goal(goal_id)

    def ensure_goal(
        self,
        repo: str | Path,
        *,
        objective: str,
        autonomy: str = "auto",
        max_worker_attempts: int = 3,
        max_lead_attempts: int = 5,
        workstream_key: str | None = None,
        continuity_reason: str = "",
    ) -> dict[str, Any]:
        """Return a project's unfinished goal, creating one when none exists."""
        objective = objective.strip()
        if not objective:
            raise ClusterError("objective must not be empty")
        if autonomy not in {"auto", "supervised"}:
            raise ClusterError("autonomy must be auto or supervised")
        requested_workstream = _optional_text(workstream_key, "workstream_key")
        continuity_reason = _optional_text(
            continuity_reason,
            "continuity_reason",
            limit=2000,
        )
        project = self.team.init_project(repo)
        now = utc_now()
        created = False
        with self.team.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                """
                SELECT goal_id, status, objective, workstream_key, continuity_reason
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
                existing_workstream = str(active["workstream_key"] or "").strip()
                effective_workstream = (
                    existing_workstream
                    if existing_workstream and existing_workstream != goal_id
                    else requested_workstream
                    or existing_workstream
                    or goal_id
                )
                effective_reason = (
                    str(active["continuity_reason"] or "").strip()
                    or continuity_reason
                )
                connection.execute(
                    """
                    UPDATE cluster_goals
                    SET workstream_key = ?, continuity_reason = ?, updated_at = ?
                    WHERE goal_id = ?
                    """,
                    (
                        effective_workstream,
                        effective_reason,
                        utc_now(),
                        goal_id,
                    ),
                )
                self._event(
                    connection,
                    goal_id,
                    "goal_reused",
                    "lead",
                    {
                        "status": active["status"],
                        "existing_objective": active["objective"],
                        "requested_objective": objective,
                        "workstream_key": effective_workstream,
                        "continuity_reason": effective_reason,
                    },
                )
            else:
                goal_id = self._new_id("G")
                created = True
                effective_workstream = requested_workstream or goal_id
                connection.execute(
                    """
                    INSERT INTO cluster_goals(
                        goal_id, project_id, objective, status, autonomy,
                        max_worker_attempts, max_lead_attempts, workstream_key,
                        continuity_reason, created_at, updated_at
                    ) VALUES (?, ?, ?, 'planning', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        goal_id,
                        project["project_id"],
                        objective,
                        autonomy,
                        max(1, min(int(max_worker_attempts), 10)),
                        max(1, min(int(max_lead_attempts), 20)),
                        effective_workstream,
                        continuity_reason,
                        now,
                        now,
                    ),
                )
                self._event(
                    connection,
                    goal_id,
                    "goal_created",
                    "lead",
                    {
                        "objective": objective,
                        "via": "ensure_goal",
                        "workstream_key": effective_workstream,
                        "continuity_reason": continuity_reason,
                    },
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
                    SELECT job_id, pid, role, task_id, agent_id, thread_id,
                           turn_id, runtime
                    FROM cluster_jobs
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
            if job.get("runtime") == "app_server":
                self._interrupt_app_turn(job, reason)
                continue
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
        for job in active_jobs:
            if job.get("runtime") != "app_server":
                continue
            with self._app_state_lock:
                state = dict(self.app_turns.get(job["job_id"], {}))
            self._finalize_app_agent(
                job,
                state,
                success=False,
                agent_already_failed=False,
                needs_reset=False,
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

    def set_goal_workstream(
        self,
        goal_id: str,
        *,
        workstream_key: str | None = None,
        continuity_reason: str | None = None,
        actor: str = "human",
    ) -> dict[str, Any]:
        normalized_key = _optional_text(workstream_key, "workstream_key")
        normalized_reason = (
            None
            if continuity_reason is None
            else _optional_text(
                continuity_reason,
                "continuity_reason",
                limit=2000,
            )
        )
        if not normalized_key and normalized_reason is None:
            raise ClusterError("workstream_key or continuity_reason is required")
        with self.team.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM cluster_goals WHERE goal_id = ?",
                (goal_id,),
            ).fetchone()
            if row is None:
                raise ClusterError(f"cluster goal not found: {goal_id}")
            if row["archived_at"]:
                raise ClusterError("archived goal cannot change workstream")
            effective_key = normalized_key or str(row["workstream_key"] or goal_id)
            effective_reason = (
                normalized_reason
                if normalized_reason is not None
                else str(row["continuity_reason"] or "")
            )
            connection.execute(
                """
                UPDATE cluster_goals
                SET workstream_key = ?, continuity_reason = ?, updated_at = ?
                WHERE goal_id = ?
                """,
                (effective_key, effective_reason, utc_now(), goal_id),
            )
            self._event(
                connection,
                goal_id,
                "goal_workstream_updated",
                actor,
                {
                    "workstream_key": effective_key,
                    "continuity_reason": effective_reason,
                },
            )
        return self.get_goal(goal_id)

    def list_agents(
        self,
        project_id: str | None = None,
        *,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        return self.agent_pool.list_agents(
            project_id,
            include_archived=include_archived,
        )

    def reset_agent(
        self,
        agent_id: str,
        *,
        actor: str = "human",
        reason: str = "manual reset requested through cluster",
    ) -> dict[str, Any]:
        try:
            return self.agent_pool.request_reset(
                agent_id,
                reason,
                actor,
            )
        except AgentPoolError as exc:
            raise ClusterError(str(exc)) from exc

    def close(self) -> None:
        if self.runtime is not None:
            self.runtime.stop()

    def tick(self) -> dict[str, Any]:
        self._apply_agent_reset_requests()
        self._sync_project_agent_archive()
        self._reconcile_jobs()
        self._check_worker_health()
        goals = self.list_goals(["planning", "active"])
        schedulable: list[dict[str, Any]] = []
        for goal in goals:
            if self._prepare_goal(goal):
                schedulable.append(goal)
        scheduled = self._schedule_goals(schedulable)
        return {"tick_at": utc_now(), "scheduled": scheduled, "goals": len(goals)}

    def _sync_project_agent_archive(self) -> None:
        with self.team.connection(write=False) as connection:
            projects = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT p.project_id, p.archived_at
                    FROM projects p
                    WHERE EXISTS (
                        SELECT 1
                        FROM cluster_agents a
                        WHERE a.project_id = p.project_id
                          AND (
                            (p.archived_at IS NOT NULL AND a.archived_at IS NULL)
                            OR
                            (p.archived_at IS NULL AND a.archived_at IS NOT NULL)
                          )
                    )
                    ORDER BY p.project_id
                    """
                ).fetchall()
            ]
        for project in projects:
            try:
                self.agent_pool.archive_project(
                    str(project["project_id"]),
                    project["archived_at"] is not None,
                )
            except Exception:
                # A live Agent can be cleaned up after the current job releases.
                continue

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
        assignment = f"""# Automated assignment

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
            developer_instructions=role_data["prompt"],
            turn_input=assignment,
            continuity_mode=str(task.get("continuity_mode") or "auto"),
            sandbox_mode=self._worker_sandbox_mode(task),
        )

    @staticmethod
    def _worker_sandbox_mode(task: dict[str, Any]) -> str:
        return (
            "danger-full-access"
            if task.get("kind") == "write"
            else "workspace-write"
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
        turn_input = f"""Goal:
{goal["objective"]}

Current board:
```json
{json.dumps(board, ensure_ascii=False, indent=2)}
```

Use goal_id = "{goal["goal_id"]}" when creating tasks for this turn.
"""
        return self._start_job(
            goal,
            task_id=None,
            kind="lead",
            role="lead",
            cwd=Path(goal["root_path"]),
            developer_instructions=LEAD_DEVELOPER_INSTRUCTIONS,
            turn_input=turn_input,
            continuity_mode="reuse" if goal.get("lead_thread_id") else "auto",
            resume_thread_id=goal.get("lead_thread_id"),
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
        developer_instructions: str,
        turn_input: str,
        continuity_mode: str = "auto",
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
        selected_runtime = self._select_job_runtime(
            goal,
            task_id,
            kind,
            job_id=job_id,
        )
        runtime_generation = (
            self._current_runtime_generation()
            if selected_runtime == "app_server"
            else None
        )
        runtime_error = (
            self._runtime_fallback_reason
            if self.agent_runtime == "app_server" and selected_runtime == "exec"
            else ""
        )
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
                    attempt, log_path, output_path, runtime,
                    runtime_generation, error, started_at
                ) VALUES (?, ?, ?, ?, ?, 'starting', ?, ?, ?, ?, ?, ?, ?)
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
                    selected_runtime,
                    runtime_generation,
                    runtime_error,
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
                    "runtime": selected_runtime,
                    "runtime_generation": runtime_generation,
                    "resume_thread_id": resume_thread_id,
                    "sandbox_mode": sandbox_mode,
                },
            )
        if selected_runtime == "app_server":
            try:
                return self._start_app_server_job(
                    goal,
                    job_id=job_id,
                    task_id=task_id,
                    kind=kind,
                    role=role,
                    cwd=cwd,
                    log_path=log_path,
                    output_path=output_path,
                    attempt=attempt,
                    developer_instructions=developer_instructions,
                    turn_input=turn_input,
                    continuity_mode=continuity_mode,
                    sandbox_mode=sandbox_mode,
                )
            except _AppServerJobStartError as exc:
                self._fallback_job_start_to_exec(
                    goal,
                    job_id=job_id,
                    task_id=task_id,
                    kind=kind,
                    role=role,
                    log_path=log_path,
                    runtime_error=str(exc),
                )
                return self._start_exec_job(
                    goal,
                    job_id=job_id,
                    task_id=task_id,
                    kind=kind,
                    role=role,
                    cwd=cwd,
                    log_path=log_path,
                    output_path=output_path,
                    attempt=attempt,
                    developer_instructions=developer_instructions,
                    turn_input=turn_input,
                    resume_thread_id=None,
                    sandbox_mode=sandbox_mode,
                )
        return self._start_exec_job(
            goal,
            job_id=job_id,
            task_id=task_id,
            kind=kind,
            role=role,
            cwd=cwd,
            log_path=log_path,
            output_path=output_path,
            attempt=attempt,
            developer_instructions=developer_instructions,
            turn_input=turn_input,
            resume_thread_id=resume_thread_id,
            sandbox_mode=sandbox_mode,
        )

    def _start_exec_job(
        self,
        goal: dict[str, Any],
        *,
        job_id: str,
        task_id: str | None,
        kind: str,
        role: str,
        cwd: Path,
        log_path: Path,
        output_path: Path,
        attempt: int,
        developer_instructions: str,
        turn_input: str,
        resume_thread_id: str | None,
        sandbox_mode: str,
    ) -> dict[str, Any]:
        prompt = f"{developer_instructions.rstrip()}\n\n{turn_input.lstrip()}"
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
            safe_error = self._redact_error(str(exc))
            with self.team.connection() as connection:
                connection.execute(
                    """
                    UPDATE cluster_jobs
                    SET status = 'failed', finished_at = ?, error = ?
                    WHERE job_id = ?
                    """,
                    (utc_now(), safe_error, job_id),
                )
                self._event(
                    connection,
                    goal["goal_id"],
                    "job_start_failed",
                    "teamd",
                    {"job_id": job_id, "error": safe_error},
                )
            raise ClusterError(
                f"could not start {kind} process: {safe_error}"
            ) from exc
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
            "runtime": "exec",
            "attempt": attempt,
            "log_path": str(log_path),
        }

    def _fallback_job_start_to_exec(
        self,
        goal: dict[str, Any],
        *,
        job_id: str,
        task_id: str | None,
        kind: str,
        role: str,
        log_path: Path,
        runtime_error: str,
    ) -> None:
        runtime_error = self._redact_error(runtime_error)
        with self.team.connection() as connection:
            job = connection.execute(
                "SELECT * FROM cluster_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            previous_thread_id = job["thread_id"] if job is not None else None
            previous_agent_id = job["agent_id"] if job is not None else None
            connection.execute(
                """
                UPDATE cluster_jobs
                SET runtime = 'exec', status = 'starting', pid = NULL,
                    thread_id = NULL, agent_id = NULL, turn_id = NULL,
                    runtime_generation = NULL, error = ?
                WHERE job_id = ?
                """,
                (runtime_error, job_id),
            )
            if kind == "lead":
                connection.execute(
                    """
                    UPDATE cluster_goals
                    SET lead_thread_id = NULL,
                        lead_generation = lead_generation + 1,
                        last_error = ?, updated_at = ?
                    WHERE goal_id = ?
                    """,
                    (runtime_error, utc_now(), goal["goal_id"]),
                )
            payload = {
                "job_id": job_id,
                "task_id": task_id,
                "kind": kind,
                "role": role,
                "configured_runtime": "app_server",
                "fallback_runtime": "exec",
                "previous_thread_id": previous_thread_id,
                "previous_agent_id": previous_agent_id,
                "error": runtime_error,
                "scope": "same_job",
            }
            self._event(
                connection,
                goal["goal_id"],
                "agent_runtime_fallback",
                "teamd",
                payload,
            )
        self._append_app_event(
            log_path,
            {"event": "agent_runtime_fallback", **payload},
        )

    def _select_job_runtime(
        self,
        goal: dict[str, Any],
        task_id: str | None,
        kind: str,
        *,
        job_id: str | None = None,
    ) -> str:
        if self.agent_runtime != "app_server":
            return "exec"
        reason = self._ensure_app_server_runtime(job_id)
        if not reason and self._current_runtime_generation() is None:
            reason = (
                "App Server runtime is healthy but did not report a generation"
            )
            self._runtime_fallback_reason = reason
        if not reason:
            return "app_server"
        with self.team.connection() as connection:
            self._event(
                connection,
                goal["goal_id"],
                "agent_runtime_fallback",
                "teamd",
                {
                    "configured_runtime": "app_server",
                    "fallback_runtime": "exec",
                    "job_id": job_id,
                    "scope": "same_job",
                    "task_id": task_id,
                    "kind": kind,
                    "error": reason,
                },
            )
        return "exec"

    def _ensure_app_server_runtime(self, job_id: str | None = None) -> str:
        if self._runtime_fallback_reason:
            return self._runtime_fallback_reason
        if self.runtime is None:
            self._runtime_fallback_reason = "App Server runtime is not configured"
            return self._runtime_fallback_reason
        runtime = self.runtime
        if self._health_is_healthy(self._runtime_health(runtime)):
            return ""
        # AppServerRuntime.start() repairs a previously running process. Mark
        # turns on that process lost before it is replaced.
        try:
            self._lose_other_app_server_jobs(
                current_job_id=job_id,
                reason="App Server runtime was unavailable before restart",
            )
        except Exception as exc:
            self._runtime_fallback_reason = self._redact_error(
                f"App Server job isolation failed: {exc}"
            )
            return self._runtime_fallback_reason
        first_failure_error: Exception
        try:
            health = runtime.start()
            if self._health_is_healthy(health):
                return ""
            first_failure_error = ClusterError(
                "App Server start returned an unhealthy state"
            )
        except Exception as exc:
            first_failure_error = exc
        try:
            health = self._restart_shared_app_server(
                runtime,
                current_job_id=job_id,
                reason="App Server initialization failed",
            )
            if not self._health_is_healthy(health):
                raise ClusterError(
                    "App Server restart returned an unhealthy state"
                )
            return ""
        except Exception as second_error:
            self._runtime_fallback_reason = self._app_server_failure(
                "initialization failed: "
                f"{self._redact_error(str(first_failure_error))}; "
                f"restart failed: {self._redact_error(str(second_error))}",
                runtime,
            )
            return self._runtime_fallback_reason

    @staticmethod
    def _health_is_healthy(health: Mapping[str, Any] | Any) -> bool:
        if not isinstance(health, Mapping):
            return False
        return bool(health.get("healthy", health.get("running", False)))

    def _runtime_health(self, runtime: Any) -> Mapping[str, Any]:
        try:
            health = runtime.health()
        except Exception:
            return {}
        return health if isinstance(health, Mapping) else {}

    def _current_runtime_generation(
        self,
        runtime: Any | None = None,
    ) -> int | None:
        selected_runtime = self.runtime if runtime is None else runtime
        if selected_runtime is None:
            return None
        health = self._runtime_health(selected_runtime)
        value: Any = health.get("generation")
        if value is None:
            try:
                value = selected_runtime.generation
            except Exception:
                return None
        return self._runtime_generation_value(value)

    @staticmethod
    def _runtime_generation_value(value: Any) -> int | None:
        try:
            generation = int(value)
        except (TypeError, ValueError):
            return None
        return generation if generation >= 0 else None

    def _record_app_turn_generation(
        self,
        job_id: str,
        generation: int | None,
    ) -> None:
        if not job_id or generation is None:
            return
        with self._app_state_lock:
            state = self.app_turns.get(job_id)
            if state is not None:
                state["runtime_generation"] = generation
        with self.team.connection() as connection:
            connection.execute(
                """
                UPDATE cluster_jobs
                SET runtime_generation = ?
                WHERE job_id = ?
                """,
                (generation, job_id),
            )

    @staticmethod
    def _redact_error(message: str) -> str:
        return _redact_sensitive_log(str(message))

    def _app_server_stderr_excerpt(
        self,
        runtime: Any,
        *,
        max_lines: int = 20,
        max_chars: int = 4000,
    ) -> str:
        try:
            lines = runtime.stderr_lines
        except Exception:
            return ""
        if isinstance(lines, str):
            lines = lines.splitlines()
        elif callable(lines):
            try:
                lines = lines()
            except Exception:
                return ""
        try:
            recent = [str(line) for line in list(lines)[-max_lines:]]
        except (TypeError, ValueError):
            return ""
        text = _redact_sensitive_log("\n".join(recent)).strip()
        if len(text) > max_chars:
            text = text[-max_chars:]
        return text

    def _app_server_failure(self, message: str, runtime: Any) -> str:
        message = self._redact_error(message)
        stderr = self._app_server_stderr_excerpt(runtime)
        if not stderr:
            return message
        return f"{message}; recent stderr:\n{stderr}"

    def _lose_other_app_server_jobs(
        self,
        *,
        current_job_id: str | None,
        reason: str,
    ) -> None:
        with self.team.connection(write=False) as connection:
            rows = connection.execute(
                """
                SELECT j.*, g.autonomy, g.max_worker_attempts,
                       g.max_lead_attempts
                FROM cluster_jobs j
                JOIN cluster_goals g ON g.goal_id = j.goal_id
                WHERE j.runtime = 'app_server'
                  AND j.status IN ('starting', 'running')
                  AND j.job_id != ?
                ORDER BY j.started_at
                """,
                (current_job_id or "",),
            ).fetchall()
        for row in rows:
            job = dict(row)
            self._finish_lost_app_server_job(
                job,
                reason=reason,
            )

    def _restart_shared_app_server(
        self,
        runtime: Any,
        *,
        current_job_id: str | None,
        reason: str,
    ) -> Mapping[str, Any]:
        # The replacement process invalidates every other live turn. Persist
        # their recovery before restart so a healthy replacement cannot hide
        # stale jobs until their timeout.
        self._lose_other_app_server_jobs(
            current_job_id=current_job_id,
            reason=reason,
        )
        try:
            health = runtime.restart()
        except Exception as exc:
            try:
                self._lose_other_app_server_jobs(
                    current_job_id=current_job_id,
                    reason=f"{reason} after failed restart",
                )
            except Exception as isolation_error:
                exc = ClusterError(
                    f"{exc}; post-restart job isolation failed: "
                    f"{isolation_error}"
                )
            raise ClusterError(
                self._app_server_failure(
                    f"restart failed: {self._redact_error(str(exc))}",
                    runtime,
                )
            ) from exc
        self._record_app_turn_generation(
            current_job_id or "",
            self._current_runtime_generation(runtime),
        )
        self._lose_other_app_server_jobs(
            current_job_id=current_job_id,
            reason=f"{reason} after restart",
        )
        return health if isinstance(health, Mapping) else {}

    def _start_fresh_app_thread(
        self,
        runtime: Any,
        cwd: Path,
        *,
        developer_instructions: str,
        job_id: str,
    ) -> str:
        def start() -> str:
            return runtime.start_thread(
                cwd,
                developer_instructions=developer_instructions,
                model=self.model or None,
                model_provider=self.model_provider or None,
                service_name="codex-team-cluster",
            )

        try:
            return start()
        except Exception as first_error:
            first_failure = self._app_server_failure(
                f"thread/start failed: "
                f"{self._redact_error(str(first_error))}",
                runtime,
            )
            if self._health_is_healthy(self._runtime_health(runtime)):
                raise _AppServerJobStartError(
                    f"{first_failure}; process remained healthy, so the shared "
                    "App Server was not restarted"
                ) from first_error
            try:
                self._restart_shared_app_server(
                    runtime,
                    current_job_id=job_id,
                    reason="thread/start failed on an unhealthy App Server",
                )
            except Exception as restart_error:
                raise _AppServerJobStartError(
                    f"{first_failure}; "
                    f"restart failed: "
                    f"{self._redact_error(str(restart_error))}"
                ) from restart_error
            try:
                return start()
            except Exception as second_error:
                raise _AppServerJobStartError(
                    self._app_server_failure(
                        f"{first_failure}; retry after restart failed: "
                        f"{self._redact_error(str(second_error))}",
                        runtime,
                    )
                ) from second_error

    def _start_app_turn_with_restart(
        self,
        runtime: Any,
        thread_id: str,
        turn_input: str,
        *,
        cwd: Path,
        sandbox_mode: str,
        job_id: str,
    ) -> dict[str, Any]:
        def start() -> dict[str, Any]:
            return runtime.start_turn(
                thread_id,
                turn_input,
                cwd=cwd,
                sandbox_mode=sandbox_mode,
                model=self.model or None,
            )

        try:
            return start()
        except Exception as first_error:
            first_failure = self._app_server_failure(
                f"turn/start failed: "
                f"{self._redact_error(str(first_error))}",
                runtime,
            )
            if self._health_is_healthy(self._runtime_health(runtime)):
                raise _AppServerJobStartError(
                    f"{first_failure}; process remained healthy, so the shared "
                    "App Server was not restarted"
                ) from first_error
            try:
                self._restart_shared_app_server(
                    runtime,
                    current_job_id=job_id,
                    reason="turn/start failed on an unhealthy App Server",
                )
            except Exception as restart_error:
                raise _AppServerJobStartError(
                    f"{first_failure}; "
                    f"restart failed: "
                    f"{self._redact_error(str(restart_error))}"
                ) from restart_error
            try:
                restored_thread_id = runtime.resume_thread(thread_id)
            except Exception as resume_error:
                raise _AppServerJobStartError(
                    self._app_server_failure(
                        f"{first_failure}; thread reload after restart failed: "
                        f"{self._redact_error(str(resume_error))}",
                        runtime,
                    )
                ) from resume_error
            try:
                return runtime.start_turn(
                    restored_thread_id,
                    turn_input,
                    cwd=cwd,
                    sandbox_mode=sandbox_mode,
                    model=self.model or None,
                )
            except Exception as second_error:
                raise _AppServerJobStartError(
                    self._app_server_failure(
                        f"{first_failure}; retry after restart failed: "
                        f"{self._redact_error(str(second_error))}",
                        runtime,
                    )
                ) from second_error

    def _start_app_server_job(
        self,
        goal: dict[str, Any],
        *,
        job_id: str,
        task_id: str | None,
        kind: str,
        role: str,
        cwd: Path,
        log_path: Path,
        output_path: Path,
        attempt: int,
        developer_instructions: str,
        turn_input: str,
        continuity_mode: str,
        sandbox_mode: str,
    ) -> dict[str, Any]:
        runtime = self.runtime
        if runtime is None:
            raise ClusterError("App Server runtime is not configured")
        developer_instructions = _stable_developer_instructions(
            goal,
            role,
            developer_instructions,
        )
        workstream_key = str(goal.get("workstream_key") or goal["goal_id"])
        task_kind = "read" if kind == "lead" else str(
            self.team.get_task(task_id)["kind"]
        )
        agent: dict[str, Any] | None = None
        thread_id = ""
        try:
            agent = self.agent_pool.select_agent(
                str(goal["project_id"]),
                role,
                workstream_key,
                continuity_mode,
                task_kind,
                self.model,
                self.model_provider,
            )
            if agent is not None:
                try:
                    thread_id = runtime.resume_thread(str(agent["thread_id"]))
                except Exception as exc:
                    reason = self._app_server_failure(
                        f"thread resume failed: "
                        f"{self._redact_error(str(exc))}",
                        runtime,
                    )
                    if not self._health_is_healthy(
                        self._runtime_health(runtime)
                    ):
                        try:
                            self._restart_shared_app_server(
                                runtime,
                                current_job_id=job_id,
                                reason=(
                                    "thread resume failed on an unhealthy "
                                    "App Server"
                                ),
                            )
                            reason += (
                                "; shared App Server restarted with a fresh "
                                "thread"
                            )
                        except Exception as restart_error:
                            raise _AppServerJobStartError(
                                f"{reason}; restart failed: "
                                f"{self._redact_error(str(restart_error))}"
                            ) from restart_error
                    else:
                        reason += (
                            "; process remained healthy, so the shared "
                            "App Server was not restarted"
                        )
                    try:
                        self.agent_pool.mark_failed(
                            str(agent["agent_id"]),
                            reason,
                            needs_reset=True,
                        )
                    except Exception:
                        pass
                    try:
                        runtime.unsubscribe_thread(str(agent["thread_id"]))
                    except Exception:
                        pass
                    self._append_app_event(
                        log_path,
                        {
                            "event": "agent_thread_resume_failed",
                            "job_id": job_id,
                            "agent_id": agent["agent_id"],
                            "thread_id": agent["thread_id"],
                            "error": reason,
                            "action": "fresh_thread",
                        },
                    )
                    agent = None
            if agent is None:
                # goal.lead_thread_id is migration-only display/exec state. It
                # must not bypass AgentPool identity, status, or reset checks.
                thread_id = self._start_fresh_app_thread(
                    runtime,
                    cwd,
                    developer_instructions=developer_instructions,
                    job_id=job_id,
                )
                agent = self.agent_pool.register_thread(
                    str(goal["project_id"]),
                    role,
                    workstream_key,
                    thread_id,
                    self.model,
                    self.model_provider,
                    f"{role} {workstream_key}",
                )
            agent = self.agent_pool.mark_busy(
                str(agent["agent_id"]),
                str(goal["goal_id"]),
                task_id,
                job_id,
                None,
            )
            runtime_generation = self._current_runtime_generation(runtime)
            if runtime_generation is None:
                raise _AppServerJobStartError(
                    "App Server runtime did not report an active generation"
                )
            with self._app_state_lock:
                self.app_turns[job_id] = {
                    "thread_id": thread_id,
                    "turn_id": None,
                    "runtime_generation": runtime_generation,
                    "status": "starting",
                    "usage": {},
                    "first_usage": {},
                    "error": {},
                    "will_retry": False,
                    "context_window_exceeded": False,
                    "interrupt_requested": False,
                }
                self.app_thread_jobs[thread_id] = job_id
            with self.team.connection() as connection:
                connection.execute(
                    """
                    UPDATE cluster_jobs
                    SET status = 'running', thread_id = ?, agent_id = ?,
                        runtime_generation = ?
                    WHERE job_id = ?
                    """,
                    (
                        thread_id,
                        agent["agent_id"],
                        runtime_generation,
                        job_id,
                    ),
                )
            self._append_app_event(
                log_path,
                {
                    "event": "thread_ready",
                    "job_id": job_id,
                    "thread_id": thread_id,
                    "agent_id": agent["agent_id"],
                    "runtime": "app_server",
                    "runtime_generation": runtime_generation,
                },
            )
            turn = self._start_app_turn_with_restart(
                runtime,
                thread_id,
                turn_input,
                cwd=cwd,
                sandbox_mode=sandbox_mode,
                job_id=job_id,
            )
        except Exception as exc:
            with self._app_state_lock:
                self.app_thread_jobs.pop(thread_id, None)
                self.app_turns.pop(job_id, None)
            if agent is not None:
                try:
                    safe_error = self._redact_error(str(exc))
                    self.agent_pool.mark_failed(
                        str(agent["agent_id"]),
                        f"App Server turn start failed: {safe_error}",
                        needs_reset=True,
                    )
                except Exception:
                    pass
            if thread_id:
                try:
                    runtime.unsubscribe_thread(thread_id)
                except Exception:
                    pass
            if isinstance(exc, _AppServerJobStartError):
                raise
            raise _AppServerJobStartError(
                self._app_server_failure(
                    f"App Server job start failed: "
                    f"{self._redact_error(str(exc))}",
                    runtime,
                )
            ) from exc
        turn_id = turn["turn_id"]
        active_generation = self._current_runtime_generation(runtime)
        with self._app_state_lock:
            state = self.app_turns.setdefault(job_id, {})
            state["thread_id"] = thread_id
            state["turn_id"] = turn_id
            if active_generation is not None:
                state["runtime_generation"] = active_generation
            if state.get("status") not in {"completed", "failed", "interrupted"}:
                state["status"] = turn.get("status") or "inProgress"
            self.app_turn_jobs[turn_id] = job_id
        self.agent_pool.mark_busy(
            str(agent["agent_id"]),
            str(goal["goal_id"]),
            task_id,
            job_id,
            turn_id,
        )
        with self.team.connection() as connection:
            connection.execute(
                """
                UPDATE cluster_jobs
                SET turn_id = ?,
                    runtime_generation = COALESCE(?, runtime_generation)
                WHERE job_id = ?
                """,
                (turn_id, active_generation, job_id),
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
        self._append_app_event(
            log_path,
            {
                "event": "turn_ready",
                "job_id": job_id,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "cwd": str(cwd),
                "sandbox_mode": sandbox_mode,
                "runtime_generation": active_generation,
            },
        )
        output_path.write_text(
            json.dumps(
                {
                    "job_id": job_id,
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "status": "inProgress",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return {
            "job_id": job_id,
            "goal_id": goal["goal_id"],
            "task_id": task_id,
            "kind": kind,
            "role": role,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "agent_id": agent["agent_id"],
            "runtime": "app_server",
            "attempt": attempt,
            "log_path": str(log_path),
        }

    @staticmethod
    def _append_app_event(path: Path, payload: Mapping[str, Any]) -> None:
        with path.open("a", encoding="utf-8") as stream:
            stream.write(
                _redact_sensitive_log(
                    json.dumps(payload, ensure_ascii=False, sort_keys=True)
                )
                + "\n"
            )

    def _app_job_for_turn(self, thread_id: str, turn_id: str) -> str | None:
        with self._app_state_lock:
            job_id = self.app_turn_jobs.get(turn_id)
            if job_id is None:
                job_id = self.app_thread_jobs.get(thread_id)
                if job_id is not None:
                    self.app_turn_jobs[turn_id] = job_id
            return job_id

    def _app_record(
        self,
        job_id: str,
        event: str,
        payload: Mapping[str, Any],
    ) -> None:
        with self.team.connection(write=False) as connection:
            row = connection.execute(
                "SELECT log_path FROM cluster_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            return
        self._append_app_event(
            Path(row["log_path"]),
            {"event": event, "job_id": job_id, **payload},
        )

    def _app_thread_status(
        self,
        thread_id: str,
        status: Mapping[str, Any],
    ) -> None:
        with self._app_state_lock:
            job_id = self.app_thread_jobs.get(thread_id)
        if job_id:
            self._app_record(
                job_id,
                "thread_status",
                {"thread_id": thread_id, "status": dict(status)},
            )

    def _app_turn_started(
        self,
        thread_id: str,
        turn_id: str,
        turn: Mapping[str, Any],
    ) -> None:
        job_id = self._app_job_for_turn(thread_id, turn_id)
        if job_id is None:
            return
        with self._app_state_lock:
            state = self.app_turns.setdefault(job_id, {})
            state["thread_id"] = thread_id
            state["turn_id"] = turn_id
            if state.get("status") not in {"completed", "failed", "interrupted"}:
                state["status"] = str(turn.get("status") or "inProgress")
        self._app_record(
            job_id,
            "turn_started",
            {
                "thread_id": thread_id,
                "turn_id": turn_id,
                "turn": dict(turn),
            },
        )

    def _app_turn_completed(
        self,
        thread_id: str,
        turn_id: str,
        turn: Mapping[str, Any],
    ) -> None:
        job_id = self._app_job_for_turn(thread_id, turn_id)
        if job_id is None:
            return
        with self._app_state_lock:
            state = self.app_turns.setdefault(job_id, {})
            state.update(
                {
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "status": str(turn.get("status") or "failed"),
                    "turn": dict(turn),
                }
            )
        self._app_record(
            job_id,
            "turn_completed",
            {
                "thread_id": thread_id,
                "turn_id": turn_id,
                "turn": dict(turn),
            },
        )

    def _app_token_usage(
        self,
        thread_id: str,
        turn_id: str,
        usage: Mapping[str, Any],
    ) -> None:
        job_id = self._app_job_for_turn(thread_id, turn_id)
        if job_id is None:
            return
        normalized = dict(usage)
        with self._app_state_lock:
            state = self.app_turns.setdefault(job_id, {})
            if not state.get("first_usage"):
                state["first_usage"] = dict(normalized)
            state["usage"] = normalized
        self._app_record(
            job_id,
            "token_usage",
            {
                "thread_id": thread_id,
                "turn_id": turn_id,
                "usage": normalized,
            },
        )

    def _app_error(
        self,
        thread_id: str,
        turn_id: str,
        error: Mapping[str, Any],
        will_retry: bool,
    ) -> None:
        job_id = self._app_job_for_turn(thread_id, turn_id)
        if job_id is None:
            return
        error_text = json.dumps(error, ensure_ascii=False).casefold()
        context_exceeded = any(
            marker in error_text
            for marker in (
                "contextwindowexceeded",
                "context window exceeded",
                "context_length_exceeded",
                "context window has been exceeded",
            )
        )
        with self._app_state_lock:
            state = self.app_turns.setdefault(job_id, {})
            state["error"] = dict(error)
            state["will_retry"] = bool(will_retry)
            state["context_window_exceeded"] = (
                bool(state.get("context_window_exceeded")) or context_exceeded
            )
        self._app_record(
            job_id,
            "turn_error",
            {
                "thread_id": thread_id,
                "turn_id": turn_id,
                "error": dict(error),
                "will_retry": bool(will_retry),
                "context_window_exceeded": context_exceeded,
            },
        )

    def _app_thread_closed(self, thread_id: str) -> None:
        with self._app_state_lock:
            job_id = self.app_thread_jobs.get(thread_id)
        if job_id:
            self._app_record(
                job_id,
                "thread_closed",
                {"thread_id": thread_id},
            )

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

    def _apply_agent_reset_requests(self) -> None:
        with self.team.connection(write=False) as connection:
            pending = connection.execute(
                """
                SELECT 1 FROM cluster_agents
                WHERE reset_requested_at IS NOT NULL
                  AND archived_at IS NULL
                LIMIT 1
                """
            ).fetchone()
        if pending is None:
            return

        def unsubscribe(thread_id: str) -> None:
            if self.runtime is None:
                return
            self.runtime.unsubscribe_thread(thread_id)

        self.agent_pool.apply_reset_requests(unsubscribe)

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
            if job.get("runtime") == "app_server":
                self._reconcile_app_server_job(job)
                continue
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

    def _reconcile_app_server_job(self, job: dict[str, Any]) -> None:
        with self._app_state_lock:
            state = dict(self.app_turns.get(job["job_id"], {}))
        status = str(state.get("status") or "")
        if status == "completed":
            self._finish_job(job, 0)
            return
        if status == "interrupted":
            error = self._app_turn_error(state) or "App Server turn interrupted"
            self._finish_job(job, -15, forced_error=error)
            return
        if not state:
            self._finish_lost_app_server_job(job)
            return
        job_generation = self._runtime_generation_value(
            job.get("runtime_generation")
        )
        state_generation = self._runtime_generation_value(
            state.get("runtime_generation")
        )
        current_generation = self._current_runtime_generation()
        if (
            state_generation is not None
            and job_generation is not None
            and state_generation != job_generation
        ):
            self._finish_lost_app_server_job(
                job,
                reason=(
                    "App Server job generation changed from "
                    f"{job_generation} to {state_generation}"
                ),
            )
            return
        recorded_generation = (
            state_generation
            if state_generation is not None
            else job_generation
        )
        if recorded_generation is None or current_generation is None:
            self._finish_lost_app_server_job(
                job,
                reason=(
                    "App Server runtime generation is unavailable for an "
                    "active job"
                ),
            )
            return
        if recorded_generation != current_generation:
            self._finish_lost_app_server_job(
                job,
                reason=(
                    "App Server runtime generation changed from "
                    f"{recorded_generation} to {current_generation}"
                ),
            )
            return
        if state.get("context_window_exceeded"):
            self._finish_app_server_context_overflow(job, state)
            return
        if status == "failed":
            error = self._app_turn_error(state) or "App Server turn failed"
            self._finish_job(job, 1, forced_error=error)
            return
        if self._job_timed_out(job):
            self._interrupt_app_turn(job, "job timeout")
            self._finish_job(job, -9, forced_error="job timeout")
            return
        if not self._app_runtime_healthy():
            self._finish_lost_app_server_job(job)

    @staticmethod
    def _app_turn_error(state: Mapping[str, Any]) -> str:
        error = state.get("error")
        if isinstance(error, Mapping):
            message = error.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
            detail = error.get("additionalDetails")
            if isinstance(detail, str) and detail.strip():
                return detail.strip()
        turn = state.get("turn")
        if isinstance(turn, Mapping):
            turn_error = turn.get("error")
            if isinstance(turn_error, Mapping):
                message = turn_error.get("message")
                if isinstance(message, str) and message.strip():
                    return message.strip()
        return ""

    def _app_runtime_healthy(self) -> bool:
        if self.runtime is None:
            return False
        try:
            health = self.runtime.health()
        except Exception:
            return False
        return bool(health.get("healthy", health.get("running", False)))

    def _finish_lost_app_server_job(
        self,
        job: dict[str, Any],
        *,
        reason: str = "App Server runtime restarted without the active turn",
    ) -> None:
        reason = self._redact_error(reason)
        with self.team.connection() as connection:
            updated = connection.execute(
                """
                UPDATE cluster_jobs
                SET status = 'lost', finished_at = ?, error = ?
                WHERE job_id = ? AND status IN ('starting', 'running')
                """,
                (
                    utc_now(),
                    reason,
                    job["job_id"],
                ),
            )
            if updated.rowcount == 0:
                return
            self._event(
                connection,
                job["goal_id"],
                "app_server_turn_lost",
                "teamd",
                {
                    "job_id": job["job_id"],
                    "task_id": job.get("task_id"),
                    "thread_id": job.get("thread_id"),
                    "turn_id": job.get("turn_id"),
                    "runtime_generation": job.get("runtime_generation"),
                    "current_runtime_generation": (
                        self._current_runtime_generation()
                    ),
                    "reason": reason,
                },
            )
        with self._app_state_lock:
            self.app_turns.pop(job["job_id"], None)
            if job.get("thread_id"):
                self.app_thread_jobs.pop(str(job["thread_id"]), None)
            if job.get("turn_id"):
                self.app_turn_jobs.pop(str(job["turn_id"]), None)
        self._failed_agent_for_job(
            job,
            reason,
        )
        if job["kind"] == "worker" and job.get("task_id"):
            self._handle_worker_failure(job, reason)
        else:
            with self.team.connection() as connection:
                connection.execute(
                    """
                    UPDATE cluster_goals
                    SET lead_thread_id = NULL,
                        lead_failures = lead_failures + 1,
                        last_error = ?, updated_at = ?
                    WHERE goal_id = ?
                    """,
                    (
                        reason,
                        utc_now(),
                        job["goal_id"],
                    ),
                )

    def _finish_app_server_context_overflow(
        self,
        job: dict[str, Any],
        state: Mapping[str, Any],
    ) -> None:
        retry_scope = (
            f"lead:{job['goal_id']}"
            if job["kind"] == "lead"
            else f"worker:{job.get('task_id') or job['job_id']}"
        )
        with self.team.connection(write=False) as connection:
            rows = connection.execute(
                """
                SELECT data_json
                FROM cluster_events
                WHERE goal_id = ? AND event_type = 'agent_context_reset'
                """,
                (job["goal_id"],),
            ).fetchall()
        reset_count = 0
        for row in rows:
            try:
                data = json.loads(row["data_json"])
            except (TypeError, ValueError):
                continue
            event_scope = str(data.get("retry_scope") or "")
            if not event_scope:
                if data.get("kind") == "lead" or (
                    "kind" not in data and data.get("task_id") is None
                ):
                    event_scope = f"lead:{job['goal_id']}"
                elif data.get("task_id"):
                    event_scope = f"worker:{data['task_id']}"
            if event_scope == retry_scope:
                reset_count += 1
        retry_allowed = reset_count < 1
        exhausted_reason = (
            "contextWindowExceeded persisted after one automatic fresh-thread retry"
        )
        agent_id = str(job.get("agent_id") or "")
        if agent_id:
            try:
                self.agent_pool.mark_failed(
                    agent_id,
                    "contextWindowExceeded",
                    needs_reset=True,
                )
            except Exception:
                pass
        self._interrupt_app_turn(job, "contextWindowExceeded")
        if self.runtime is not None and job.get("thread_id"):
            try:
                self.runtime.unsubscribe_thread(str(job["thread_id"]))
            except Exception:
                pass
        with self.team.connection() as connection:
            self._event(
                connection,
                job["goal_id"],
                "agent_context_reset",
                "teamd",
                {
                    "job_id": job["job_id"],
                    "task_id": job.get("task_id"),
                    "kind": job["kind"],
                    "retry_scope": retry_scope,
                    "retry_number": reset_count + 1,
                    "thread_id": job.get("thread_id"),
                    "turn_id": job.get("turn_id"),
                    "retry_allowed": retry_allowed,
                    "error": self._app_turn_error(state),
                },
            )
            if not retry_allowed:
                if job["kind"] == "lead":
                    connection.execute(
                        """
                        UPDATE cluster_goals
                        SET status = 'failed', last_error = ?,
                            completed_at = COALESCE(completed_at, ?),
                            updated_at = ?
                        WHERE goal_id = ?
                        """,
                        (
                            exhausted_reason,
                            utc_now(),
                            utc_now(),
                            job["goal_id"],
                        ),
                    )
                self._event(
                    connection,
                    job["goal_id"],
                    "agent_context_retry_exhausted",
                    "teamd",
                    {
                        "job_id": job["job_id"],
                        "task_id": job.get("task_id"),
                        "kind": job["kind"],
                        "retry_scope": retry_scope,
                        "thread_id": job.get("thread_id"),
                        "reason": exhausted_reason,
                    },
                )
        if not retry_allowed and job["kind"] == "worker" and job.get("task_id"):
            task = self.team.get_task(str(job["task_id"]))
            if task["status"] not in {
                "review",
                "done",
                "blocked",
                "canceled",
            }:
                self.team.block_task(
                    str(job["task_id"]),
                    actor="teamd",
                    reason=exhausted_reason,
                )
        self._finish_job(
            job,
            1,
            forced_error=(
                "contextWindowExceeded"
                if retry_allowed
                else exhausted_reason
            ),
            agent_already_failed=True,
            context_overflow=True,
        )

    def _interrupt_app_turn(self, job: Mapping[str, Any], reason: str) -> None:
        thread_id = str(job.get("thread_id") or "")
        turn_id = str(job.get("turn_id") or "")
        with self._app_state_lock:
            state = self.app_turns.get(str(job.get("job_id")), {})
            already_requested = bool(state.get("interrupt_requested"))
            if not already_requested and state:
                state["interrupt_requested"] = True
                state["interrupt_reason"] = reason
        if not thread_id or not turn_id or already_requested or self.runtime is None:
            return
        try:
            self.runtime.interrupt_turn(thread_id, turn_id)
        except Exception as exc:
            with self.team.connection() as connection:
                self._event(
                    connection,
                    str(job["goal_id"]),
                    "app_server_interrupt_failed",
                    "teamd",
                    {
                        "job_id": job.get("job_id"),
                        "thread_id": thread_id,
                        "turn_id": turn_id,
                        "reason": reason,
                        "error": str(exc),
                    },
                )

    def _failed_agent_for_job(
        self,
        job: Mapping[str, Any],
        reason: str,
        *,
        needs_reset: bool = False,
    ) -> None:
        agent_id = str(job.get("agent_id") or "")
        if not agent_id:
            return
        try:
            self.agent_pool.mark_failed(
                agent_id,
                reason,
                needs_reset=needs_reset,
            )
        except Exception:
            pass

    def _app_job_usage(
        self,
        job: Mapping[str, Any],
        state: Mapping[str, Any],
    ) -> dict[str, int | None]:
        def values(value: Any) -> Mapping[str, Any]:
            return value if isinstance(value, Mapping) else {}

        usage = values(state.get("usage"))
        nested_usage = "total" in usage or "last" in usage
        total = values(usage.get("total")) if nested_usage else usage
        last_value = usage.get("last")
        last = values(last_value)
        first_usage = values(state.get("first_usage")) or usage
        first_nested_usage = (
            "total" in first_usage or "last" in first_usage
        )
        first_total = (
            values(first_usage.get("total"))
            if first_nested_usage
            else first_usage
        )
        first_last_value = first_usage.get("last")
        first_last = values(first_last_value)
        baseline_input = 0
        baseline_cached = 0
        baseline_output = 0
        agent_id = str(job.get("agent_id") or "")
        if agent_id:
            with self.team.connection(write=False) as connection:
                agent = connection.execute(
                    """
                    SELECT
                        a.total_input_tokens,
                        a.total_cached_input_tokens,
                        COALESCE((
                            SELECT SUM(j.output_tokens)
                            FROM cluster_jobs j
                            WHERE j.agent_id = a.agent_id
                              AND j.job_id <> ?
                              AND j.status = 'completed'
                        ), 0) AS prior_output_tokens
                    FROM cluster_agents a
                    WHERE a.agent_id = ?
                    """,
                    (str(job.get("job_id") or ""), agent_id),
                ).fetchone()
            if agent is not None:
                baseline_input = max(0, int(agent["total_input_tokens"] or 0))
                baseline_cached = max(
                    0,
                    int(agent["total_cached_input_tokens"] or 0),
                )
                baseline_output = max(
                    0,
                    int(agent["prior_output_tokens"] or 0),
                )

        def number(mapping: Mapping[str, Any], name: str) -> int:
            try:
                return max(0, int(mapping.get(name, 0) or 0))
            except (TypeError, ValueError):
                return 0

        def turn_values(
            cumulative: Mapping[str, Any],
            last_present: bool,
            per_turn: Mapping[str, Any],
        ) -> tuple[int, int, int, int]:
            if last_present:
                input_count = number(per_turn, "input")
                cached_count = min(number(per_turn, "cached"), input_count)
                output_count = number(per_turn, "output")
                context_count = number(per_turn, "context")
            else:
                input_count = max(
                    0,
                    number(cumulative, "input") - baseline_input,
                )
                cached_count = min(
                    max(0, number(cumulative, "cached") - baseline_cached),
                    input_count,
                )
                output_count = max(
                    0,
                    number(cumulative, "output") - baseline_output,
                )
                context_count = number(cumulative, "context")
            return input_count, cached_count, output_count, context_count

        input_tokens, cached_tokens, output_tokens, context_tokens = turn_values(
            total,
            isinstance(last_value, Mapping),
            last,
        )
        first_input, first_cached, _, _ = turn_values(
            first_total,
            isinstance(first_last_value, Mapping),
            first_last,
        )
        raw_window = usage.get(
            "modelContextWindow",
            total.get("modelContextWindow"),
        )
        try:
            model_window = (
                max(0, int(raw_window))
                if raw_window not in {None, 0}
                else None
            )
        except (TypeError, ValueError):
            model_window = None
        return {
            "input_tokens": input_tokens,
            "cached_input_tokens": cached_tokens,
            "uncached_input_tokens": input_tokens - cached_tokens,
            "output_tokens": output_tokens,
            "first_input_tokens": first_input,
            "first_cached_input_tokens": first_cached,
            "first_uncached_input_tokens": first_input - first_cached,
            "context_tokens": context_tokens,
            "model_context_window": model_window,
        }

    def _finalize_app_agent(
        self,
        job: Mapping[str, Any],
        state: Mapping[str, Any],
        *,
        success: bool,
        agent_already_failed: bool,
        needs_reset: bool,
    ) -> None:
        thread_id = str(job.get("thread_id") or "")
        if thread_id and self.runtime is not None:
            try:
                self.runtime.unsubscribe_thread(thread_id)
            except Exception:
                pass
        agent_id = str(job.get("agent_id") or "")
        if agent_id and not agent_already_failed:
            usage = self._app_job_usage(job, state)
            if success:
                try:
                    self.agent_pool.release(
                        agent_id,
                        usage,
                        cache_epoch_changed=True,
                    )
                except Exception:
                    pass
            else:
                self._failed_agent_for_job(
                    job,
                    self._app_turn_error(state) or "App Server turn failed",
                    needs_reset=needs_reset,
                )
        with self._app_state_lock:
            job_id = str(job.get("job_id"))
            self.app_turns.pop(job_id, None)
            if thread_id and self.app_thread_jobs.get(thread_id) == job_id:
                self.app_thread_jobs.pop(thread_id, None)
            turn_id = str(job.get("turn_id") or "")
            if turn_id and self.app_turn_jobs.get(turn_id) == job_id:
                self.app_turn_jobs.pop(turn_id, None)

    def _finish_job(
        self,
        job: dict[str, Any],
        exit_code: int | None,
        *,
        forced_error: str = "",
        agent_already_failed: bool = False,
        context_overflow: bool = False,
    ) -> None:
        pump = self.log_threads.pop(job["job_id"], None)
        if pump is not None:
            pump.join(timeout=5)
        for path_key in ("log_path", "output_path"):
            path_value = job.get(path_key)
            if path_value:
                _redact_sensitive_file(Path(path_value))
        app_server_runtime = job.get("runtime") == "app_server"
        if app_server_runtime:
            parsed: dict[str, Any] = {}
            with self._app_state_lock:
                app_state = dict(self.app_turns.get(job["job_id"], {}))
            thread_id = job.get("thread_id")
            process_ok = (
                not forced_error
                and exit_code == 0
                and app_state.get("status") == "completed"
            )
            process_error = forced_error or self._app_turn_error(app_state)
        else:
            app_state = {}
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
        usage = self._app_job_usage(job, app_state) if app_server_runtime else {}
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
                    SET status = ?, thread_id = ?, finished_at = ?, exit_code = ?, error = ?,
                        input_tokens = ?, cached_input_tokens = ?,
                        uncached_input_tokens = ?, output_tokens = ?,
                        first_input_tokens = ?, first_cached_input_tokens = ?,
                        first_uncached_input_tokens = ?, context_tokens = ?,
                        model_context_window = ?
                    WHERE job_id = ?
                    """,
                    (
                        status,
                        thread_id,
                        utc_now(),
                        exit_code,
                        error,
                        usage.get("input_tokens", 0),
                        usage.get("cached_input_tokens", 0),
                        usage.get("uncached_input_tokens", 0),
                        usage.get("output_tokens", 0),
                        usage.get("first_input_tokens", 0),
                        usage.get("first_cached_input_tokens", 0),
                        usage.get("first_uncached_input_tokens", 0),
                        usage.get("context_tokens", 0),
                        usage.get("model_context_window"),
                        job["job_id"],
                    ),
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
            if app_server_runtime:
                self._finalize_app_agent(
                    job,
                    app_state,
                    success=status == "completed",
                    agent_already_failed=agent_already_failed,
                    needs_reset=app_state.get("context_window_exceeded", False),
                )
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
                SET status = ?, thread_id = ?, finished_at = ?, exit_code = ?, error = ?,
                    input_tokens = ?, cached_input_tokens = ?,
                    uncached_input_tokens = ?, output_tokens = ?,
                    first_input_tokens = ?, first_cached_input_tokens = ?,
                    first_uncached_input_tokens = ?, context_tokens = ?,
                    model_context_window = ?
                WHERE job_id = ?
                """,
                (
                    status,
                    thread_id,
                    utc_now(),
                    exit_code,
                    error,
                    usage.get("input_tokens", 0),
                    usage.get("cached_input_tokens", 0),
                    usage.get("uncached_input_tokens", 0),
                    usage.get("output_tokens", 0),
                    usage.get("first_input_tokens", 0),
                    usage.get("first_cached_input_tokens", 0),
                    usage.get("first_uncached_input_tokens", 0),
                    usage.get("context_tokens", 0),
                    usage.get("model_context_window"),
                    job["job_id"],
                ),
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
                reset_thread = context_overflow or failures >= 2
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
        if app_server_runtime:
            self._finalize_app_agent(
                job,
                app_state,
                success=status == "completed",
                agent_already_failed=agent_already_failed,
                needs_reset=app_state.get("context_window_exceeded", False),
            )

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
    return redact_sensitive_text(text)


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
