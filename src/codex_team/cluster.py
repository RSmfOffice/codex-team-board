from __future__ import annotations

import json
import hashlib
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

from . import __version__
from .agent_home import service_codex_home, sync_agent_codex_home
from .agent_pool import AgentPool, AgentPoolError
from .app_server import (
    AppServerDisconnectedError,
    AppServerEvents,
    AppServerProtocolError,
    AppServerRPCError,
    AppServerRuntime,
    AppServerStartupError,
    AppServerTimeoutError,
)
from .cache_contract import agent_cache_profile, configured_surface
from .config import Settings
from .db import connect, connect_readonly, utc_now
from .native_threads import discover_native_sessions, inspect_native_session
from .processes import (
    ProcessJob,
    identity_record,
    persisted_identity_fields,
    persisted_process_identity,
    persisted_process_is_running,
    pid_is_running,
    process_identity,
    process_identity_matches,
)
from .prompt_contract import (
    configured_access_mode,
    instructions_for_surface,
)
from .redaction import redact_sensitive_text
from .scheduling import (
    DEFAULT_GLOBAL_JOB_CAPACITY,
    DEFAULT_GOAL_STALL_TICKS,
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
from .usage import (
    JOB_USAGE_FIELDS,
    normalize_usage,
    usage_field_is_present,
    usage_from_exec_log,
    usage_has_any_evidence,
    usage_has_cost_evidence,
    usage_is_cost_complete,
)
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
TICK_PHASE_ORDER = (
    "settle",
    "recover",
    "control",
    "prepare",
    "dispatch",
    "health",
    "publish",
)
TICK_RETRY_BUDGET = 4
TICK_PHASE_MAX_ATTEMPTS = 2
TICK_PHASE_RETRY_BASE_SECONDS = 0.05
TICK_PHASE_RETRY_MAX_SECONDS = 0.5
TICK_CONTROL_BACKOFF_BASE_SECONDS = 5.0
TICK_CONTROL_BACKOFF_MAX_SECONDS = 60.0
APP_SERVER_DELIVERY_MAX_ATTEMPTS = 2
TRANSIENT_APP_SERVER_ERRORS = (
    AppServerDisconnectedError,
    AppServerStartupError,
    AppServerTimeoutError,
    ConnectionError,
    TimeoutError,
    OSError,
)
LEAD_DEVELOPER_INSTRUCTIONS = """You are the managed Lead for an automated
Codex work cluster. Do not spawn subagents; the deterministic teamd dispatcher
launches Workers. You may use shell tools and the stable codexTeamBoard Core
tools: team_read(operation="...") and team_write(operation="...").

Use the compact board digest returned in each turn as the current source of
truth. Request a full task only when its detail is required. If there are
no tasks, decompose the objective into small, non-overlapping tasks with a goal
ID, acceptance criteria, dependencies, and owner roles. Use kind write for code
changes and team_worker; use read for team_explorer.

At phase boundaries, call
team_write(operation="team_goal_set_checkpoint", arguments={...}) with a compact
structured summary containing confirmed decisions, completed work, changed
files, tests, open risks, rejected approaches, and the next action. Update the
checkpoint whenever those durable facts change. Do not copy raw logs or full
transcripts into it.

For review tasks, inspect the diff and call
team_read(operation="team_get_task_events", arguments={...}), run tests, merge
the task branch into its recorded target branch, then call
team_write(operation="team_review_task", arguments={...}) with the real
merge_commit. Do not approve failed or dirty work.

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
    return f"""# Managed Agent Role Contract
{instructions.strip()}

## Thread Scope

- Project ID: `{goal.get("project_id") or "unknown"}`
- Project root: `{goal.get("root_path") or "unknown"}`
- Role: `{role}`
- Workstream: `{workstream}`

This identity is stable for the Agent thread. The current goal, assignment,
board state, and task-specific instructions arrive in the turn input.

## Mutation Idempotency

For every `team_write` call, pass a non-empty top-level `request_id` next to
`operation` and `arguments`. Derive it from the current job or goal, operation,
and a stable logical step. Retry the exact same payload with the exact same
`request_id`; use a new id for a distinct mutation. A mutation without
`request_id` remains compatible but is explicitly non-idempotent.
"""


def _agent_cache_profile(
    goal: Mapping[str, Any],
    role: str,
    instructions: str,
    *,
    cwd: Path | None = None,
    sandbox_mode: str = "workspace-write",
    runtime_version: str = "",
    runtime_identity: str = "",
) -> tuple[str, str]:
    stable_instructions = _stable_developer_instructions(goal, role, instructions)
    worktree_policy = (cwd / "AGENTS.md") if cwd is not None else None
    policy_path = (
        worktree_policy
        if worktree_policy is not None and worktree_policy.is_file()
        else Path(goal["root_path"]) / "AGENTS.md"
    )
    profile = agent_cache_profile(
        stable_instructions,
        policy_path=policy_path,
        sandbox_mode=sandbox_mode,
        surface=configured_surface(),
        access_mode=configured_access_mode(),
        mcp_instructions=instructions_for_surface(
            configured_surface(),
            configured_access_mode(),
        ),
        runtime_version=runtime_version,
        runtime_identity=runtime_identity,
    )
    return stable_instructions, profile


def _clip_text(value: object, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 20)].rstrip() + "\n...[truncated]"


def _runtime_mode(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    if not normalized:
        normalized = "app_server"
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


class WorkerClaimConflict(ClusterError):
    """A worker task was claimed by another owner before dispatch."""


class WorkflowEpochMismatch(ClusterError):
    """A goal or task cannot run under the current architecture epoch."""


class _AppServerJobStartError(ClusterError):
    """An App Server job could not start after its one runtime restart."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        failure_class: str = "permanent",
    ) -> None:
        super().__init__(message)
        self.retryable = bool(retryable)
        self.failure_class = str(failure_class)


class _AppServerGenerationDeltaError(ClusterError):
    """An App Server RPC observed an internally replaced runtime process."""

    def __init__(
        self,
        operation: str,
        previous_generation: int,
        current_generation: int,
    ) -> None:
        self.operation = operation
        self.previous_generation = previous_generation
        self.current_generation = current_generation
        super().__init__(
            f"{operation} observed App Server runtime generation change "
            f"from {previous_generation} to {current_generation}"
        )


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
        self.agent_codex_home_sync = sync_agent_codex_home(settings)
        self.agent_codex_home = self.agent_codex_home_sync.destination
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
                else "app_server"
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
        self.goal_stall_ticks = env_int(
            "CODEX_TEAM_GOAL_STALL_TICKS",
            DEFAULT_GOAL_STALL_TICKS,
            minimum=2,
            maximum=100,
        )
        self.lead_cooldown_seconds = max(0.0, float(lead_cooldown_seconds))
        self.processes: dict[str, subprocess.Popen[str]] = {}
        self.process_jobs: dict[str, ProcessJob] = {}
        self.log_threads: dict[str, threading.Thread] = {}
        self.app_turns: dict[str, dict[str, Any]] = {}
        self.app_thread_jobs: dict[str, str] = {}
        self.app_turn_jobs: dict[str, str] = {}
        self._app_state_lock = threading.RLock()
        self._tick_lock = threading.Lock()
        self._phase_failure_state: dict[str, dict[str, Any]] = {}
        self._runtime_fallback_reason = ""
        self.runtime = runtime
        if self.runtime is None and self.agent_runtime == "app_server":
            self.runtime = AppServerRuntime(
                [*self.agent_command, "app-server", "--listen", "stdio://"],
                stderr_log_path=self.settings.logs_dir / "app-server.stderr.log",
                event_handler=_ClusterAppServerEvents(self),
                env={
                    "CODEX_TEAM_HOME": str(self.settings.home),
                    "CODEX_HOME": str(self.agent_codex_home),
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
        project = self.team.register_project(repo)
        self.team.ensure_project_active(project)
        workflow_epoch_value = self.team.current_workflow_epoch(
            project["root_path"],
            resolved_root=True,
        )
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
                    goal_id, project_id, workflow_epoch, objective, status, autonomy,
                    max_worker_attempts, max_lead_attempts, workstream_key,
                    continuity_reason, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'planning', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    goal_id,
                    project["project_id"],
                    workflow_epoch_value,
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
                    "workflow_epoch": workflow_epoch_value,
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
        project = self.team.register_project(repo)
        self.team.ensure_project_active(project)
        workflow_epoch_value = self.team.current_workflow_epoch(
            project["root_path"],
            resolved_root=True,
        )
        now = utc_now()
        created = False
        with self.team.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                """
                SELECT goal_id, status, objective, workstream_key,
                       continuity_reason, workflow_epoch
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
                if active["workflow_epoch"] != workflow_epoch_value:
                    raise ClusterError(
                        "existing goal has incompatible workflow epoch; "
                        "migrate or archive it before continuing"
                    )
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
                        goal_id, project_id, workflow_epoch, objective, status,
                        autonomy,
                        max_worker_attempts, max_lead_attempts, workstream_key,
                        continuity_reason, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'planning', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        goal_id,
                        project["project_id"],
                        workflow_epoch_value,
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
                        "workflow_epoch": workflow_epoch_value,
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
                include_archived=True,
            )
            return result

    def migrate_goal_workflow_epoch(
        self,
        goal_id: str,
        *,
        actor: str = "human",
        reason: str = "explicit workflow epoch migration",
    ) -> dict[str, Any]:
        actor = str(actor or "").strip()
        reason = str(reason or "").strip()
        if not actor:
            raise ClusterError("actor is required")
        if not reason:
            raise ClusterError("migration reason is required")
        now = utc_now()
        with self.team.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            goal = connection.execute(
                """
                SELECT g.*, p.root_path
                FROM cluster_goals g
                JOIN projects p ON p.project_id = g.project_id
                WHERE g.goal_id = ?
                """,
                (goal_id,),
            ).fetchone()
            if goal is None:
                raise ClusterError(f"cluster goal not found: {goal_id}")
            if goal["archived_at"]:
                raise ClusterError("archived goal cannot migrate workflow epoch")
            active_jobs = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM cluster_jobs
                WHERE goal_id = ? AND status IN ('starting', 'running')
                """,
                (goal_id,),
            ).fetchone()["count"]
            if int(active_jobs):
                raise ClusterError(
                    "cannot migrate workflow epoch while the goal has active jobs"
                )
            current_epoch = self.team.current_workflow_epoch(
                goal["root_path"],
                resolved_root=True,
            )
            previous_epoch = str(goal["workflow_epoch"] or "")
            tasks = connection.execute(
                """
                SELECT task_id, status, workflow_epoch
                FROM tasks
                WHERE goal_id = ?
                ORDER BY task_id
                """,
                (goal_id,),
            ).fetchall()
            if any(task["status"] == "running" for task in tasks):
                raise ClusterError(
                    "cannot migrate workflow epoch with a running task"
                )
            needs_migration = (
                previous_epoch != current_epoch
                or any(
                    task["status"] not in TERMINAL_TASK_STATUSES
                    and task["workflow_epoch"] != current_epoch
                    for task in tasks
                )
            )
            if (
                needs_migration
                and any(
                    task["status"] not in TERMINAL_TASK_STATUSES
                    for task in tasks
                )
                and not str(goal["checkpoint_summary"] or "").strip()
            ):
                raise ClusterError(
                    "workflow epoch migration requires a durable checkpoint"
                )
            connection.execute(
                """
                UPDATE cluster_goals
                SET workflow_epoch = ?, updated_at = ?
                WHERE goal_id = ?
                """,
                (current_epoch, now, goal_id),
            )
            migrated_tasks: list[str] = []
            for task in tasks:
                if task["status"] in TERMINAL_TASK_STATUSES:
                    continue
                connection.execute(
                    """
                    UPDATE tasks
                    SET workflow_epoch = ?, updated_at = ?, version = version + 1
                    WHERE task_id = ?
                    """,
                    (current_epoch, now, task["task_id"]),
                )
                self.team._event(
                    connection,
                    str(task["task_id"]),
                    "workflow_epoch_migrated",
                    actor,
                    {
                        "previous_epoch": task["workflow_epoch"],
                        "workflow_epoch": current_epoch,
                        "reason": reason,
                    },
                )
                migrated_tasks.append(str(task["task_id"]))
            self._event(
                connection,
                goal_id,
                "workflow_epoch_migrated",
                actor,
                {
                    "previous_epoch": previous_epoch,
                    "workflow_epoch": current_epoch,
                    "migrated_tasks": migrated_tasks,
                    "reason": reason,
                },
            )
        return {
            "goal": self.get_goal(goal_id),
            "previous_epoch": previous_epoch,
            "workflow_epoch": current_epoch,
            "migrated_tasks": migrated_tasks,
        }

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
                    f"""
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
                """
                SELECT g.*, p.root_path
                FROM cluster_goals g
                JOIN projects p ON p.project_id = g.project_id
                WHERE g.goal_id = ?
                """,
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
                current_epoch = self.team.current_workflow_epoch(
                    row["root_path"],
                    resolved_root=True,
                )
                if row["workflow_epoch"] != current_epoch:
                    raise ClusterError(
                        "goal workflow epoch is incompatible with current "
                        "architecture; migrate it before resume"
                    )
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
            if status == "active":
                connection.execute(
                    """
                    UPDATE cluster_goals
                    SET stall_ticks = 0, stall_signature = '', stalled_at = NULL
                    WHERE goal_id = ?
                    """,
                    (goal_id,),
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
        active_jobs: list[dict[str, Any]] = []
        with self.team.connection() as connection:
            # Fence dispatch and settle the captured task/job set in one writer
            # transaction. A concurrent archive/status/cancel therefore either
            # observes the old state and rejects, or the fully canceled state.
            connection.execute("BEGIN IMMEDIATE")
            goal = connection.execute(
                """
                SELECT g.*, p.root_path
                FROM cluster_goals g
                JOIN projects p ON p.project_id = g.project_id
                WHERE g.goal_id = ?
                """,
                (goal_id,),
            ).fetchone()
            if goal is None:
                raise ClusterError(f"cluster goal not found: {goal_id}")
            if goal["archived_at"] is not None:
                raise ClusterError(
                    "archived goal cannot be canceled; restore it first"
                )
            previous_status = str(goal["status"])
            if previous_status in TERMINAL_GOAL_STATUSES:
                raise ClusterError(
                    f"cannot cancel terminal goal: {previous_status}"
                )
            if previous_status not in GOAL_STATUS_TRANSITIONS:
                raise ClusterError(
                    f"cannot cancel goal with unknown status: {previous_status}"
                )
            active_jobs = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT *
                    FROM cluster_jobs
                    WHERE goal_id = ? AND status IN ('starting', 'running')
                    ORDER BY started_at, job_id
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
                    ORDER BY task_id
                    """,
                    (goal_id,),
                ).fetchall()
            ]
            now = utc_now()
            fenced = connection.execute(
                """
                UPDATE cluster_goals
                SET status = 'failed', updated_at = ?, completed_at = ?,
                    last_error = ?
                WHERE goal_id = ? AND status = ? AND archived_at IS NULL
                """,
                (now, now, reason, goal_id, previous_status),
            )
            if fenced.rowcount != 1:
                raise ClusterError(
                    "goal changed while cancellation was acquiring its fence"
                )
            for job in active_jobs:
                usage: Mapping[str, Any] | None = None
                usage_source: str | None = None
                if job.get("runtime") == "app_server":
                    with self._app_state_lock:
                        state = dict(self.app_turns.get(job["job_id"], {}))
                    usage = self._app_job_usage(job, state)
                    if _usage_has_evidence(usage):
                        usage_source = (
                            "cluster_agents.last_usage+runtime_raw_turn"
                        )
                settled = self._settle_job_row(
                    connection,
                    job["job_id"],
                    status="interrupted",
                    error=reason,
                    exit_code=-9,
                    usage=usage,
                    usage_source=usage_source,
                    telemetry_complete=(
                        _usage_is_telemetry_complete(usage)
                        if usage is not None and _usage_has_evidence(usage)
                        else False
                    ),
                    usage_missing_fields=(
                        _usage_missing_fields_json(usage)
                        if usage is not None
                        else None
                    ),
                )
                if not settled:
                    raise ClusterError(
                        f"active job changed before cancellation: {job['job_id']}"
                    )
            for task in tasks:
                updated = connection.execute(
                    """
                    UPDATE tasks
                    SET status = 'blocked', blocker = ?, claimed_by = NULL,
                        claim_token = NULL, lease_until = NULL,
                        updated_at = ?, version = version + 1
                    WHERE task_id = ? AND status NOT IN ('done', 'canceled')
                    """,
                    (f"goal canceled: {reason}", now, task["task_id"]),
                )
                if updated.rowcount != 1:
                    continue
                self.team._event(
                    connection,
                    task["task_id"],
                    "goal_canceled",
                    "teamd",
                    {"goal_id": goal_id, "reason": reason},
                )
            self._event(
                connection,
                goal_id,
                "goal_status",
                "teamd",
                {
                    "status": "failed",
                    "reason": reason,
                    "previous_status": previous_status,
                    "canceled_jobs": [
                        str(job["job_id"]) for job in active_jobs
                    ],
                    "blocked_tasks": [
                        str(task["task_id"]) for task in tasks
                    ],
                },
            )
        for job in active_jobs:
            if job.get("runtime") == "app_server":
                self._interrupt_app_turn(job, reason)
                continue
            self._terminate_exec_job(job, exit_code=-9)
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
        return self.get_goal(goal_id)

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
            now = utc_now()
            archived_at = now if archived else None
            changed_task_ids: list[str] = []
            if archived:
                tasks = connection.execute(
                    """
                    SELECT task_id
                    FROM tasks
                    WHERE goal_id = ? AND archived_at IS NULL
                    ORDER BY task_id
                    """,
                    (goal_id,),
                ).fetchall()
                for task in tasks:
                    task_id = str(task["task_id"])
                    updated = connection.execute(
                        """
                        UPDATE tasks
                        SET archived_at = ?, archived_by_goal = 1,
                            updated_at = ?, version = version + 1
                        WHERE task_id = ? AND archived_at IS NULL
                        """,
                        (now, now, task_id),
                    )
                    if updated.rowcount != 1:
                        continue
                    changed_task_ids.append(task_id)
                    connection.execute(
                        """
                        INSERT INTO task_events(
                            task_id, event_type, actor, data_json, created_at
                        ) VALUES (?, 'archived_by_goal', ?, ?, ?)
                        """,
                        (
                            task_id,
                            actor,
                            json.dumps(
                                {"goal_id": goal_id},
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                            now,
                        ),
                    )
            else:
                tasks = connection.execute(
                    """
                    SELECT task_id
                    FROM tasks
                    WHERE goal_id = ? AND archived_by_goal = 1
                    ORDER BY task_id
                    """,
                    (goal_id,),
                ).fetchall()
                for task in tasks:
                    task_id = str(task["task_id"])
                    updated = connection.execute(
                        """
                        UPDATE tasks
                        SET archived_at = NULL, archived_by_goal = 0,
                            updated_at = ?, version = version + 1
                        WHERE task_id = ? AND archived_by_goal = 1
                        """,
                        (now, task_id),
                    )
                    if updated.rowcount != 1:
                        continue
                    changed_task_ids.append(task_id)
                    connection.execute(
                        """
                        INSERT INTO task_events(
                            task_id, event_type, actor, data_json, created_at
                        ) VALUES (?, 'restored_by_goal', ?, ?, ?)
                        """,
                        (
                            task_id,
                            actor,
                            json.dumps(
                                {"goal_id": goal_id},
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                            now,
                        ),
                    )
            connection.execute(
                """
                UPDATE cluster_goals
                SET archived_at = ?, updated_at = ?
                WHERE goal_id = ?
                """,
                (archived_at, now, goal_id),
            )
            self._event(
                connection,
                goal_id,
                "goal_archived" if archived else "goal_restored",
                actor,
                {
                    "archived": archived,
                    "task_ids": changed_task_ids,
                    "task_count": len(changed_task_ids),
                },
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

    def set_goal_checkpoint(
        self,
        goal_id: str,
        *,
        summary: str,
        actor: str = "lead",
    ) -> dict[str, Any]:
        normalized = str(summary or "").strip()
        if not normalized:
            raise ClusterError("checkpoint summary must not be empty")
        if len(normalized) > 12000:
            raise ClusterError("checkpoint summary must not exceed 12000 characters")
        now = utc_now()
        with self.team.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM cluster_goals WHERE goal_id = ?",
                (goal_id,),
            ).fetchone()
            if row is None:
                raise ClusterError(f"cluster goal not found: {goal_id}")
            if row["archived_at"]:
                raise ClusterError("archived goal cannot update checkpoint")
            connection.execute(
                """
                UPDATE cluster_goals
                SET checkpoint_summary = ?, checkpoint_updated_at = ?,
                    updated_at = ?
                WHERE goal_id = ?
                """,
                (normalized, now, now, goal_id),
            )
            self._event(
                connection,
                goal_id,
                "goal_checkpoint_updated",
                actor,
                {
                    "summary_chars": len(normalized),
                    "checkpoint_updated_at": now,
                },
            )
        return self.get_goal(goal_id)

    def latest_workstream_checkpoint(
        self,
        project_id: str,
        workstream_key: str,
    ) -> dict[str, Any] | None:
        project_id = str(project_id or "").strip()
        workstream_key = str(workstream_key or "").strip()
        if not project_id or not workstream_key:
            return None
        with self.team.connection(write=False) as connection:
            row = connection.execute(
                """
                SELECT goal_id, objective, checkpoint_summary,
                       checkpoint_updated_at
                FROM cluster_goals
                WHERE project_id = ? AND workstream_key = ?
                  AND checkpoint_summary != ''
                ORDER BY checkpoint_updated_at DESC, updated_at DESC, goal_id DESC
                LIMIT 1
                """,
                (project_id, workstream_key),
            ).fetchone()
        return dict(row) if row is not None else None

    def _checkpoint_context(self, goal: Mapping[str, Any]) -> dict[str, Any]:
        summary = str(goal.get("checkpoint_summary") or "").strip()
        if summary:
            return {
                "goal_id": goal.get("goal_id"),
                "objective": goal.get("objective"),
                "summary": summary,
                "updated_at": goal.get("checkpoint_updated_at"),
                "source": "current_goal",
            }
        workstream = str(
            goal.get("workstream_key") or goal.get("goal_id") or ""
        ).strip()
        inherited = self.latest_workstream_checkpoint(
            str(goal.get("project_id") or ""),
            workstream,
        )
        if inherited is None:
            return {
                "summary": "",
                "source": "none",
            }
        return {**inherited, "source": "latest_workstream"}

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

    def register_native_agent(
        self,
        repo: str | Path,
        thread_id: str,
        role: str,
        workstream_key: str,
        *,
        actor: str,
        task_id: str | None = None,
        display_name: str | None = None,
        model: str = "",
        model_provider: str = "",
        task_candidates: list[str] | None = None,
        task_assignment_ambiguous: bool | None = None,
        assignment_source: str | None = None,
        assignment_confidence: str | None = None,
        identity_source: str | None = None,
        identity_confidence: str | None = None,
    ) -> dict[str, Any]:
        inspection = inspect_native_session(
            thread_id,
            task_id=task_id,
        )
        if not inspection.get("session_found"):
            raise ClusterError(
                "native session is missing; project ownership cannot be verified"
            )
        session_cwd = str(inspection.get("cwd") or "").strip()
        if not session_cwd:
            raise ClusterError(
                "native session cwd is missing; project ownership cannot be verified"
            )
        project = self.team.project_for_repo(repo)
        self.team.ensure_project_active(project)
        active_projects = self.team.list_projects(include_archived=False)
        matching_projects = [
            candidate
            for candidate in active_projects
            if _path_is_within(
                session_cwd,
                str(candidate.get("root_path") or ""),
            )
        ]
        matching_projects.sort(
            key=lambda candidate: len(
                str(Path(str(candidate["root_path"])).resolve())
            ),
            reverse=True,
        )
        if (
            not matching_projects
            or str(matching_projects[0]["project_id"])
            != str(project["project_id"])
        ):
            raise ClusterError(
                "native session cwd does not belong to the requested project"
            )
        task: dict[str, Any] | None = None
        if task_id:
            task = self.team.get_task(task_id)
            if task["project_id"] != project["project_id"]:
                raise ClusterError("task does not belong to this project")
            if task.get("archived_at"):
                raise ClusterError("archived task cannot bind a native thread")
        inspection_assignment = str(
            inspection.get("assignment_task_id") or ""
        ).strip().upper()
        requested_task = str(task_id or "").strip().upper()
        if (
            requested_task
            and inspection_assignment
            and requested_task != inspection_assignment
        ):
            raise ClusterError(
                "native session assignment conflicts with the requested task"
            )
        requested_role = str(role or "").strip().lower()
        session_role = str(inspection.get("agent_role") or "").strip().lower()
        session_identity_source = str(
            inspection.get("identity_source") or "missing"
        ).strip().lower()
        session_identity_confidence = str(
            inspection.get("identity_confidence") or "unknown"
        ).strip().lower()
        documented_roles = {
            "lead",
            "team_explorer",
            "team_worker",
            "team_reviewer",
        }
        requested_role_is_documented = requested_role in documented_roles
        session_role_is_documented = session_role in documented_roles
        session_role_is_explicit = (
            session_role_is_documented
            and session_identity_source == "explicit_session_metadata"
        )
        if (
            session_role_is_explicit
            and requested_role_is_documented
            and requested_role != session_role
        ):
            raise ClusterError(
                "native session role conflicts with the requested registration role"
            )
        if session_role_is_explicit:
            effective_role = session_role
            resolved_identity_source = "explicit_session_metadata"
            resolved_identity_confidence = "high"
        elif requested_role_is_documented:
            effective_role = requested_role
            if identity_source == "registry":
                resolved_identity_source = "registry"
                resolved_identity_confidence = (
                    identity_confidence or "medium"
                )
            elif (
                identity_source == "agent_path_inference"
                and session_role == requested_role
            ):
                resolved_identity_source = "agent_path_inference"
                resolved_identity_confidence = (
                    identity_confidence or "low"
                )
            else:
                resolved_identity_source = "registration"
                resolved_identity_confidence = (
                    identity_confidence
                    if identity_source == "registration"
                    else "medium"
                )
        elif session_role_is_documented:
            effective_role = session_role
            resolved_identity_source = session_identity_source
            resolved_identity_confidence = session_identity_confidence
        else:
            effective_role = "unknown"
            resolved_identity_source = (
                identity_source
                if identity_source in {"registration", "registry"}
                else "missing"
            )
            resolved_identity_confidence = (
                identity_confidence or "unknown"
                if identity_source in {"registration", "registry"}
                else "unknown"
            )
        candidates = sorted(
            {
                str(value).strip().upper()
                for value in (
                    task_candidates
                    if task_candidates is not None
                    else inspection.get("task_candidates", [])
                )
                if str(value).strip()
            }
        )
        if task_id and str(task_id).strip().upper() not in candidates:
            candidates.append(str(task_id).strip().upper())
            candidates.sort()
        assignment_ambiguous = (
            bool(task_assignment_ambiguous)
            if task_assignment_ambiguous is not None
            else bool(inspection.get("task_assignment_ambiguous"))
        )
        resolved_assignment_source = str(
            assignment_source
            or inspection.get("assignment_source")
            or "none"
        )
        resolved_assignment_confidence = str(
            assignment_confidence
            or inspection.get("assignment_confidence")
            or "unknown"
        )
        if task_id and resolved_assignment_source in {"none", "ambiguous"}:
            resolved_assignment_source = "registration"
            resolved_assignment_confidence = "high"
            assignment_ambiguous = False
        elif not task_id and len(candidates) > 1:
            resolved_assignment_source = "ambiguous"
            resolved_assignment_confidence = "unknown"
            assignment_ambiguous = True
        try:
            agent = self.agent_pool.register_native_thread(
                str(project["project_id"]),
                effective_role,
                workstream_key,
                thread_id,
                actor,
                task_id=task_id,
                parent_thread_id=inspection.get("parent_thread_id"),
                agent_path=inspection.get("agent_path"),
                agent_nickname=inspection.get("agent_nickname"),
                display_name=display_name,
                model=model,
                model_provider=model_provider,
                prompt_hash=str(inspection.get("prompt_hash") or ""),
                prompt_status=str(
                    inspection.get("prompt_status") or "unknown"
                ),
                native_session_path=inspection.get("session_path"),
                native_session_mtime_ns=int(
                    inspection.get("session_mtime_ns") or 0
                ),
                native_session_size=int(
                    inspection.get("session_size") or 0
                ),
                last_seen_at=inspection.get("last_seen_at"),
                usage=(
                    inspection.get("usage")
                    if isinstance(inspection.get("usage"), Mapping)
                    else None
                ),
                identity_source=resolved_identity_source,
                identity_confidence=resolved_identity_confidence,
                lifecycle=str(inspection.get("lifecycle") or "unknown"),
                lifecycle_source=str(
                    inspection.get("lifecycle_source") or "unknown"
                ),
                assignment_source=resolved_assignment_source,
                assignment_confidence=resolved_assignment_confidence,
                task_candidates=candidates,
                task_assignment_ambiguous=assignment_ambiguous,
                session_stale=bool(inspection.get("session_stale", True)),
            )
        except AgentPoolError as exc:
            raise ClusterError(str(exc)) from exc
        if task_id:
            event_type = (
                "native_prompt_missing"
                if inspection.get("prompt_status") == "missing"
                else "native_thread_registered"
            )
            with self.team.connection() as connection:
                self.team._event(
                    connection,
                    task_id,
                    event_type,
                    actor,
                    {
                        "thread_id": thread_id,
                        "prompt_status": inspection.get("prompt_status"),
                        "session_found": inspection.get("session_found"),
                        "parent_thread_id": inspection.get(
                            "parent_thread_id"
                        ),
                        "identity_source": resolved_identity_source,
                        "identity_confidence": resolved_identity_confidence,
                        "lifecycle": inspection.get("lifecycle"),
                        "task_candidates": candidates,
                        "task_assignment_ambiguous": assignment_ambiguous,
                    },
                    thread_id=thread_id,
                    correlation_id=f"native:{thread_id}",
                    assignment_id=thread_id,
                )
        return {
            "agent": agent,
            "inspection": inspection,
            "effective_role": effective_role,
            "role_mismatch": bool(
                session_role
                and requested_role
                and session_role != requested_role
            ),
            "same_task_only": True,
        }

    def sync_native_agents(
        self,
        *,
        actor: str = "dashboard",
        project_id: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        max_age_hours = env_int(
            "CODEX_TEAM_NATIVE_DISCOVERY_MAX_AGE_HOURS",
            24,
            maximum=24 * 365,
        )
        discovered = discover_native_sessions(
            limit=limit,
            max_age_seconds=max_age_hours * 3600,
        )
        projects = self.team.list_projects(include_archived=False)
        if project_id:
            projects = [
                project
                for project in projects
                if str(project["project_id"]) == project_id
            ]
        project_root_rows: list[tuple[Path, dict[str, Any]]] = []
        for project in projects:
            try:
                root = Path(
                    str(project["root_path"])
                ).expanduser().resolve()
            except (OSError, RuntimeError):
                continue
            project_root_rows.append((root, project))
        project_roots = sorted(
            project_root_rows,
            key=lambda item: len(str(item[0])),
            reverse=True,
        )
        with self.team.connection(write=False) as connection:
            registered_threads = {
                str(row["thread_id"]): dict(row)
                for row in connection.execute(
                    """
                    SELECT *
                    FROM cluster_agents
                    """
                ).fetchall()
            }
            task_rows = connection.execute(
                """
                SELECT task_id, project_id, archived_at
                FROM tasks
                ORDER BY created_at DESC, task_id
                """
            ).fetchall()
        tasks = {
            str(row["task_id"]): {
                "task_id": str(row["task_id"]),
                "project_id": str(row["project_id"]),
                "archived": bool(row["archived_at"]),
            }
            for row in task_rows
        }

        def matching_project(cwd: str | None) -> dict[str, Any] | None:
            if not cwd:
                return None
            try:
                path = Path(cwd).expanduser().resolve()
            except (OSError, RuntimeError):
                return None
            for root, project in project_roots:
                if path == root or root in path.parents:
                    return project
            return None

        def native_state_needs_refresh(
            existing: Mapping[str, Any],
            item: Mapping[str, Any],
        ) -> bool:
            if existing.get("runtime") != "native":
                return False
            if not item.get("session_found", True):
                return bool(
                    int(existing.get("session_stale") or 0) != 1
                    or existing.get("native_usage_status")
                    not in {"stale", "unavailable"}
                )
            usage = (
                item.get("usage")
                if isinstance(item.get("usage"), Mapping)
                else {}
            )
            item_candidates = json.dumps(
                sorted(
                    {
                        str(value)
                        for value in item.get("task_candidates", [])
                        if str(value)
                    }
                ),
                separators=(",", ":"),
            )
            usage_status = str(usage.get("status") or "unavailable")
            existing_usage_status = str(
                existing.get("native_usage_status") or "unavailable"
            )
            effective_usage_status = (
                "stale"
                if usage_status == "unavailable"
                and existing_usage_status
                in {"available", "partial", "stale"}
                else usage_status
            )
            missing_usage_fields = {
                str(field)
                for field in usage.get("missing_fields", [])
                if str(field).strip()
            }
            usage_changed = False
            if effective_usage_status in {"available", "partial", "stale"}:
                usage_fields = (
                    "last_input_tokens",
                    "last_cached_input_tokens",
                    "last_uncached_input_tokens",
                    "total_input_tokens",
                    "total_cached_input_tokens",
                    "total_uncached_input_tokens",
                    "output_tokens",
                    "context_tokens",
                    "model_context_window",
                    "turn_count",
                    "last_turn_id",
                )
                usage_changed = any(
                    existing.get(field) != usage.get(field)
                    for field in usage_fields
                    if field not in missing_usage_fields
                )
            return any(
                (
                    existing.get("last_seen_at")
                    != item.get("last_seen_at"),
                    existing.get("identity_source")
                    != item.get("identity_source"),
                    existing.get("identity_confidence")
                    != item.get("identity_confidence"),
                    existing.get("lifecycle") != item.get("lifecycle"),
                    int(existing.get("session_stale") or 0)
                    != int(bool(item.get("session_stale"))),
                    bool(int(existing.get("task_assignment_ambiguous") or 0))
                    != bool(item.get("task_assignment_ambiguous")),
                    existing.get("task_candidates_json") != item_candidates,
                    existing_usage_status != effective_usage_status,
                    int(existing.get("native_session_mtime_ns") or 0)
                    != int(
                        item.get("session_mtime_ns")
                        or item.get("native_session_mtime_ns")
                        or 0
                    ),
                    int(existing.get("native_session_size") or 0)
                    != int(
                        item.get("session_size")
                        or item.get("native_session_size")
                        or 0
                    ),
                    usage_changed,
                )
            )

        def valid_task_id(
            value: Any,
            project: Mapping[str, Any],
        ) -> str | None:
            task_id = str(value or "").strip().upper()
            task = tasks.get(task_id)
            if (
                task is None
                or task["project_id"] != str(project["project_id"])
                or task["archived"]
            ):
                return None
            return task_id

        def existing_candidates(existing: Mapping[str, Any]) -> list[str]:
            try:
                parsed = json.loads(
                    str(existing.get("task_candidates_json") or "[]")
                )
            except (TypeError, ValueError):
                parsed = []
            values = parsed if isinstance(parsed, list) else []
            return sorted(
                {
                    str(value).strip().upper()
                    for value in values
                    if str(value).strip()
                }
            )

        def normalized_native_state(
            item: Mapping[str, Any],
            existing: Mapping[str, Any] | None,
            project: Mapping[str, Any],
        ) -> dict[str, Any]:
            explicit_assignment = str(
                item.get("assignment_task_id") or ""
            ).strip().upper()
            explicit_task_row = tasks.get(explicit_assignment)
            if (
                explicit_assignment
                and explicit_task_row is not None
                and (
                    explicit_task_row["project_id"]
                    != str(project["project_id"])
                    or explicit_task_row["archived"]
                )
            ):
                raise ClusterError(
                    "native session task assignment does not belong to "
                    "the session project"
                )
            candidates = sorted(
                {
                    str(value).strip().upper()
                    for value in item.get("task_candidates", [])
                    if str(value).strip()
                }
            )
            if existing is not None:
                candidates = sorted(
                    set(candidates) | set(existing_candidates(existing))
                )
            session_role = str(
                item.get("agent_role") or ""
            ).strip().lower()
            existing_role = (
                str(existing.get("role") or "").strip().lower()
                if existing is not None
                else ""
            )
            documented_roles = {
                "lead",
                "team_explorer",
                "team_worker",
                "team_reviewer",
            }
            if (
                session_role in documented_roles
                and (
                    item.get("identity_source")
                    == "explicit_session_metadata"
                    or existing_role not in documented_roles
                )
            ):
                role = session_role
                identity_source = str(
                    item.get("identity_source")
                    or "explicit_session_metadata"
                )
                identity_confidence = str(
                    item.get("identity_confidence") or "high"
                )
            elif existing_role in documented_roles:
                role = existing_role
                identity_source = "registry"
                identity_confidence = "medium"
            else:
                role = "unknown"
                identity_source = "missing"
                identity_confidence = "unknown"

            existing_task = valid_task_id(
                existing.get("current_task_id") if existing else None,
                project,
            )
            explicit_task = valid_task_id(
                item.get("assignment_task_id"),
                project,
            )
            if existing_task:
                if explicit_task and explicit_task != existing_task:
                    raise ClusterError(
                        "native session task assignment conflicts with "
                        "the registered same-task binding"
                    )
                task_id = existing_task
                assignment_source = "registry"
                assignment_confidence = "high"
                assignment_ambiguous = bool(
                    item.get("task_assignment_ambiguous")
                    or len(candidates) > 1
                )
            elif explicit_task:
                task_id = explicit_task
                assignment_source = str(
                    item.get("assignment_source")
                    or "explicit_session_metadata"
                )
                assignment_confidence = str(
                    item.get("assignment_confidence") or "high"
                )
                assignment_ambiguous = bool(
                    item.get("task_assignment_ambiguous")
                )
            elif len(candidates) == 1 and candidates[0] in tasks:
                task_id = valid_task_id(candidates[0], project)
                assignment_source = (
                    "unique_session_candidate"
                    if task_id
                    else "none"
                )
                assignment_confidence = "medium" if task_id else "unknown"
                assignment_ambiguous = False
            else:
                task_id = None
                assignment_source = (
                    "ambiguous" if len(candidates) > 1 else "none"
                )
                assignment_confidence = (
                    "unknown"
                    if assignment_source == "ambiguous"
                    else "unknown"
                )
                assignment_ambiguous = len(candidates) > 1

            return {
                **dict(item),
                "role": role,
                "identity_source": identity_source,
                "identity_confidence": identity_confidence,
                "task_id": task_id,
                "task_candidates": candidates,
                "assignment_source": assignment_source,
                "assignment_confidence": assignment_confidence,
                "task_assignment_ambiguous": assignment_ambiguous,
            }

        registered: list[dict[str, Any]] = []
        refreshed: list[dict[str, Any]] = []
        skipped_existing: list[str] = []
        skipped_unknown_project: list[str] = []
        errors: list[dict[str, str]] = []
        discovered_thread_ids = {
            str(item["thread_id"]) for item in discovered
        }
        projects_by_id = {
            str(project["project_id"]): project for project in projects
        }
        for item in discovered:
            thread_id = str(item["thread_id"])
            project = matching_project(item.get("cwd"))
            if project is None:
                skipped_unknown_project.append(thread_id)
                continue
            existing = registered_threads.get(thread_id)
            if (
                existing is not None
                and str(existing.get("project_id"))
                != str(project["project_id"])
            ):
                errors.append(
                    {
                        "thread_id": thread_id,
                        "error": (
                            "native session cwd belongs to a different "
                            "project than the registered thread"
                        ),
                    }
                )
                continue
            try:
                state = normalized_native_state(
                    item,
                    existing,
                    project,
                )
            except (ClusterError, TeamError, OSError) as exc:
                errors.append(
                    {
                        "thread_id": thread_id,
                        "error": str(exc),
                    }
                )
                continue
            if (
                existing is not None
                and not native_state_needs_refresh(existing, state)
            ):
                skipped_existing.append(thread_id)
                continue
            if existing is not None:
                workstream_key = str(existing["workstream_key"])
            else:
                parent_thread_id = item.get("parent_thread_id")
                workstream_key = (
                    f"native:{parent_thread_id}"
                    if parent_thread_id
                    else f"native:{thread_id}"
                )
            try:
                result = self.register_native_agent(
                    project["root_path"],
                    thread_id,
                    str(state["role"]),
                    workstream_key,
                    actor=f"{actor}:{thread_id}",
                    task_id=state["task_id"],
                    display_name=(
                        item.get("agent_nickname")
                        or item.get("agent_path")
                        or thread_id
                    ),
                    model_provider=str(item.get("model_provider") or ""),
                    task_candidates=state["task_candidates"],
                    task_assignment_ambiguous=state[
                        "task_assignment_ambiguous"
                    ],
                    assignment_source=state["assignment_source"],
                    assignment_confidence=state[
                        "assignment_confidence"
                    ],
                    identity_source=state["identity_source"],
                    identity_confidence=state["identity_confidence"],
                )
            except (AgentPoolError, ClusterError, TeamError, OSError) as exc:
                errors.append(
                    {
                        "thread_id": thread_id,
                        "error": str(exc),
                    }
                )
                continue
            if existing is None:
                registered.append(result["agent"])
            else:
                refreshed.append(result["agent"])
            registered_threads[thread_id] = {
                **dict(existing or {}),
                **result["agent"],
            }

        for thread_id, existing in registered_threads.items():
            if (
                existing.get("runtime") != "native"
                or thread_id in discovered_thread_ids
            ):
                continue
            project = projects_by_id.get(str(existing.get("project_id")))
            if project is None:
                continue
            inspection = inspect_native_session(
                thread_id,
                task_id=existing.get("current_task_id"),
            )
            inspected_project = matching_project(inspection.get("cwd"))
            if (
                inspected_project is None
                or str(inspected_project["project_id"])
                != str(project["project_id"])
            ):
                errors.append(
                    {
                        "thread_id": thread_id,
                        "error": (
                            "native session cwd cannot verify the "
                            "registered project ownership"
                        ),
                    }
                )
                continue
            try:
                state = normalized_native_state(
                    inspection,
                    existing,
                    project,
                )
            except (ClusterError, TeamError, OSError) as exc:
                errors.append(
                    {
                        "thread_id": thread_id,
                        "error": str(exc),
                    }
                )
                continue
            if not native_state_needs_refresh(existing, state):
                continue
            try:
                result = self.register_native_agent(
                    project["root_path"],
                    thread_id,
                    str(state["role"]),
                    str(existing["workstream_key"]),
                    actor=f"{actor}:{thread_id}",
                    task_id=state["task_id"],
                    display_name=str(existing.get("display_name") or thread_id),
                    model_provider=str(
                        existing.get("model_provider") or ""
                    ),
                    task_candidates=state["task_candidates"],
                    task_assignment_ambiguous=state[
                        "task_assignment_ambiguous"
                    ],
                    assignment_source=state["assignment_source"],
                    assignment_confidence=state[
                        "assignment_confidence"
                    ],
                    identity_source=state["identity_source"],
                    identity_confidence=state["identity_confidence"],
                )
            except (AgentPoolError, ClusterError, TeamError, OSError) as exc:
                errors.append(
                    {
                        "thread_id": thread_id,
                        "error": str(exc),
                    }
                )
                continue
            refreshed.append(result["agent"])
        return {
            "discovered": len(discovered),
            "max_age_hours": max_age_hours,
            "registered": registered,
            "registered_count": len(registered),
            "refreshed": refreshed,
            "refreshed_count": len(refreshed),
            "skipped_existing": skipped_existing,
            "skipped_unknown_project": skipped_unknown_project,
            "errors": errors,
        }

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
        try:
            for job in list(self.process_jobs.values()):
                job.close()
            self.process_jobs.clear()
            for process in list(self.processes.values()):
                try:
                    process.wait(timeout=5)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    pass
                for stream in (
                    process.stdin,
                    process.stdout,
                    process.stderr,
                ):
                    if stream is None or stream.closed:
                        continue
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass
            self.processes.clear()
            for pump in list(self.log_threads.values()):
                pump.join(timeout=5)
            self.log_threads.clear()
        finally:
            if self.runtime is not None:
                self.runtime.stop()

    def tick(self) -> dict[str, Any]:
        with self._tick_lock:
            return self._tick_locked()

    def _tick_locked(self) -> dict[str, Any]:
        tick_id = self._new_id("TICK")
        phases: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        failed_phases: list[str] = []
        skipped: list[dict[str, Any]] = []
        retry_used = 0
        phase_data: dict[str, Any] = {
            "worktree_recovery": [],
            "scheduled": [],
            "goals": [],
            "prepare_ready": False,
            "worker_alerts": 0,
            "goal_alerts": 0,
        }

        def normalize_errors(
            name: str,
            raw_errors: Any,
        ) -> list[dict[str, Any]]:
            if raw_errors is None:
                return []
            if isinstance(raw_errors, Mapping):
                candidates = [raw_errors]
            elif isinstance(raw_errors, (list, tuple)):
                candidates = list(raw_errors)
            else:
                candidates = [raw_errors]
            normalized: list[dict[str, Any]] = []
            for candidate in candidates:
                if isinstance(candidate, Mapping):
                    message = self._redact_error(
                        str(
                            candidate.get("redacted_error")
                            or candidate.get("error")
                            or candidate.get("message")
                            or f"{name} phase reported a failure"
                        )
                    )
                    error_code = str(
                        candidate.get("error_code")
                        or f"{name}_phase_failure"
                    )
                    source = str(candidate.get("source") or name)
                    detail = {
                        key: value
                        for key, value in candidate.items()
                        if key
                        not in {"redacted_error", "error", "message"}
                    }
                else:
                    message = self._redact_error(str(candidate))
                    error_code = f"{name}_phase_failure"
                    source = name
                    detail = {}
                normalized.append(
                    {
                        **detail,
                        "phase": name,
                        "source": source,
                        "error_code": error_code,
                        "redacted_error": message,
                    }
                )
            return normalized

        def append_phase(
            name: str,
            *,
            ok: bool,
            attempts: int,
            duration_ms: float,
            counts: Mapping[str, Any] | None = None,
            phase_errors: list[dict[str, Any]] | None = None,
            retry_suppressed: bool = False,
        ) -> None:
            normalized = list(phase_errors or [])
            first_error = normalized[0] if normalized else None
            phases.append(
                {
                    "name": name,
                    "ok": ok,
                    "degraded": not ok,
                    "attempts": attempts,
                    "duration_ms": round(duration_ms, 3),
                    "counts": dict(counts or {"count": 0}),
                    "error_code": (
                        "" if ok else str(
                            (first_error or {}).get("error_code")
                            or f"{name}_phase_failure"
                        )
                    ),
                    "redacted_error": (
                        "" if ok else str(
                            (first_error or {}).get("redacted_error") or ""
                        )
                    ),
                    "retry_suppressed": bool(retry_suppressed),
                }
            )
            if ok:
                return
            if name not in failed_phases:
                failed_phases.append(name)
            errors.extend(normalized)

        def record_failure(
            name: str,
            normalized: list[dict[str, Any]],
            attempts: int,
            *,
            allow_retry: bool = True,
        ) -> tuple[bool, bool, float]:
            first_error = normalized[0]
            signature = hashlib.sha256(
                (
                    f"{first_error['error_code']}\0"
                    f"{first_error['redacted_error']}"
                ).encode("utf-8")
            ).hexdigest()[:16]
            previous = self._phase_failure_state.get(name)
            same_signature = bool(
                previous
                and previous.get("signature") == signature
            )
            consecutive = (
                int(previous.get("failures") or 0) + 1
                if previous
                else 1
            )
            cross_tick_delay = (
                min(
                    TICK_CONTROL_BACKOFF_BASE_SECONDS
                    * (2 ** (consecutive - 1)),
                    TICK_CONTROL_BACKOFF_MAX_SECONDS,
                )
                if name == "control"
                else 0.0
            )
            self._phase_failure_state[name] = {
                "signature": signature,
                "failures": consecutive,
                "error_code": first_error["error_code"],
                "redacted_error": first_error["redacted_error"],
                "next_retry_at": (
                    time.monotonic() + cross_tick_delay
                    if cross_tick_delay
                    else 0.0
                ),
            }
            can_retry = bool(
                allow_retry
                and attempts < TICK_PHASE_MAX_ATTEMPTS
                and retry_used < TICK_RETRY_BUDGET
                and not same_signature
            )
            delay = min(
                TICK_PHASE_RETRY_BASE_SECONDS
                * (2 ** (consecutive - 1)),
                TICK_PHASE_RETRY_MAX_SECONDS,
            )
            return (
                can_retry,
                bool(previous and same_signature),
                delay,
            )

        def run_phase(
            name: str,
            action: Any,
            *,
            gate_blockers: list[str] | None = None,
        ) -> bool:
            nonlocal retry_used
            blockers = list(gate_blockers or [])
            if blockers:
                skipped.append(
                    {
                        "name": name,
                        "reason": "prerequisites_unavailable",
                        "blocked_by": blockers,
                    }
                )
                return False

            previous_failure = self._phase_failure_state.get(name)
            if name == "control" and previous_failure:
                next_retry_at = float(
                    previous_failure.get("next_retry_at") or 0
                )
                now = time.monotonic()
                if next_retry_at > now:
                    normalized = normalize_errors(
                        name,
                        {
                            "source": "phase_backoff",
                            "error_code": str(
                                previous_failure.get("error_code")
                                or "control_phase_failure"
                            ),
                            "redacted_error": str(
                                previous_failure.get("redacted_error")
                                or "control phase backoff active"
                            ),
                        },
                    )
                    append_phase(
                        name,
                        ok=False,
                        attempts=0,
                        duration_ms=0.0,
                        counts={
                            "count": 0,
                            "backoff_suppressed": True,
                            "retry_in_seconds": round(
                                next_retry_at - now,
                                3,
                            ),
                        },
                        phase_errors=normalized,
                        retry_suppressed=True,
                    )
                    return False

            started = time.perf_counter()
            attempts = 0
            while attempts < TICK_PHASE_MAX_ATTEMPTS:
                attempts += 1
                try:
                    result = action()
                    if isinstance(result, Mapping):
                        counts = dict(result)
                    else:
                        counts = {"count": int(result or 0)}
                    raw_errors = counts.pop("_errors", [])
                    degraded = bool(counts.pop("_degraded", False))
                    normalized = normalize_errors(name, raw_errors)
                    if normalized or degraded:
                        if not normalized:
                            normalized = normalize_errors(
                                name,
                                f"{name} phase reported degraded work",
                            )
                        can_retry, suppressed, delay = record_failure(
                            name,
                            normalized,
                            attempts,
                            # Worktree recovery may deliberately block a
                            # Task on its first uncertain result. Do not
                            # retry that returned error in the same tick.
                            allow_retry=name != "recover",
                        )
                        if can_retry:
                            retry_used += 1
                            time.sleep(delay)
                            continue
                        append_phase(
                            name,
                            ok=False,
                            attempts=attempts,
                            duration_ms=(
                                time.perf_counter() - started
                            ) * 1000,
                            counts=counts,
                            phase_errors=normalized,
                            retry_suppressed=suppressed,
                        )
                        return False
                    self._phase_failure_state.pop(name, None)
                    append_phase(
                        name,
                        ok=True,
                        attempts=attempts,
                        duration_ms=(
                            time.perf_counter() - started
                        ) * 1000,
                        counts=counts,
                    )
                    return True
                except Exception as exc:
                    normalized = normalize_errors(
                        name,
                        self._phase_error_record(name, exc),
                    )
                    can_retry, suppressed, delay = record_failure(
                        name,
                        normalized,
                        attempts,
                        allow_retry=name != "recover",
                    )
                    if can_retry:
                        retry_used += 1
                        time.sleep(delay)
                        continue
                    append_phase(
                        name,
                        ok=False,
                        attempts=attempts,
                        duration_ms=(
                            time.perf_counter() - started
                        ) * 1000,
                        phase_errors=normalized,
                        retry_suppressed=suppressed,
                    )
                    return False
            return False

        def settle() -> Mapping[str, Any]:
            return self._reconcile_jobs()

        def recover() -> Mapping[str, Any]:
            recovered = self.worktrees.recover_pending()
            annotated = [
                self._annotate_worktree_recovery(item)
                for item in recovered
            ]
            recovery_errors: list[dict[str, Any]] = []
            for item in annotated:
                if item.get("status") != "failed":
                    continue
                task_id = str(item.get("task_id") or "")
                if not task_id:
                    continue
                try:
                    manual_reason = (
                        "worktree recovery requires manual action: "
                        f"{item.get('manual_action') or item.get('redacted_error')}"
                    )
                    blocked = self._block_worktree_task_for_manual_action(
                        task_id,
                        manual_reason,
                    )
                    item["task_status"] = blocked["status"]
                    if blocked["status"] == "blocked":
                        item["task_blocked"] = True
                    elif blocked["status"] in {"running", "claimed"}:
                        recovery_errors.append(
                            {
                                "source": "worktree_recovery",
                                "task_id": task_id,
                                "operation_id": item.get("operation_id", ""),
                                "error_code": "worktree_foreign_claim_preserved",
                                "redacted_error": (
                                    "worktree recovery needs manual action, "
                                    "but the task is owned by an active claim"
                                ),
                                "manual_action": item.get("manual_action", ""),
                            }
                        )
                except Exception as exc:
                    recovery_errors.append(
                        {
                            **self._phase_error_record(
                                "recover",
                                exc,
                                source="worktree_task_block",
                            ),
                            "task_id": task_id,
                            "operation_id": item.get("operation_id", ""),
                        }
                    )
            phase_data["worktree_recovery"] = annotated
            recovery_errors.extend(
                {
                    "source": "worktree_recovery",
                    "task_id": item.get("task_id", ""),
                    "operation_id": item.get("operation_id", ""),
                    "error_code": item["error_code"],
                    "redacted_error": item["redacted_error"],
                    "manual_action": item.get("manual_action", ""),
                }
                for item in annotated
                if item.get("status") in {"failed", "deferred"}
            )
            return {
                "count": len(annotated),
                "completed": sum(
                    item.get("status") == "completed" for item in annotated
                ),
                "failed": sum(
                    item.get("status") == "failed" for item in annotated
                ),
                "deferred": sum(
                    item.get("status") == "deferred" for item in annotated
                ),
                "manual": len(recovery_errors),
                "_errors": recovery_errors,
            }

        def control() -> Mapping[str, Any]:
            control_errors: list[dict[str, Any]] = []
            try:
                reset_counts = self._apply_agent_reset_requests()
            except Exception as exc:
                reset_counts = {}
                control_errors.append(
                    {
                        **self._phase_error_record(
                            "control",
                            exc,
                            source="agent_reset",
                        ),
                        "phase": "control",
                    }
                )
            try:
                archive_counts = self._sync_project_agent_archive()
            except Exception as exc:
                archive_counts = {}
                control_errors.append(
                    {
                        **self._phase_error_record(
                            "control",
                            exc,
                            source="agent_archive",
                        ),
                        "phase": "control",
                    }
                )
            control_errors.extend(
                normalize_errors(
                    "control",
                    archive_counts.get("errors", []),
                )
            )
            phase_data["control_errors"] = control_errors
            return {
                "count": int(reset_counts.get("count", 0))
                + int(archive_counts.get("count", 0)),
                "agent_resets": int(reset_counts.get("count", 0)),
                "project_archives": int(
                    archive_counts.get("count", 0)
                ),
                "_errors": control_errors,
            }

        def prepare() -> Mapping[str, Any]:
            goals = self.list_goals(["planning", "active"])
            phase_data["goals"] = goals
            schedulable: list[dict[str, Any]] = []
            paused = 0
            prepare_errors: list[dict[str, Any]] = []
            for goal in goals:
                try:
                    if self._prepare_goal(goal):
                        schedulable.append(goal)
                except WorkflowEpochMismatch as exc:
                    self._pause_workflow_mismatch(goal, exc)
                    paused += 1
                except Exception as exc:
                    prepare_errors.append(
                        {
                            **self._phase_error_record(
                                "prepare",
                                exc,
                                source="prepare_goal",
                            ),
                            "goal_id": goal.get("goal_id"),
                        }
                    )
            phase_data["schedulable"] = schedulable
            phase_data["prepare_errors"] = prepare_errors
            phase_data["prepare_ready"] = True
            return {
                "count": len(goals),
                "schedulable": len(schedulable),
                "paused_epoch_mismatch": paused,
                "goal_errors": len(prepare_errors),
                "_errors": prepare_errors,
            }

        def dispatch() -> Mapping[str, Any]:
            scheduled = self._schedule_goals(
                list(phase_data.get("schedulable") or [])
            )
            phase_data["scheduled"] = scheduled
            return {"count": len(scheduled)}

        def health() -> Mapping[str, Any]:
            health_errors: list[dict[str, Any]] = []
            try:
                worker_health = self._check_worker_health()
            except Exception as exc:
                worker_health = {}
                health_errors.append(
                    self._phase_error_record(
                        "health",
                        exc,
                        source="worker_health",
                    )
                )
            try:
                goal_health = self._check_goal_stalls(
                    list(phase_data.get("goals") or []),
                    list(phase_data.get("scheduled") or []),
                )
            except Exception as exc:
                goal_health = {}
                health_errors.append(
                    self._phase_error_record(
                        "health",
                        exc,
                        source="goal_health",
                    )
                )
            phase_data["worker_alerts"] = int(
                worker_health.get("alerts", 0)
            )
            phase_data["goal_alerts"] = int(
                goal_health.get("count", 0)
            )
            return {
                "count": int(worker_health.get("count", 0))
                + int(goal_health.get("count", 0)),
                "workers": int(worker_health.get("count", 0)),
                "alerts": int(worker_health.get("alerts", 0)),
                "goals": int(goal_health.get("count", 0)),
                "_errors": health_errors,
            }

        settle_ok = run_phase("settle", settle)
        run_phase("recover", recover)
        run_phase("control", control)
        run_phase("prepare", prepare)
        dispatch_blockers: list[str] = []
        if not settle_ok:
            dispatch_blockers.append("settle")
        if not phase_data.get("prepare_ready"):
            dispatch_blockers.append("prepare")
        run_phase(
            "dispatch",
            dispatch,
            gate_blockers=dispatch_blockers,
        )
        run_phase("health", health)

        def publish() -> Mapping[str, Any]:
            phase_data["tick_at"] = utc_now()
            return {"count": 1}

        run_phase("publish", publish)
        completed_names = {phase["name"] for phase in phases}
        skipped_names = [
            name for name in TICK_PHASE_ORDER if name not in completed_names
        ]
        degraded = bool(failed_phases)
        failed_phase = failed_phases[0] if failed_phases else None
        first_error = errors[0]["redacted_error"] if errors else ""
        worktree_manual_actions = [
            {
                "operation_id": item.get("operation_id"),
                "task_id": item.get("task_id"),
                "error_code": item.get("error_code"),
                "manual_action": item.get("manual_action"),
            }
            for item in phase_data.get("worktree_recovery") or []
            if item.get("manual_action")
        ]
        health_summary = {
            "ok": not degraded,
            "degraded": degraded,
            "failed_phases": list(failed_phases),
            "degraded_phases": list(failed_phases),
            "skipped_phases": skipped_names,
            "errors": errors,
            "worker_alerts": int(phase_data.get("worker_alerts") or 0),
            "goal_alerts": int(phase_data.get("goal_alerts") or 0),
            "worktree_manual_actions": worktree_manual_actions,
        }
        retry = {
            "limit": TICK_RETRY_BUDGET,
            "used": retry_used,
            "exhausted": retry_used >= TICK_RETRY_BUDGET,
        }
        return {
            "ok": not degraded,
            "tick_id": tick_id,
            "tick_at": phase_data.get("tick_at") or utc_now(),
            "phase_order": list(TICK_PHASE_ORDER),
            "phases": phases,
            "failed_phase": failed_phase,
            "failed_phases": list(failed_phases),
            "degraded": degraded,
            "degraded_phases": list(failed_phases),
            "skipped_phases": skipped_names,
            "skips": skipped,
            "error": first_error,
            "error_code": (
                errors[0]["error_code"] if errors else ""
            ),
            "errors": errors,
            "retry": retry,
            "retry_used": retry_used,
            "retry_budget": TICK_RETRY_BUDGET,
            "health": health_summary,
            "scheduled": list(phase_data.get("scheduled") or []),
            "goals": len(phase_data.get("goals") or []),
            "worktree_recovery": list(
                phase_data.get("worktree_recovery") or []
            ),
        }

    def _phase_error_record(
        self,
        phase: str,
        exc: Exception,
        *,
        source: str | None = None,
    ) -> dict[str, str]:
        message = self._redact_error(str(exc))
        lowered = message.casefold()
        if "branch exists but worktree target is missing" in lowered:
            error_code = "worktree_branch_without_directory"
        elif "worktree target exists but branch/root do not match intent" in lowered:
            error_code = "worktree_target_mismatch"
        elif "repository root is missing" in lowered:
            error_code = "worktree_repository_missing"
        elif "repository lock" in lowered or "lock timeout" in lowered:
            error_code = "repository_lock_timeout"
        elif isinstance(exc, TimeoutError):
            error_code = "phase_timeout"
        elif isinstance(exc, OSError):
            error_code = "phase_os_error"
        elif isinstance(exc, AssertionError):
            error_code = "phase_assertion_error"
        else:
            error_code = f"{phase}_phase_failure"
        return {
            "source": source or phase,
            "error_code": error_code,
            "redacted_error": message,
        }

    def _annotate_worktree_recovery(
        self,
        item: Mapping[str, Any],
    ) -> dict[str, Any]:
        annotated = dict(item)
        operation_id = str(annotated.get("operation_id") or "")
        message = self._redact_error(
            str(
                annotated.get("error")
                or annotated.get("redacted_error")
                or ""
            )
        )
        task_id = str(annotated.get("task_id") or "")
        if operation_id and (not task_id or not message):
            with self.team.connection(write=False) as connection:
                operation = connection.execute(
                    """
                    SELECT task_id, error
                    FROM external_operations
                    WHERE operation_id = ?
                      AND operation_type = 'worktree_create'
                    """,
                    (operation_id,),
                ).fetchone()
            if operation is not None:
                task_id = task_id or str(operation["task_id"] or "")
                message = message or self._redact_error(
                    str(operation["error"] or "")
                )
        lowered = message.casefold()
        if "branch exists but worktree target is missing" in lowered:
            error_code = "worktree_branch_without_directory"
            manual_action = (
                "Inspect the recorded branch and target path. If both belong "
                "to the task, recreate the missing worktree with "
                "`git worktree add <path> <branch>`; if the branch is stale, "
                "delete it intentionally. Automatic deletion is disabled."
            )
        elif "worktree target exists but branch/root do not match intent" in lowered:
            error_code = "worktree_target_mismatch"
            manual_action = (
                "Inspect the target directory, repository root, and branch. "
                "Move or remove only the confirmed task-owned directory, then "
                "retry worktree creation. Automatic deletion is disabled."
            )
        elif "repository root is missing" in lowered:
            error_code = "worktree_repository_missing"
            manual_action = (
                "Restore or relocate the recorded repository root, then retry "
                "worktree recovery. No Git state was changed automatically."
            )
        elif annotated.get("status") == "deferred":
            error_code = "repository_lock_timeout"
            manual_action = ""
        else:
            error_code = "worktree_recovery_failed"
            manual_action = (
                "Inspect the external operation, task worktree pointer, and "
                "Git worktree list before retrying. Automatic deletion is "
                "disabled while the intended state is uncertain."
            )
        requires_manual_action = annotated.get("status") == "failed"
        annotated.update(
            {
                "task_id": task_id or None,
                "error_code": error_code,
                "redacted_error": message,
                "requires_manual_action": requires_manual_action,
                "manual_action": manual_action if requires_manual_action else "",
            }
        )
        annotated.pop("error", None)
        return annotated

    def _block_worktree_task_for_manual_action(
        self,
        task_id: str,
        reason: str,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.team.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = connection.execute(
                """
                SELECT status, claimed_by, claim_token
                FROM tasks
                WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
            if task is None:
                raise TeamError(f"task not found: {task_id}")
            status = str(task["status"])
            if status == "blocked":
                return {"status": "blocked", "changed": False}
            if task["claimed_by"] or task["claim_token"]:
                return {"status": "claimed", "changed": False}
            if status not in {"ready", "backlog"}:
                return {"status": status, "changed": False}
            updated = connection.execute(
                """
                UPDATE tasks
                SET status = 'blocked', blocker = ?, claimed_by = NULL,
                    claim_token = NULL, lease_until = NULL,
                    updated_at = ?, version = version + 1
                WHERE task_id = ?
                  AND status IN ('ready', 'backlog')
                  AND claimed_by IS NULL
                  AND claim_token IS NULL
                """,
                (reason, now, task_id),
            )
            if updated.rowcount != 1:
                return {"status": "claimed", "changed": False}
            self.team._event(
                connection,
                task_id,
                "blocked",
                "teamd",
                {"reason": reason},
            )
        return {"status": "blocked", "changed": True}

    def _sync_project_agent_archive(self) -> dict[str, Any]:
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
        processed = 0
        failed = 0
        errors: list[dict[str, Any]] = []
        for project in projects:
            try:
                self.agent_pool.archive_project(
                    str(project["project_id"]),
                    project["archived_at"] is not None,
                )
                processed += 1
            except Exception as exc:
                # A live Agent can be cleaned up after the current job releases.
                failed += 1
                errors.append(
                    {
                        **self._phase_error_record(
                            "control",
                            exc,
                            source="agent_archive",
                        ),
                        "project_id": str(project["project_id"]),
                    }
                )
                continue
        return {
            "count": processed + failed,
            "processed": processed,
            "failed": failed,
            "errors": errors,
        }

    def _prepare_goal(self, goal: dict[str, Any]) -> bool:
        self._require_workflow_epoch(goal)
        goal_id = goal["goal_id"]
        root = Path(goal["root_path"])
        tasks = self.team.list_tasks(root, goal_id=goal_id, include_done=True)
        if self._block_invalid_dependencies(goal_id, tasks):
            tasks = self.team.list_tasks(root, goal_id=goal_id, include_done=True)
        active_workers = self._active_worker_jobs(goal_id)
        active_lead = self._active_lead_job(goal_id)
        if (
            tasks
            and all(item["status"] in TERMINAL_TASK_STATUSES for item in tasks)
            and not active_workers
            and not active_lead
        ):
            if goal["status"] == "planning":
                self.set_goal_status(
                    goal_id,
                    "active",
                    reason="terminal reconciliation",
                )
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

    def _block_invalid_dependencies(
        self,
        goal_id: str,
        tasks: list[dict[str, Any]],
    ) -> bool:
        changed = False
        for task in tasks:
            if task["status"] not in {"ready", "backlog"}:
                continue
            invalid = [
                dependency
                for dependency in task.get("blocked_by") or []
                if dependency.get("status") == "canceled"
                or dependency.get("archived_at")
            ]
            if not invalid:
                continue
            details = ", ".join(
                f"{dependency['task_id']}({dependency.get('status') or 'unknown'})"
                for dependency in invalid
            )
            reason = f"dependency unavailable: {details}"
            try:
                self.team.block_task(
                    str(task["task_id"]),
                    actor="teamd",
                    reason=reason,
                )
            except TeamError:
                continue
            with self.team.connection() as connection:
                self._event(
                    connection,
                    goal_id,
                    "dependency_invalidated",
                    "teamd",
                    {
                        "task_id": task["task_id"],
                        "dependencies": invalid,
                        "reason": reason,
                    },
                )
            changed = True
        return changed

    def _check_goal_stalls(
        self,
        goals: list[dict[str, Any]],
        scheduled: list[dict[str, Any]],
    ) -> dict[str, Any]:
        scheduled_goal_ids = {
            str(job.get("goal_id"))
            for job in scheduled
            if job.get("goal_id")
        }
        global_active_jobs = self._active_job_count()
        paused = 0
        for candidate in goals:
            goal = self.get_goal(str(candidate["goal_id"]))
            if goal["status"] not in {"planning", "active"}:
                continue
            goal_id = str(goal["goal_id"])
            active_goal_jobs = self._active_goal_job_count(goal_id)
            if (
                goal_id in scheduled_goal_ids
                or active_goal_jobs > 0
                or global_active_jobs >= self.max_jobs
            ):
                self._reset_goal_stall(goal_id)
                continue
            tasks = self.team.list_tasks(
                goal["root_path"],
                goal_id=goal_id,
                include_done=True,
            )
            if any(
                task["status"] == "running"
                and not task.get("lease_expired")
                for task in tasks
            ):
                self._reset_goal_stall(goal_id)
                continue
            if any(
                dependency.get("status") in {"running", "review", "blocked"}
                for task in tasks
                for dependency in task.get("blocked_by") or []
            ):
                self._reset_goal_stall(goal_id)
                continue
            signature = self._goal_stall_signature(goal, tasks)
            previous_signature = str(goal.get("stall_signature") or "")
            previous_ticks = int(goal.get("stall_ticks") or 0)
            stall_ticks = (
                previous_ticks + 1
                if previous_signature == signature
                else 1
            )
            if stall_ticks >= self.goal_stall_ticks:
                self._pause_stalled_goal(
                    goal,
                    tasks,
                    signature,
                    stall_ticks,
                    active_goal_jobs=active_goal_jobs,
                    global_active_jobs=global_active_jobs,
                )
                paused += 1
                continue
            with self.team.connection() as connection:
                connection.execute(
                    """
                    UPDATE cluster_goals
                    SET stall_ticks = ?, stall_signature = ?, stalled_at = NULL
                    WHERE goal_id = ? AND status IN ('planning', 'active')
                    """,
                    (stall_ticks, signature, goal_id),
                )
        return {
            "count": len(goals),
            "paused": paused,
        }

    @staticmethod
    def _goal_stall_signature(
        goal: Mapping[str, Any],
        tasks: list[dict[str, Any]],
    ) -> str:
        payload = {
            "goal": {
                "status": goal.get("status"),
                "lead_generation": goal.get("lead_generation"),
                "lead_ticks": goal.get("lead_ticks"),
                "checkpoint_updated_at": goal.get("checkpoint_updated_at"),
            },
            "tasks": [
                {
                    "task_id": task.get("task_id"),
                    "status": task.get("status"),
                    "version": task.get("version"),
                    "claimed_by": task.get("claimed_by"),
                    "lease_until": task.get("lease_until"),
                    "blocked_by": task.get("blocked_by") or [],
                }
                for task in sorted(
                    tasks,
                    key=lambda item: str(item.get("task_id") or ""),
                )
            ],
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _reset_goal_stall(self, goal_id: str) -> None:
        with self.team.connection() as connection:
            connection.execute(
                """
                UPDATE cluster_goals
                SET stall_ticks = 0, stall_signature = '', stalled_at = NULL
                WHERE goal_id = ?
                  AND (
                    stall_ticks != 0
                    OR stall_signature != ''
                    OR stalled_at IS NOT NULL
                  )
                """,
                (goal_id,),
            )

    def _pause_stalled_goal(
        self,
        goal: Mapping[str, Any],
        tasks: list[dict[str, Any]],
        signature: str,
        stall_ticks: int,
        *,
        active_goal_jobs: int,
        global_active_jobs: int,
    ) -> None:
        now = utc_now()
        reason = (
            "goal stalled: no schedulable progress for "
            f"{stall_ticks} consecutive ticks"
        )
        counts: dict[str, int] = {}
        for task in tasks:
            status = str(task.get("status") or "unknown")
            counts[status] = counts.get(status, 0) + 1
        with self.team.connection() as connection:
            updated = connection.execute(
                """
                UPDATE cluster_goals
                SET status = 'paused', stall_ticks = ?, stall_signature = ?,
                    stalled_at = ?, last_error = ?, updated_at = ?
                WHERE goal_id = ? AND status IN ('planning', 'active')
                """,
                (
                    stall_ticks,
                    signature,
                    now,
                    reason,
                    now,
                    goal["goal_id"],
                ),
            )
            if updated.rowcount == 0:
                return
            self._event(
                connection,
                str(goal["goal_id"]),
                "goal_stalled",
                "teamd",
                {
                    "stall_ticks": stall_ticks,
                    "signature": signature,
                    "task_counts": counts,
                    "active_goal_jobs": active_goal_jobs,
                    "global_active_jobs": global_active_jobs,
                    "reason": reason,
                },
            )
            self._event(
                connection,
                str(goal["goal_id"]),
                "goal_status",
                "teamd",
                {"status": "paused", "reason": reason},
            )

    def _active_goal_job_count(self, goal_id: str) -> int:
        with self.team.connection(write=False) as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS active_jobs
                FROM cluster_jobs
                WHERE goal_id = ? AND status IN ('starting', 'running')
                """,
                (goal_id,),
            ).fetchone()
        return int(row["active_jobs"])

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
                return (
                    self._dispatch_worker(
                        goal,
                        task,
                        reserve_claim=True,
                    ),
                    True,
                    True,
                )
            except WorkflowEpochMismatch as exc:
                self.team.block_task(
                    task["task_id"],
                    actor="teamd",
                    reason=str(exc),
                )
                with self.team.connection() as connection:
                    self._event(
                        connection,
                        goal_id,
                        "workflow_epoch_mismatch",
                        "teamd",
                        {
                            "task_id": task["task_id"],
                            "task_epoch": task.get("workflow_epoch"),
                            "error": str(exc),
                        },
                    )
                return None, True, True
            except WorkerClaimConflict as exc:
                with self.team.connection() as connection:
                    self._event(
                        connection,
                        goal_id,
                        "dispatch_skipped",
                        "teamd",
                        {
                            "task_id": task["task_id"],
                            "reason": "claim_conflict",
                            "error": str(exc),
                        },
                    )
                return None, True, True
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
            except WorkflowEpochMismatch as exc:
                self._pause_workflow_mismatch(goal, exc)
                return None, True, False
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

    def _check_worker_health(self) -> dict[str, Any]:
        now = datetime.now(UTC)
        alerts = 0
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
            alerts += 1
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
        return {
            "count": len(jobs),
            "alerts": alerts,
        }

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

    def _dispatch_worker(
        self,
        goal: dict[str, Any],
        task: dict[str, Any],
        *,
        reserve_claim: bool = False,
    ) -> dict[str, Any]:
        role = str(task["owner_role"])
        if role not in self.team.ROLE_FILES:
            raise ClusterError(f"unsupported worker role: {role}")
        self._require_workflow_epoch(goal, task)
        job_id = self._new_id("J")
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
        actor = self._worker_actor(role, task["task_id"], job_id)
        if reserve_claim:
            lease_minutes = max(
                5,
                min((int(self.worker_timeout_seconds) + 59) // 60 + 5, 240),
            )
            try:
                self.team.claim_task(
                    task["task_id"],
                    actor=actor,
                    lease_minutes=lease_minutes,
                )
            except TeamError as exc:
                raise WorkerClaimConflict(
                    f"worker task claim conflict: {exc}"
                ) from exc
        checkpoint = self._checkpoint_context(goal)
        checkpoint_summary = _clip_text(
            checkpoint.get("summary"),
            3000,
        )
        assignment = f"""# Managed Assignment Contract
Use the stable codexTeamBoard Core tools. Do not spawn subagents or another team.
First call `team_write(operation="team_claim_task", arguments={{...}})` for the
current task using the Actor name below.
For every mutation, include a top-level `request_id` such as
`{job_id}:<operation>:<stable-step>`; retries of the same payload must reuse
that exact id, while distinct mutations must use different ids.
Work only on this task. Keep the claim token private. For jobs that may exceed
four minutes, call `team_write(operation="team_heartbeat", arguments={{...}})`
at least every three minutes. If your context approaches its limit, commit safe
progress, call `team_write(operation="team_block_task", arguments={{...}})` with
the reason `worker-context-near-limit`, and stop. Before exiting, either call
`team_write(operation="team_submit_task", arguments={{...}})` or call
`team_write(operation="team_block_task", arguments={{...}})` with a precise
blocker.

Return a compact structured summary:
- outcome
- changed files
- tests and results
- confirmed decisions
- risks or unresolved questions
- next action
Do not return raw logs or a full transcript.

# Current Assignment
- Goal ID: `{goal["goal_id"]}`
- Job ID: `{job_id}`
- Task ID: `{task["task_id"]}`
- Repository: `{goal["root_path"]}`
- Worktree: `{task.get("worktree_path") or "(none)"}`
- Actor name: `{actor}`

## Workstream Checkpoint
```text
{checkpoint_summary or "(none)"}
```
"""
        try:
            return self._start_job(
                goal,
                job_id=job_id,
                task_id=task["task_id"],
                kind="worker",
                role=role,
                cwd=cwd,
                developer_instructions=role_data["prompt"],
                turn_input=assignment,
                continuity_mode=str(task.get("continuity_mode") or "auto"),
                sandbox_mode=self._worker_sandbox_mode(task),
                actor=actor,
            )
        except Exception:
            if reserve_claim:
                self._release_worker_reservation(
                    task["task_id"],
                    goal["goal_id"],
                    actor,
                    reason="worker dispatch failed before the job could start",
                )
            raise

    @staticmethod
    def _worker_actor(role: str, task_id: str, job_id: str) -> str:
        return f"auto-{role}-{str(task_id).lower()}-{str(job_id).lower()}"

    def _release_worker_reservation(
        self,
        task_id: str,
        goal_id: str,
        actor: str,
        *,
        reason: str,
    ) -> bool:
        now = utc_now()
        with self.team.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = connection.execute(
                """
                SELECT status, claimed_by, claim_token, version
                FROM tasks WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
            if (
                task is None
                or task["status"] != "running"
                or task["claimed_by"] != actor
            ):
                return False
            active_job = connection.execute(
                """
                SELECT 1 FROM cluster_jobs
                WHERE task_id = ? AND kind = 'worker'
                  AND status IN ('starting', 'running')
                LIMIT 1
                """,
                (task_id,),
            ).fetchone()
            if active_job is not None:
                return False
            updated = connection.execute(
                """
                UPDATE tasks
                SET status = 'ready', claimed_by = NULL, claim_token = NULL,
                    lease_until = NULL, blocker = '', updated_at = ?,
                    version = version + 1
                WHERE task_id = ? AND status = 'running' AND version = ?
                  AND claimed_by = ? AND claim_token IS ?
                """,
                (
                    now,
                    task_id,
                    int(task["version"]),
                    actor,
                    task["claim_token"],
                ),
            )
            if updated.rowcount == 0:
                return False
            self._event(
                connection,
                goal_id,
                "worker_reservation_released",
                "teamd",
                {
                    "task_id": task_id,
                    "actor": actor,
                    "reason": reason,
                },
            )
            return True

    def _require_workflow_epoch(
        self,
        goal: Mapping[str, Any],
        task: Mapping[str, Any] | None = None,
    ) -> str:
        current = self.team.current_workflow_epoch(
            goal["root_path"],
            resolved_root=True,
        )
        expected = str(
            (task or goal).get("workflow_epoch") or ""
        ).strip()
        boundary = (
            f"task {task['task_id']}"
            if task is not None
            else f"goal {goal['goal_id']}"
        )
        if not expected or expected != current:
            raise WorkflowEpochMismatch(
                f"{boundary} workflow epoch {expected or '(unbound)'} "
                f"does not match current {current}; migrate it explicitly"
            )
        return current

    def _pause_workflow_mismatch(
        self,
        goal: Mapping[str, Any],
        error: WorkflowEpochMismatch,
    ) -> None:
        goal_id = str(goal["goal_id"])
        self.set_goal_status(
            goal_id,
            "paused",
            reason=str(error),
        )
        with self.team.connection() as connection:
            self._event(
                connection,
                goal_id,
                "workflow_epoch_mismatch",
                "teamd",
                {
                    "goal_epoch": goal.get("workflow_epoch"),
                    "error": str(error),
                },
            )

    @staticmethod
    def _worker_sandbox_mode(task: dict[str, Any]) -> str:
        return (
            "danger-full-access"
            if task.get("kind") == "write"
            else "workspace-write"
        )

    @staticmethod
    def _compact_task_digest(
        task: Mapping[str, Any],
        *,
        completed: bool,
    ) -> dict[str, Any]:
        common = {
            "task_id": task.get("task_id"),
            "title": task.get("title"),
            "kind": task.get("kind"),
            "status": task.get("status"),
            "owner_role": task.get("owner_role"),
            "priority": task.get("priority"),
            "workflow_epoch": task.get("workflow_epoch"),
        }
        if completed:
            return {
                **common,
                "commit_sha": task.get("commit_sha") or "",
                "result": _clip_text(task.get("result"), 1200),
                "tests": _clip_text(task.get("tests"), 600),
                "completed_at": task.get("completed_at"),
            }
        return {
            **common,
            "description": _clip_text(task.get("description"), 1800),
            "acceptance": _clip_text(task.get("acceptance"), 1000),
            "blocked_by": list(task.get("blocked_by") or []),
            "blocker": _clip_text(task.get("blocker"), 800),
            "claimed_by": task.get("claimed_by"),
            "branch": task.get("branch"),
            "worktree_path": task.get("worktree_path"),
            "result": _clip_text(task.get("result"), 1200),
            "tests": _clip_text(task.get("tests"), 600),
            "commit_sha": task.get("commit_sha") or "",
        }

    def _lead_board_digest(
        self,
        goal: Mapping[str, Any],
        tasks: list[dict[str, Any]],
        checkpoint: Mapping[str, Any],
    ) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for task in tasks:
            status = str(task.get("status") or "unknown")
            counts[status] = counts.get(status, 0) + 1
        active_tasks = [
            self._compact_task_digest(task, completed=False)
            for task in tasks
            if task.get("status") not in {"done", "canceled"}
        ]
        completed_tasks = sorted(
            (
                task
                for task in tasks
                if task.get("status") == "done"
            ),
            key=lambda task: str(
                task.get("completed_at") or task.get("updated_at") or ""
            ),
            reverse=True,
        )
        recent_completed = [
            self._compact_task_digest(task, completed=True)
            for task in completed_tasks[:8]
        ]
        return {
            "goal": {
                "goal_id": goal["goal_id"],
                "objective": goal["objective"],
                "autonomy": goal["autonomy"],
                "workstream_key": goal.get("workstream_key"),
                "workflow_epoch": goal.get("workflow_epoch"),
            },
            "counts": counts,
            "checkpoint": dict(checkpoint),
            "active_tasks": active_tasks,
            "recent_completed": recent_completed,
            "omitted_completed_count": max(0, len(completed_tasks) - 8),
            "detail_policy": (
                "This is a compact digest. Use team_read operation team_get_task "
                "and team_get_task_events only when full history is needed."
            ),
        }

    def _dispatch_lead(
        self,
        goal: dict[str, Any],
        tasks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self._require_workflow_epoch(goal)
        if int(goal["lead_ticks"]) >= 20:
            self.set_goal_status(goal["goal_id"], "failed", reason="lead tick limit exceeded")
            raise ClusterError("lead tick limit exceeded")
        checkpoint = self._checkpoint_context(goal)
        board = self._lead_board_digest(goal, tasks, checkpoint)
        authority_token = self.team.operator_authority()
        turn_input = f"""# Current Goal
{goal["objective"]}

## Compact Board Digest
```json
{json.dumps(board, ensure_ascii=False, indent=2)}
```

# Turn Rules
- Use goal_id = "{goal["goal_id"]}" when creating tasks.
- Use the compact digest by default.
- Read a full task or its events only when the digest is insufficient.
- At phase boundaries, call
  `team_write(operation="team_goal_set_checkpoint", arguments={...})` with
  durable decisions, completed work, tests, rejected approaches, risks, and
  next action.
- Do not copy raw logs or full transcripts into the checkpoint.

## Local Authority
- Privileged Team Board MCP operations require this local authority token:
  `{authority_token}`
- Keep it private. Never include it in checkpoints, task descriptions, logs,
  results, messages, or other durable text.
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
        job_id: str | None = None,
        task_id: str | None,
        kind: str,
        role: str,
        cwd: Path,
        developer_instructions: str,
        turn_input: str,
        actor: str = "",
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
        task = self.team.get_task(task_id) if task_id else None
        workflow_epoch_value = self._require_workflow_epoch(goal, task)
        job_id = job_id or self._new_id("J")
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
        prompt_hash = hashlib.sha256(
            (
                developer_instructions.rstrip()
                + "\n\n"
                + turn_input.lstrip()
            ).encode("utf-8")
        ).hexdigest()
        with self.team.connection() as connection:
            connection.execute(
                """
                INSERT INTO cluster_jobs(
                    job_id, goal_id, task_id, workflow_epoch, kind, role, actor, status,
                    attempt, log_path, output_path, runtime,
                    runtime_generation, error, started_at, correlation_id,
                    prompt_hash
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, 'starting',
                    ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    job_id,
                    goal["goal_id"],
                    task_id,
                    workflow_epoch_value,
                    kind,
                    role,
                    actor,
                    attempt,
                    str(log_path),
                    str(output_path),
                    selected_runtime,
                    runtime_generation,
                    runtime_error,
                    utc_now(),
                    job_id,
                    prompt_hash,
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
                    "actor": actor,
                    "attempt": attempt,
                    "runtime": selected_runtime,
                    "runtime_generation": runtime_generation,
                    "resume_thread_id": resume_thread_id,
                    "sandbox_mode": sandbox_mode,
                    "correlation_id": job_id,
                    "prompt_hash": prompt_hash,
                    "workflow_epoch": workflow_epoch_value,
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
                    retryable=exc.retryable,
                    failure_class=exc.failure_class,
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
                    actor=actor,
                    thread_action="fallback_exec",
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
            actor=actor,
            thread_action="resume" if resume_thread_id else "fresh_exec",
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
        actor: str,
        thread_action: str,
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
                "CODEX_HOME": str(self.agent_codex_home),
                "CODEX_TEAM_GOAL_ID": goal["goal_id"],
                "CODEX_TEAM_JOB_ID": job_id,
                "CODEX_TEAM_TASK_ID": task_id or "",
                "CODEX_TEAM_ROLE": role,
                "CODEX_TEAM_ACTOR": actor,
                "CODEX_TEAM_REPO_ROOT": goal["root_path"],
                "PYTHONUTF8": "1",
            }
        )
        creationflags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
        process: subprocess.Popen[str] | None = None
        process_job: ProcessJob | None = None
        process_record: dict[str, Any] = {}
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
            try:
                process_job = ProcessJob.attach(process)
            except Exception:
                if process.poll() is None:
                    raise
            process_record = process_identity(process.pid)
            if (
                os.name == "nt"
                and getattr(process, "_handle", None)
                and not process_record
                and process.poll() is None
            ):
                raise ClusterError(
                    f"could not verify {kind} process identity"
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
            self.processes[job_id] = process
            if process_job is not None:
                self.process_jobs[job_id] = process_job
            identity_fields = persisted_identity_fields(process_record)
            with self.team.connection() as connection:
                connection.execute(
                    """
                    UPDATE cluster_jobs
                    SET status = 'running', pid = ?,
                        process_creation_time = ?,
                        process_executable = ?,
                        process_instance_id = ?,
                        thread_id = ?,
                        thread_action = ?
                    WHERE job_id = ?
                    """,
                    (
                        process.pid,
                        identity_fields["process_creation_time"],
                        identity_fields["process_executable"],
                        identity_fields["process_instance_id"],
                        resume_thread_id,
                        thread_action,
                        job_id,
                    ),
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
        except Exception as exc:
            self.processes.pop(job_id, None)
            recorded_job = self.process_jobs.pop(job_id, None)
            if recorded_job is not None:
                process_job = recorded_job
            if process is not None:
                if process_job is not None:
                    process_job.close()
                else:
                    try:
                        process.kill()
                    except (OSError, ValueError):
                        pass
                try:
                    process.wait(timeout=5)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    pass
                for stream in (
                    process.stdin,
                    process.stdout,
                    process.stderr,
                ):
                    if stream is None or stream.closed:
                        continue
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass
            pump = self.log_threads.pop(job_id, None)
            if pump is not None:
                pump.join(timeout=5)
            safe_error = self._redact_error(str(exc))
            try:
                with self.team.connection() as connection:
                    if self._settle_job_row(
                        connection,
                        job_id,
                        status="failed",
                        error=safe_error,
                        exit_code=None,
                        thread_action=thread_action,
                    ):
                        self._event(
                            connection,
                            goal["goal_id"],
                            "job_start_failed",
                            "teamd",
                            {"job_id": job_id, "error": safe_error},
                        )
            except Exception as settlement_exc:
                settlement_error = self._redact_error(str(settlement_exc))
                safe_error = (
                    f"{safe_error}; failed to settle job: {settlement_error}"
                )
            raise ClusterError(
                f"could not start {kind} process: {safe_error}"
            ) from exc
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
            "thread_action": thread_action,
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
        retryable: bool = False,
        failure_class: str = "permanent",
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
                    process_creation_time = NULL,
                    process_executable = NULL,
                    process_instance_id = NULL,
                    thread_id = NULL, agent_id = NULL, turn_id = NULL,
                    runtime_generation = NULL, cache_profile = '',
                    thread_action = 'fallback_exec', error = ?
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
                "retryable": bool(retryable),
                "failure_class": str(failure_class),
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

    @staticmethod
    def _app_server_error_is_transient(exc: BaseException) -> bool:
        if isinstance(exc, (AppServerProtocolError, AppServerRPCError)):
            return False
        return isinstance(exc, TRANSIENT_APP_SERVER_ERRORS)

    def _runtime_health(self, runtime: Any) -> Mapping[str, Any]:
        try:
            health = runtime.health()
        except Exception:
            return {}
        return health if isinstance(health, Mapping) else {}

    def _runtime_process_identity(self, runtime: Any) -> dict[str, Any]:
        health = self._runtime_health(runtime)
        record = identity_record(health.get("process_identity"))
        if record:
            return record
        pid = health.get("pid")
        try:
            return process_identity(int(pid or 0))
        except (TypeError, ValueError):
            return {}

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

    def _call_app_server_rpc(
        self,
        runtime: Any,
        *,
        job_id: str,
        operation: str,
        request: Any,
    ) -> Any:
        before = self._current_runtime_generation(runtime)
        try:
            result = request()
        except Exception as exc:
            after = self._current_runtime_generation(runtime)
            if (
                before is not None
                and after is not None
                and after != before
            ):
                self._isolate_app_server_generation_delta(
                    runtime,
                    job_id=job_id,
                    operation=operation,
                    previous_generation=before,
                    current_generation=after,
                )
                raise _AppServerGenerationDeltaError(
                    operation,
                    before,
                    after,
                ) from exc
            raise
        after = self._current_runtime_generation(runtime)
        if before is not None and after is not None and after != before:
            self._isolate_app_server_generation_delta(
                runtime,
                job_id=job_id,
                operation=operation,
                previous_generation=before,
                current_generation=after,
            )
        return result

    def _isolate_app_server_generation_delta(
        self,
        runtime: Any,
        *,
        job_id: str,
        operation: str,
        previous_generation: int,
        current_generation: int,
    ) -> None:
        generation = self._current_runtime_generation(runtime)
        self._record_app_turn_generation(
            job_id,
            generation if generation is not None else current_generation,
        )
        self._lose_other_app_server_jobs(
            current_job_id=job_id,
            reason=(
                f"App Server runtime generation changed from "
                f"{previous_generation} to "
                f"{generation if generation is not None else current_generation}"
                f" during {operation}"
            ),
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

        def guarded_start() -> str:
            return self._call_app_server_rpc(
                runtime,
                job_id=job_id,
                operation="thread/start",
                request=start,
            )

        try:
            return guarded_start()
        except _AppServerGenerationDeltaError as first_error:
            first_failure = self._app_server_failure(
                f"thread/start failed: "
                f"{self._redact_error(str(first_error))}",
                runtime,
            )
            try:
                return guarded_start()
            except Exception as second_error:
                raise _AppServerJobStartError(
                    self._app_server_failure(
                        f"{first_failure}; fresh-thread retry after runtime "
                        f"generation change failed: "
                        f"{self._redact_error(str(second_error))}",
                        runtime,
                    )
                ) from second_error
        except Exception as first_error:
            first_failure = self._app_server_failure(
                f"thread/start failed: "
                f"{self._redact_error(str(first_error))}",
                runtime,
            )
            healthy = self._health_is_healthy(self._runtime_health(runtime))
            if (
                self._app_server_error_is_transient(first_error)
                and healthy
            ):
                last_failure: BaseException = first_error
                for _ in range(1, APP_SERVER_DELIVERY_MAX_ATTEMPTS):
                    try:
                        return guarded_start()
                    except _AppServerGenerationDeltaError as retry_error:
                        raise _AppServerJobStartError(
                            self._app_server_failure(
                                f"{first_failure}; thread/start transient "
                                f"retry observed a runtime generation "
                                f"change: "
                                f"{self._redact_error(str(retry_error))}",
                                runtime,
                            ),
                            retryable=True,
                            failure_class="transient",
                        ) from retry_error
                    except Exception as retry_error:
                        last_failure = retry_error
                        if not self._app_server_error_is_transient(
                            retry_error
                        ):
                            raise _AppServerJobStartError(
                                self._app_server_failure(
                                    f"{first_failure}; thread/start retry "
                                    f"failed permanently: "
                                    f"{self._redact_error(str(retry_error))}",
                                    runtime,
                                ),
                                failure_class="permanent",
                            ) from retry_error
                raise _AppServerJobStartError(
                    self._app_server_failure(
                        f"{first_failure}; thread/start transient retry "
                        f"failed: {self._redact_error(str(last_failure))}",
                        runtime,
                    ),
                    retryable=True,
                    failure_class="transient",
                ) from last_failure
            if healthy:
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
                return guarded_start()
            except Exception as second_error:
                raise _AppServerJobStartError(
                    self._app_server_failure(
                        f"{first_failure}; retry after restart failed: "
                        f"{self._redact_error(str(second_error))}",
                        runtime,
                    )
                ) from second_error

    def _resume_thread_and_retry_app_turn(
        self,
        runtime: Any,
        thread_id: str,
        turn_input: str,
        *,
        cwd: Path,
        sandbox_mode: str,
        job_id: str,
        first_failure: str,
    ) -> dict[str, Any]:
        try:
            restored_thread_id = self._call_app_server_rpc(
                runtime,
                job_id=job_id,
                operation="thread/resume",
                request=lambda: runtime.resume_thread(thread_id),
            )
        except Exception as resume_error:
            raise _AppServerJobStartError(
                self._app_server_failure(
                    f"{first_failure}; thread reload after replacement failed: "
                    f"{self._redact_error(str(resume_error))}",
                    runtime,
                )
            ) from resume_error
        try:
            return self._call_app_server_rpc(
                runtime,
                job_id=job_id,
                operation="turn/start retry",
                request=lambda: runtime.start_turn(
                    restored_thread_id,
                    turn_input,
                    cwd=cwd,
                    sandbox_mode=sandbox_mode,
                    model=self.model or None,
                ),
            )
        except Exception as second_error:
            raise _AppServerJobStartError(
                self._app_server_failure(
                    f"{first_failure}; retry after replacement failed: "
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

        def guarded_start() -> dict[str, Any]:
            return self._call_app_server_rpc(
                runtime,
                job_id=job_id,
                operation="turn/start",
                request=start,
            )

        try:
            return guarded_start()
        except _AppServerGenerationDeltaError as first_error:
            first_failure = self._app_server_failure(
                f"turn/start failed: "
                f"{self._redact_error(str(first_error))}",
                runtime,
            )
            return self._resume_thread_and_retry_app_turn(
                runtime,
                thread_id,
                turn_input,
                cwd=cwd,
                sandbox_mode=sandbox_mode,
                job_id=job_id,
                first_failure=first_failure,
            )
        except Exception as first_error:
            first_failure = self._app_server_failure(
                f"turn/start failed: "
                f"{self._redact_error(str(first_error))}",
                runtime,
            )
            if (
                self._app_server_error_is_transient(first_error)
                and self._health_is_healthy(self._runtime_health(runtime))
            ):
                last_failure: BaseException = first_error
                for _ in range(1, APP_SERVER_DELIVERY_MAX_ATTEMPTS):
                    try:
                        return guarded_start()
                    except _AppServerGenerationDeltaError as retry_error:
                        return self._resume_thread_and_retry_app_turn(
                            runtime,
                            thread_id,
                            turn_input,
                            cwd=cwd,
                            sandbox_mode=sandbox_mode,
                            job_id=job_id,
                            first_failure=(
                                f"{first_failure}; transient retry observed "
                                f"a runtime generation change: "
                                f"{self._redact_error(str(retry_error))}"
                            ),
                        )
                    except Exception as retry_error:
                        last_failure = retry_error
                        if not self._app_server_error_is_transient(
                            retry_error
                        ):
                            raise _AppServerJobStartError(
                                self._app_server_failure(
                                    f"{first_failure}; turn/start retry "
                                    f"failed permanently: "
                                    f"{self._redact_error(str(retry_error))}",
                                    runtime,
                                ),
                                failure_class="permanent",
                            ) from retry_error
                raise _AppServerJobStartError(
                    self._app_server_failure(
                        f"{first_failure}; turn/start transient retry "
                        f"failed: {self._redact_error(str(last_failure))}",
                        runtime,
                    ),
                    retryable=True,
                    failure_class="transient",
                ) from last_failure
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
            return self._resume_thread_and_retry_app_turn(
                runtime,
                thread_id,
                turn_input,
                cwd=cwd,
                sandbox_mode=sandbox_mode,
                job_id=job_id,
                first_failure=first_failure,
            )

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
        developer_instructions, cache_profile = _agent_cache_profile(
            goal,
            role,
            developer_instructions,
            cwd=cwd,
            sandbox_mode=sandbox_mode,
            runtime_version=__version__,
            runtime_identity=(
                f"{self.agent_runtime}:" + " ".join(self.agent_command)
            ),
        )
        if configured_surface() != "core":
            # Compatibility surfaces are for administration and migration;
            # their changing tool declarations must not be reused by Agents.
            continuity_mode = "fresh"
        workstream_key = str(goal.get("workstream_key") or goal["goal_id"])
        task_kind = "read" if kind == "lead" else str(
            self.team.get_task(task_id)["kind"]
        )
        agent: dict[str, Any] | None = None
        thread_id = ""
        thread_action = "fresh"
        try:
            agent = self.agent_pool.select_agent(
                str(goal["project_id"]),
                role,
                workstream_key,
                continuity_mode,
                task_kind,
                self.model,
                self.model_provider,
                profile=cache_profile,
            )
            if agent is not None:
                try:
                    thread_id = self._call_app_server_rpc(
                        runtime,
                        job_id=job_id,
                        operation="thread/resume",
                        request=lambda: runtime.resume_thread(
                            str(agent["thread_id"])
                        ),
                    )
                    thread_action = "resume"
                except Exception as exc:
                    reason = self._app_server_failure(
                        f"thread resume failed: "
                        f"{self._redact_error(str(exc))}",
                        runtime,
                    )
                    if isinstance(exc, _AppServerGenerationDeltaError):
                        reason += (
                            "; runtime generation changed, so the replacement "
                            "process will use a fresh thread"
                        )
                    elif not self._health_is_healthy(
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
                    profile=cache_profile,
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
            runtime_process_identity = self._runtime_process_identity(runtime)
            runtime_identity_fields = persisted_identity_fields(
                runtime_process_identity
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
                        runtime_generation = ?, cache_profile = ?,
                        thread_action = ?, pid = ?,
                        process_creation_time = ?,
                        process_executable = ?,
                        process_instance_id = ?
                    WHERE job_id = ?
                    """,
                    (
                        thread_id,
                        agent["agent_id"],
                        runtime_generation,
                        cache_profile,
                        thread_action,
                        runtime_process_identity.get("pid"),
                        runtime_identity_fields["process_creation_time"],
                        runtime_identity_fields["process_executable"],
                        runtime_identity_fields["process_instance_id"],
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
                    "cache_profile": cache_profile,
                    "thread_action": thread_action,
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
        active_process_identity = self._runtime_process_identity(runtime)
        active_identity_fields = persisted_identity_fields(
            active_process_identity
        )
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
                    runtime_generation = COALESCE(?, runtime_generation),
                    pid = COALESCE(?, pid),
                    process_creation_time = COALESCE(?, process_creation_time),
                    process_executable = COALESCE(?, process_executable),
                    process_instance_id = COALESCE(?, process_instance_id)
                WHERE job_id = ?
                """,
                (
                    turn_id,
                    active_generation,
                    active_process_identity.get("pid"),
                    active_identity_fields["process_creation_time"],
                    active_identity_fields["process_executable"],
                    active_identity_fields["process_instance_id"],
                    job_id,
                ),
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
            "cache_profile": cache_profile,
            "thread_action": thread_action,
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

    def _apply_agent_reset_requests(self) -> dict[str, Any]:
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
            return {"count": 0, "applied": 0}

        def unsubscribe(thread_id: str) -> None:
            if self.runtime is None:
                return
            self.runtime.unsubscribe_thread(thread_id)

        applied = self.agent_pool.apply_reset_requests(unsubscribe)
        return {"count": len(applied), "applied": len(applied)}

    def _reconcile_jobs(self) -> dict[str, Any]:
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
        errors: list[dict[str, Any]] = []
        processed = 0
        for row in rows:
            job = dict(row)
            try:
                self._reconcile_job(job)
                processed += 1
            except Exception as exc:
                errors.append(
                    {
                        "source": "job_settlement",
                        "error_code": "job_settlement_failed",
                        "job_id": str(job.get("job_id") or ""),
                        "goal_id": str(job.get("goal_id") or ""),
                        "task_id": str(job.get("task_id") or ""),
                        "kind": str(job.get("kind") or ""),
                        "runtime": str(job.get("runtime") or ""),
                        "error_type": type(exc).__name__,
                        "redacted_error": self._redact_error(str(exc)),
                    }
                )
        result: dict[str, Any] = {
            "count": len(rows),
            "processed": processed,
            "deferred": len(errors),
            "errors": errors,
        }
        if errors:
            result["_errors"] = errors
        return result

    def _reconcile_job(self, job: dict[str, Any]) -> None:
        if job.get("runtime") == "app_server":
            self._reconcile_app_server_job(job)
            return
        process = self.processes.get(job["job_id"])
        if self._job_timed_out(job):
            terminated = self._terminate_exec_job(job, exit_code=-9)
            if (
                process is None
                and persisted_process_is_running(job)
                and not terminated
            ):
                return
            self._finish_job(job, -9, forced_error="job timeout")
            return
        exit_code = process.poll() if process is not None else None
        if process is not None and exit_code is None:
            return
        if process is None:
            if persisted_process_is_running(job):
                if not self._terminate_exec_job(job, exit_code=-9):
                    return
            self._finish_job(
                job,
                exit_code,
                forced_error=(
                    "exec job process is not owned by this daemon instance"
                ),
                forced_status="lost",
            )
            return
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
        with self._app_state_lock:
            state = dict(self.app_turns.get(job["job_id"], {}))
        usage = self._app_job_usage(job, state)
        usage_has_evidence = _usage_has_evidence(usage)
        usage_source = (
            "cluster_agents.last_usage+runtime_raw_turn"
            if usage_has_evidence
            else None
        )
        telemetry_complete = (
            _usage_is_telemetry_complete(usage)
            if usage_has_evidence
            else False
        )
        settled = False
        with self.team.connection() as connection:
            settled = self._settle_job_row(
                connection,
                job["job_id"],
                status="lost",
                error=reason,
                exit_code=job.get("exit_code"),
                thread_id=job.get("thread_id"),
                usage=usage,
                usage_source=usage_source,
                telemetry_complete=telemetry_complete,
                usage_missing_fields=_usage_missing_fields_json(usage),
            )
            if settled:
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
                        "usage_source": usage_source,
                    },
                )
        if not settled:
            return
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
            current_state = self.app_turns.get(str(job.get("job_id")), {})
            already_requested = bool(current_state.get("interrupt_requested"))
            if not already_requested and current_state:
                current_state["interrupt_requested"] = True
                current_state["interrupt_reason"] = reason
            state = dict(current_state)
        self._persist_app_job_usage(job, state)
        if not thread_id or not turn_id or already_requested or self.runtime is None:
            return
        try:
            self.runtime.interrupt_turn(thread_id, turn_id)
        except Exception as exc:
            event_goal_id = str(job.get("goal_id") or "")
            if not event_goal_id:
                return
            with self.team.connection() as connection:
                self._event(
                    connection,
                    event_goal_id,
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

    def _persist_app_job_usage(
        self,
        job: Mapping[str, Any],
        state: Mapping[str, Any] | None = None,
    ) -> bool:
        """Persist available App Server usage before an interrupted terminal write."""

        job_id = str(job.get("job_id") or "")
        if not job_id:
            return False
        try:
            with self.team.connection(write=False) as connection:
                row = connection.execute(
                    """
                    SELECT *
                    FROM cluster_jobs
                    WHERE job_id = ? AND status IN ('starting', 'running')
                    """,
                    (job_id,),
                ).fetchone()
            if row is None:
                return False
            current_job = dict(row)
            usage_state: dict[str, Any] = {}
            if isinstance(state, Mapping):
                if "usage" in state:
                    usage_state["usage"] = state["usage"]
                if "first_usage" in state:
                    usage_state["first_usage"] = state["first_usage"]
            log_state = _app_usage_state_from_log(current_job.get("log_path"))
            if log_state:
                usage_state.update(log_state)
            elif not _usage_has_any_evidence(usage_state.get("usage")):
                return False

            usage = self._app_job_usage(
                {**current_job, **dict(job)},
                usage_state,
            )
            if not _usage_has_any_evidence(usage):
                return False
            existing_nonzero = any(
                int(current_job.get(field) or 0) > 0
                for field in (
                    "input_tokens",
                    "cached_input_tokens",
                    "uncached_input_tokens",
                    "output_tokens",
                )
            )
            recovered_nonzero = any(
                int(usage.get(field) or 0) > 0
                for field in (
                    "input_tokens",
                    "cached_input_tokens",
                    "uncached_input_tokens",
                    "output_tokens",
                )
            )
            if existing_nonzero and not recovered_nonzero:
                return False

            telemetry_complete = _usage_is_telemetry_complete(usage)
            usage_source = (
                "app_server.token_usage"
                if _usage_has_evidence(usage)
                else None
            )
            missing_fields = _usage_missing_fields_json(usage)
            with self.team.connection() as connection:
                updated = connection.execute(
                    """
                    UPDATE cluster_jobs
                    SET input_tokens = COALESCE(?, input_tokens),
                        cached_input_tokens = COALESCE(?, cached_input_tokens),
                        uncached_input_tokens = COALESCE(
                            ?, uncached_input_tokens
                        ),
                        output_tokens = COALESCE(?, output_tokens),
                        first_input_tokens = COALESCE(
                            ?, first_input_tokens
                        ),
                        first_cached_input_tokens = COALESCE(
                            ?, first_cached_input_tokens
                        ),
                        first_uncached_input_tokens = COALESCE(
                            ?, first_uncached_input_tokens
                        ),
                        context_tokens = COALESCE(?, context_tokens),
                        model_context_window = COALESCE(
                            ?, model_context_window
                        ),
                        cache_write_input_tokens = COALESCE(
                            ?, cache_write_input_tokens
                        ),
                        reasoning_output_tokens = COALESCE(
                            ?, reasoning_output_tokens
                        ),
                        usage_source = COALESCE(?, usage_source),
                        telemetry_complete = CASE
                            WHEN ? THEN 1
                            ELSE telemetry_complete
                        END,
                        usage_missing_fields = COALESCE(
                            ?, usage_missing_fields
                        )
                    WHERE job_id = ? AND status IN ('starting', 'running')
                    """,
                    (
                        usage.get("input_tokens"),
                        usage.get("cached_input_tokens"),
                        usage.get("uncached_input_tokens"),
                        usage.get("output_tokens"),
                        usage.get("first_input_tokens"),
                        usage.get("first_cached_input_tokens"),
                        usage.get("first_uncached_input_tokens"),
                        usage.get("context_tokens"),
                        usage.get("model_context_window"),
                        usage.get("cache_write_input_tokens"),
                        usage.get("reasoning_output_tokens"),
                        usage_source,
                        int(telemetry_complete),
                        missing_fields,
                        job_id,
                    ),
                )
            return updated.rowcount > 0
        except Exception:
            return False

    def _app_job_usage(
        self,
        job: Mapping[str, Any],
        state: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not _usage_state_has_evidence(state):
            log_state = _app_usage_state_from_log(job.get("log_path"))
            if log_state:
                state = {**state, **log_state}

        def values(value: Any) -> Mapping[str, Any]:
            return value if isinstance(value, Mapping) else {}

        usage = values(state.get("usage"))
        total = (
            values(usage.get("total"))
            if "total" in usage
            else usage
        )
        last_value = usage.get("last")
        last = values(last_value)
        first_usage = values(state.get("first_usage")) or usage
        first_total = (
            values(first_usage.get("total"))
            if "total" in first_usage
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
                prior = connection.execute(
                    """
                    SELECT
                        COALESCE(SUM(input_tokens), 0) AS prior_input_tokens,
                        COALESCE(SUM(cached_input_tokens), 0)
                            AS prior_cached_input_tokens,
                        COALESCE(SUM(output_tokens), 0)
                            AS prior_output_tokens
                    FROM cluster_jobs
                    WHERE agent_id = ?
                      AND job_id <> ?
                      AND status = 'completed'
                    """,
                    (agent_id, str(job.get("job_id") or "")),
                ).fetchone()
            if prior is not None:
                baseline_input = max(0, int(prior["prior_input_tokens"] or 0))
                baseline_cached = max(
                    0,
                    int(prior["prior_cached_input_tokens"] or 0),
                )
                baseline_output = max(
                    0,
                    int(prior["prior_output_tokens"] or 0),
                )

        def snapshot_values(
            cumulative: Mapping[str, Any],
            last_present: bool,
            per_turn: Mapping[str, Any],
            *,
            subtract_baseline: bool,
        ) -> tuple[
            int | None,
            int | None,
            int | None,
            int | None,
            int | None,
            int | None,
            frozenset[str],
        ]:
            cumulative_values = normalize_usage(cumulative)
            per_turn_values = normalize_usage(per_turn)

            def has_cost(values: Mapping[str, Any]) -> bool:
                return any(
                    values.get(field) is not None
                    for field in (
                        "input_tokens",
                        "cached_input_tokens",
                        "uncached_input_tokens",
                        "cache_write_input_tokens",
                        "output_tokens",
                        "reasoning_output_tokens",
                    )
                )

            use_per_turn = (
                not has_cost(cumulative_values)
                and last_present
                and (
                    has_cost(per_turn_values)
                    or (
                        not usage_has_any_evidence(cumulative)
                        and usage_has_any_evidence(per_turn)
                    )
                )
            )
            selected_raw = per_turn if use_per_turn else cumulative
            fallback_raw = cumulative if use_per_turn else per_turn
            selected = per_turn_values if use_per_turn else cumulative_values
            fallback = cumulative_values if use_per_turn else per_turn_values

            input_count = selected.get("input_tokens")
            cached_count = selected.get("cached_input_tokens")
            output_count = selected.get("output_tokens")
            if use_per_turn or not subtract_baseline:
                baseline_input_count = 0
                baseline_cached_count = 0
                baseline_output_count = 0
            else:
                baseline_input_count = baseline_input
                baseline_cached_count = baseline_cached
                baseline_output_count = baseline_output
            if input_count is not None:
                input_count = max(
                    0,
                    input_count - baseline_input_count,
                )
            if cached_count is not None:
                cached_count = max(
                    0,
                    cached_count - baseline_cached_count,
                )
                if input_count is not None:
                    cached_count = min(cached_count, input_count)
            if output_count is not None:
                output_count = max(
                    0,
                    output_count - baseline_output_count,
                )
            context_count = selected.get("context_tokens")
            if context_count is None:
                context_count = fallback.get("context_tokens")
            cache_write = selected.get("cache_write_input_tokens")
            if cache_write is None:
                cache_write = fallback.get("cache_write_input_tokens")
            reasoning_output = selected.get("reasoning_output_tokens")
            if reasoning_output is None:
                reasoning_output = fallback.get("reasoning_output_tokens")
            present_fields = {
                field
                for field in (
                    "input_tokens",
                    "cached_input_tokens",
                    "output_tokens",
                )
                if usage_field_is_present(selected_raw, field)
            }
            for field in (
                "context_tokens",
                "cache_write_input_tokens",
                "reasoning_output_tokens",
            ):
                if (
                    usage_field_is_present(selected_raw, field)
                    or usage_field_is_present(fallback_raw, field)
                ):
                    present_fields.add(field)
            if usage_field_is_present(
                selected_raw,
                "uncached_input_tokens",
            ) or {
                "input_tokens",
                "cached_input_tokens",
            }.issubset(present_fields):
                present_fields.add("uncached_input_tokens")
            return (
                input_count,
                cached_count,
                output_count,
                context_count,
                cache_write,
                reasoning_output,
                frozenset(present_fields),
            )

        (
            input_tokens,
            cached_tokens,
            output_tokens,
            context_tokens,
            cache_write,
            reasoning_output,
            present_fields,
        ) = snapshot_values(
            total,
            isinstance(last_value, Mapping),
            last,
            subtract_baseline=True,
        )
        (
            first_input,
            first_cached,
            _,
            _,
            _,
            _,
            first_present_fields,
        ) = snapshot_values(
            first_total,
            isinstance(first_last_value, Mapping),
            first_last,
            subtract_baseline=True,
        )
        raw_window = next(
            (
                value
                for value in (
                    usage.get("modelContextWindow"),
                    usage.get("model_context_window"),
                    total.get("modelContextWindow"),
                    total.get("model_context_window"),
                    last.get("modelContextWindow"),
                    last.get("model_context_window"),
                )
                if value is not None
            ),
            None,
        )
        try:
            model_window = (
                max(0, int(raw_window))
                if raw_window not in {None, 0}
                else None
            )
        except (TypeError, ValueError):
            model_window = None
        if model_window is not None:
            present_fields = present_fields | {"model_context_window"}
        output_present_fields = set(present_fields)
        for final_field, snapshot_field in (
            ("first_input_tokens", "input_tokens"),
            ("first_cached_input_tokens", "cached_input_tokens"),
            ("first_uncached_input_tokens", "uncached_input_tokens"),
        ):
            if snapshot_field in first_present_fields:
                output_present_fields.add(final_field)

        result: dict[str, Any] = {
            "input_tokens": input_tokens,
            "cached_input_tokens": cached_tokens,
            "uncached_input_tokens": (
                input_tokens - cached_tokens
                if input_tokens is not None and cached_tokens is not None
                else None
            ),
            "output_tokens": output_tokens,
            "first_input_tokens": first_input,
            "first_cached_input_tokens": first_cached,
            "first_uncached_input_tokens": (
                first_input - first_cached
                if first_input is not None and first_cached is not None
                else None
            ),
            "context_tokens": context_tokens,
            "model_context_window": model_window,
            "cache_write_input_tokens": cache_write,
            "reasoning_output_tokens": reasoning_output,
        }
        missing_fields = tuple(
            field
            for field in JOB_USAGE_FIELDS
            if (
                field not in output_present_fields
                or result.get(field) is None
            )
        )
        result["usage_present_fields"] = tuple(
            sorted(output_present_fields)
        )
        result["usage_missing_fields"] = missing_fields
        result["telemetry_complete"] = all(
            field in output_present_fields
            and result.get(field) is not None
            for field in (
                "input_tokens",
                "cached_input_tokens",
                "uncached_input_tokens",
                "output_tokens",
            )
        )
        return result

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
            if success and _usage_is_telemetry_complete(usage):
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
                    (
                        self._app_turn_error(state)
                        if not success
                        else "App Server usage incomplete; thread baseline quarantined"
                    )
                    or "App Server turn failed",
                    needs_reset=needs_reset or success,
                )
        with self._app_state_lock:
            job_id = str(job.get("job_id"))
            self.app_turns.pop(job_id, None)
            if thread_id and self.app_thread_jobs.get(thread_id) == job_id:
                self.app_thread_jobs.pop(thread_id, None)
            turn_id = str(job.get("turn_id") or "")
            if turn_id and self.app_turn_jobs.get(turn_id) == job_id:
                self.app_turn_jobs.pop(turn_id, None)

    def _settle_job_row(
        self,
        connection: Any,
        job_id: str,
        *,
        status: str,
        error: str,
        exit_code: int | None,
        thread_id: str | None = None,
        usage: Mapping[str, Any] | None = None,
        usage_source: str | None = None,
        telemetry_complete: bool | None = None,
        usage_missing_fields: str | None = None,
        thread_action: str | None = None,
    ) -> bool:
        if status not in {"completed", "failed", "interrupted", "lost"}:
            raise ClusterError(f"invalid terminal job status: {status}")
        now = utc_now()
        fields = [
            "status = ?",
            "finished_at = ?",
            "exit_code = ?",
            "error = ?",
            "outcome = ?",
            "settled_at = ?",
        ]
        parameters: list[Any] = [
            status,
            now,
            exit_code,
            error,
            status,
            now,
        ]
        if thread_id is not None:
            fields.append("thread_id = ?")
            parameters.append(thread_id)
        if thread_action is not None:
            fields.append("thread_action = ?")
            parameters.append(thread_action)
        if usage is not None:
            fields.extend(
                [
                    "input_tokens = ?",
                    "cached_input_tokens = ?",
                    "uncached_input_tokens = ?",
                    "output_tokens = ?",
                    "first_input_tokens = ?",
                    "first_cached_input_tokens = ?",
                    "first_uncached_input_tokens = ?",
                    "context_tokens = ?",
                    "model_context_window = ?",
                    "cache_write_input_tokens = ?",
                    "reasoning_output_tokens = ?",
                    "usage_source = ?",
                    "telemetry_complete = ?",
                    "usage_missing_fields = ?",
                ]
            )
            parameters.extend(
                [
                    usage.get("input_tokens") or 0,
                    usage.get("cached_input_tokens") or 0,
                    usage.get("uncached_input_tokens") or 0,
                    usage.get("output_tokens") or 0,
                    usage.get("first_input_tokens") or 0,
                    usage.get("first_cached_input_tokens") or 0,
                    usage.get("first_uncached_input_tokens") or 0,
                    usage.get("context_tokens") or 0,
                    usage.get("model_context_window"),
                    usage.get("cache_write_input_tokens") or 0,
                    usage.get("reasoning_output_tokens") or 0,
                    usage_source,
                    1 if telemetry_complete else 0,
                    usage_missing_fields
                    or _usage_missing_fields_json(usage),
                ]
            )
        parameters.append(job_id)
        updated = connection.execute(
            f"""
            UPDATE cluster_jobs
            SET {", ".join(fields)}
            WHERE job_id = ? AND status IN ('starting', 'running')
            """,
            parameters,
        )
        return updated.rowcount == 1

    def _finish_job(
        self,
        job: dict[str, Any],
        exit_code: int | None,
        *,
        forced_error: str = "",
        agent_already_failed: bool = False,
        context_overflow: bool = False,
        forced_status: str | None = None,
    ) -> None:
        if forced_status not in {
            None,
            "completed",
            "failed",
            "interrupted",
            "lost",
        }:
            raise ClusterError(f"invalid forced job status: {forced_status}")
        process = self.processes.get(str(job["job_id"]))
        if process is not None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        pump = self.log_threads.pop(job["job_id"], None)
        if pump is not None:
            pump.join(timeout=5)
        process_job = self.process_jobs.pop(job["job_id"], None)
        if process_job is not None:
            process_job.close()
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
        usage = self._app_job_usage(job, app_state) if app_server_runtime else parsed
        usage_missing_fields = _usage_missing_fields_json(usage)
        if app_server_runtime:
            usage_has_evidence = _usage_has_evidence(usage)
            usage_source = (
                "cluster_agents.last_usage+runtime_raw_turn"
                if usage_has_evidence
                else None
            )
            telemetry_complete = (
                _usage_is_telemetry_complete(usage)
                if usage_has_evidence
                else False
            )
        else:
            usage_source = parsed.get("usage_source")
            telemetry_complete = _usage_is_telemetry_complete(usage)
        if job["kind"] == "worker":
            task = self.team.get_task(job["task_id"])
            worker_ok = task["status"] in {"review", "done", "blocked"}
            status = forced_status or (
                "completed" if process_ok and worker_ok else "failed"
            )
            error = ""
            if not worker_ok:
                error = "worker exited without submitting or blocking its task"
            elif task["status"] == "blocked" and process_error:
                error = process_error
            elif not process_ok:
                error = process_error or "worker process failed"
            settled = False
            with self.team.connection() as connection:
                settled = self._settle_job_row(
                    connection,
                    job["job_id"],
                    status=status,
                    error=error,
                    exit_code=exit_code,
                    thread_id=thread_id,
                    usage=usage,
                    usage_source=usage_source,
                    telemetry_complete=telemetry_complete,
                    usage_missing_fields=usage_missing_fields,
                )
                if settled:
                    self._event(
                        connection,
                        job["goal_id"],
                        "worker_finished",
                        "teamd",
                        {
                            "job_id": job["job_id"],
                            "task_id": job["task_id"],
                            "status": status,
                            "outcome": status,
                            "exit_code": exit_code,
                            "thread_id": thread_id,
                            "error": error or process_error,
                        },
                    )
            self.processes.pop(job["job_id"], None)
            if not settled:
                return
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

        status = forced_status or ("completed" if process_ok else "failed")
        error = "" if process_ok else process_error or (
            f"lead process exited with code {exit_code}" if exit_code is not None else "lead process lost"
        )
        settled = False
        with self.team.connection() as connection:
            settled = self._settle_job_row(
                connection,
                job["job_id"],
                status=status,
                error=error,
                exit_code=exit_code,
                thread_id=thread_id,
                usage=usage,
                usage_source=usage_source,
                telemetry_complete=telemetry_complete,
                usage_missing_fields=usage_missing_fields,
            )
            if settled:
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
                        "outcome": status,
                        "exit_code": exit_code,
                        "thread_id": thread_id,
                        "error": error,
                    },
                )
        self.processes.pop(job["job_id"], None)
        if not settled:
            return
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

    def _terminate_exec_job(
        self,
        job: Mapping[str, Any],
        *,
        exit_code: int,
    ) -> bool:
        job_id = str(job.get("job_id") or "")
        process = self.processes.pop(job_id, None)
        process_job = self.process_jobs.pop(job_id, None)
        terminated = False
        if process_job is not None:
            terminated = process_job.terminate(exit_code)
            process_job.close()
        else:
            expected = persisted_process_identity(job)
            if not process_identity_matches(expected):
                return False
            subprocess.run(
                ["taskkill", "/PID", str(expected["pid"]), "/T", "/F"],
                capture_output=True,
                text=True,
                check=False,
            )
            deadline = time.monotonic() + 5
            while process_identity_matches(expected):
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
            terminated = not process_identity_matches(expected)
        if process is not None:
            try:
                process.wait(timeout=5)
                terminated = True
            except subprocess.TimeoutExpired:
                pass
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is None or stream.closed:
                    continue
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
        return terminated

    def _handle_worker_failure(self, job: dict[str, Any], reason: str) -> None:
        task = self.team.get_task(job["task_id"])
        if task["status"] in {"review", "done", "blocked", "canceled"}:
            return
        expected_actor = self._worker_actor(
            str(job["role"]),
            str(job["task_id"]),
            str(job["job_id"]),
        )
        if task["status"] == "running" and task.get("claimed_by") != expected_actor:
            self._mark_worker_claim_conflict(
                job,
                expected_actor=expected_actor,
                actual_actor=str(task.get("claimed_by") or ""),
                reason=reason,
            )
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
            expected_actor=expected_actor,
        )

    def _mark_worker_claim_conflict(
        self,
        job: Mapping[str, Any],
        *,
        expected_actor: str,
        actual_actor: str,
        reason: str,
    ) -> None:
        error = (
            "worker claim conflict: "
            f"expected {expected_actor}, found {actual_actor or '(none)'}"
        )
        with self.team.connection() as connection:
            if self._settle_job_row(
                connection,
                str(job["job_id"]),
                status="lost",
                error=error,
                exit_code=None,
            ):
                self._event(
                    connection,
                    str(job["goal_id"]),
                    "worker_claim_conflict",
                    "teamd",
                    {
                        "job_id": job["job_id"],
                        "task_id": job["task_id"],
                        "expected_actor": expected_actor,
                        "actual_actor": actual_actor,
                        "reason": reason,
                        "error": error,
                    },
                )

    def _recover_task(
        self,
        task_id: str,
        goal_id: str,
        *,
        reason: str,
        job_id: str | None = None,
        attempt: int = 0,
        expected_actor: str | None = None,
    ) -> bool:
        with self.team.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = connection.execute(
                """
                SELECT status, claimed_by, claim_token, version
                FROM tasks WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
            if task is None:
                return False
            if task["status"] in TERMINAL_TASK_STATUSES or task["status"] == "review":
                return False
            if task["status"] == "ready":
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
                        "already_ready": True,
                    },
                )
                return True
            if task["status"] != "running":
                return False
            if expected_actor and task["claimed_by"] != expected_actor:
                return False
            owner = expected_actor or task["claimed_by"]
            updated = connection.execute(
                """
                UPDATE tasks
                SET status = 'ready', claimed_by = NULL, claim_token = NULL,
                    lease_until = NULL, blocker = ?, updated_at = ?,
                    version = version + 1
                WHERE task_id = ? AND status = 'running' AND version = ?
                  AND claimed_by IS ? AND claim_token IS ?
                """,
                (
                    reason,
                    utc_now(),
                    task_id,
                    int(task["version"]),
                    owner,
                    task["claim_token"],
                ),
            )
            if updated.rowcount == 0:
                return False
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
            return True

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


def _path_is_within(path: str | Path, root: str | Path) -> bool:
    try:
        resolved_path = Path(path).expanduser().resolve()
        resolved_root = Path(root).expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    return (
        resolved_path == resolved_root
        or resolved_root in resolved_path.parents
    )


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
    usage_record = usage_from_exec_log(path)
    usage = {
        field: usage_record.get(field)
        for field in (
            "input_tokens",
            "cached_input_tokens",
            "uncached_input_tokens",
            "cache_write_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
            "context_tokens",
            "model_context_window",
        )
    }
    first_usage = usage_record.get("first_usage")
    result: dict[str, Any] = {
        "thread_id": None,
        "completed": False,
        "error": "",
        **usage,
        "first_input_tokens": (
            first_usage.get("input_tokens")
            if isinstance(first_usage, Mapping)
            else usage["input_tokens"]
        ),
        "first_cached_input_tokens": (
            first_usage.get("cached_input_tokens")
            if isinstance(first_usage, Mapping)
            else usage["cached_input_tokens"]
        ),
        "first_uncached_input_tokens": (
            first_usage.get("uncached_input_tokens")
            if isinstance(first_usage, Mapping)
            else usage["uncached_input_tokens"]
        ),
        "usage_source": usage_record.get("usage_source"),
    }
    present_fields: set[str] = set()
    first_present_fields: set[str] | None = None
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
                    event_usage = item.get("usage")
                    if isinstance(event_usage, Mapping):
                        event_fields = {
                            field
                            for field in (
                                "input_tokens",
                                "cached_input_tokens",
                                "uncached_input_tokens",
                                "cache_write_input_tokens",
                                "output_tokens",
                                "reasoning_output_tokens",
                                "context_tokens",
                                "model_context_window",
                            )
                            if usage_field_is_present(event_usage, field)
                        }
                        present_fields.update(event_fields)
                        if first_present_fields is None:
                            first_present_fields = set(event_fields)
                elif event_type in {"turn.failed", "error"}:
                    error = item.get("error") or item.get("message")
                    if isinstance(error, dict):
                        error = error.get("message") or json.dumps(error, ensure_ascii=False)
                    result["error"] = str(error or event_type)
    except OSError:
        result["error"] = "log unreadable"
    if (
        "uncached_input_tokens" in present_fields
        or {
            "input_tokens",
            "cached_input_tokens",
        }.issubset(present_fields)
    ):
        present_fields.add("uncached_input_tokens")
    output_present_fields = set(present_fields)
    first_present_fields = first_present_fields or set()
    if (
        "uncached_input_tokens" in first_present_fields
        or {
            "input_tokens",
            "cached_input_tokens",
        }.issubset(first_present_fields)
    ):
        first_present_fields.add("uncached_input_tokens")
    for final_field, snapshot_field in (
        ("first_input_tokens", "input_tokens"),
        ("first_cached_input_tokens", "cached_input_tokens"),
        ("first_uncached_input_tokens", "uncached_input_tokens"),
    ):
        if snapshot_field in first_present_fields:
            output_present_fields.add(final_field)
    result["usage_present_fields"] = tuple(sorted(output_present_fields))
    result["usage_missing_fields"] = ()
    result["telemetry_complete"] = all(
        field in output_present_fields
        and result.get(field) is not None
        for field in (
            "input_tokens",
            "cached_input_tokens",
            "uncached_input_tokens",
            "output_tokens",
        )
    )
    result["usage_missing_fields"] = _usage_missing_fields_json(result)
    return result


def _usage_has_evidence(value: Mapping[str, Any] | None) -> bool:
    return usage_has_cost_evidence(value)


def _usage_has_any_evidence(value: Mapping[str, Any] | None) -> bool:
    return usage_has_any_evidence(value)


def _usage_is_telemetry_complete(
    usage: Mapping[str, Any] | None,
) -> bool:
    if isinstance(usage, Mapping):
        declared = usage.get("telemetry_complete")
        if isinstance(declared, bool):
            return declared
    return usage_is_cost_complete(usage)


def _usage_missing_fields_json(usage: Mapping[str, Any] | None) -> str:
    values = usage if isinstance(usage, Mapping) else {}
    declared = values.get("usage_missing_fields")
    declared_missing = (
        {
            field
            for field in declared
            if isinstance(field, str) and field in JOB_USAGE_FIELDS
        }
        if isinstance(declared, (list, tuple, set, frozenset))
        else set()
    )
    present = values.get("usage_present_fields")
    present_fields = (
        {
            field
            for field in present
            if isinstance(field, str) and field in JOB_USAGE_FIELDS
        }
        if isinstance(present, (list, tuple, set, frozenset))
        else None
    )
    missing = sorted(
        field
        for field in JOB_USAGE_FIELDS
        if (
            field in declared_missing
            or values.get(field) is None
            or (
                present_fields is not None
                and field not in present_fields
            )
        )
    )
    return json.dumps(missing, ensure_ascii=False, separators=(",", ":"))


def _usage_state_has_evidence(state: Mapping[str, Any] | None) -> bool:
    if not isinstance(state, Mapping):
        return False
    return _usage_has_evidence(state.get("usage"))


def _app_usage_state_has_evidence(
    job: Mapping[str, Any],
    state: Mapping[str, Any] | None,
) -> bool:
    if _usage_state_has_evidence(state):
        return True
    return _usage_state_has_evidence(
        _app_usage_state_from_log(job.get("log_path"))
    )


def _app_usage_state_from_log(path: Any) -> dict[str, Any] | None:
    """Read the first and latest App Server token usage events from a job log."""

    if not path:
        return None
    first_usage: Mapping[str, Any] | None = None
    latest_usage: Mapping[str, Any] | None = None
    try:
        with Path(str(path)).open(
            "r",
            encoding="utf-8",
            errors="replace",
        ) as stream:
            for line in stream:
                if not line.startswith("{"):
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, Mapping):
                    continue
                if event.get("event") != "token_usage":
                    continue
                usage = event.get("usage")
                if not isinstance(usage, Mapping) or not _usage_has_any_evidence(
                    usage
                ):
                    continue
                if first_usage is None:
                    first_usage = usage
                latest_usage = usage
    except OSError:
        return None
    if latest_usage is None:
        return None
    return {
        "usage": latest_usage,
        "first_usage": first_usage,
    }


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


def daemon_lock_state(settings: Settings) -> dict[str, Any]:
    lock_path, _, _ = daemon_paths(settings)
    exists = lock_path.exists()
    payload: dict[str, Any] = {}
    state_valid = False
    if exists:
        try:
            parsed = json.loads(lock_path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                payload = parsed
                state_valid = True
        except (OSError, ValueError, TypeError):
            pass
    try:
        pid = int(payload.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    recorded = identity_record(payload.get("process_identity"))
    verified = bool(
        pid > 0
        and recorded
        and int(recorded.get("pid") or 0) == pid
        and process_identity_matches(recorded)
    )
    pid_running = bool(pid > 0 and pid_is_running(pid))
    return {
        "exists": exists,
        "path": lock_path,
        "payload": payload,
        "state_valid": state_valid and pid > 0,
        "pid": pid,
        "process_identity": recorded,
        "process_identity_verified": verified,
        "pid_running": pid_running,
        "unverified_live_process": bool(pid_running and not verified),
    }


def daemon_identity(settings: Settings) -> dict[str, Any]:
    runtime = _runtime_mode(os.environ.get("CODEX_TEAM_AGENT_RUNTIME"))
    model = os.environ.get("CODEX_TEAM_MODEL", "").strip()
    model_provider = os.environ.get("CODEX_TEAM_MODEL_PROVIDER", "").strip()
    home = str(settings.home.resolve())
    codex_home = str(service_codex_home(settings))
    payload = {
        "runtime": runtime,
        "model": model,
        "model_provider": model_provider,
        "team_home": home,
        "codex_home": codex_home,
        "version": __version__,
    }
    fingerprint = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {**payload, "fingerprint": fingerprint}


def daemon_status(settings: Settings) -> dict[str, Any]:
    lock_path, state_path, log_path = daemon_paths(settings)
    lock = daemon_lock_state(settings)
    pid = int(lock["pid"])
    lock_state = lock["payload"]
    state: dict[str, Any] = {}
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        pass
    expected_identity = daemon_identity(settings)
    actual_identity = state.get("identity") if isinstance(state, dict) else None
    recorded_process = lock["process_identity"]
    process_verified = bool(lock["process_identity_verified"])
    return {
        "running": process_verified,
        "pid": pid or None,
        "process_identity": recorded_process or None,
        "process_identity_verified": process_verified,
        "lock_state_valid": bool(lock["state_valid"]),
        "lock_state": lock_state,
        "pid_running": bool(lock["pid_running"]),
        "unverified_live_process": bool(lock["unverified_live_process"]),
        "lock_path": str(lock_path),
        "state_path": str(state_path),
        "log_path": str(log_path),
        "state": state,
        "expected_identity": expected_identity,
        "config_mismatch": bool(
            process_verified
            and actual_identity != expected_identity
        ),
    }


def ensure_daemon(settings: Settings) -> dict[str, Any]:
    current = daemon_status(settings)
    if current["running"]:
        return current
    lock_path, state_path, log_path = daemon_paths(settings)
    if current["unverified_live_process"]:
        raise ClusterError(
            "cluster daemon lock contains an unverified live process "
            f"(pid {current['pid']}); stop the legacy daemon before upgrading"
        )
    if current["lock_state_valid"] is False and lock_path.exists():
        raise ClusterError(
            "cluster daemon lock is corrupt and cannot be verified; "
            f"confirm no daemon is running and remove {lock_path}"
        )
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


def _wait_for_pid_exit(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while pid_is_running(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


def stop_daemon(settings: Settings) -> dict[str, Any]:
    current = daemon_status(settings)
    pid = int(current.get("pid") or 0)
    terminated = False
    if current.get("running") and pid:
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        if not _wait_for_pid_exit(pid):
            if result.returncode != 0:
                raise ClusterError(
                    f"verified cluster daemon pid {pid} did not stop"
                )
            raise ClusterError(
                f"cluster daemon pid {pid} is still running after stop"
            )
        terminated = True
    lock_path, _, _ = daemon_paths(settings)
    if current.get("unverified_live_process"):
        raise ClusterError(
            "cluster daemon lock contains an unverified live process "
            f"(pid {pid}); stop the legacy daemon manually before removing "
            f"{lock_path}"
        )
    if (
        current.get("lock_state_valid") is False
        and lock_path.exists()
    ):
        raise ClusterError(
            "cluster daemon lock is corrupt and cannot be verified; "
            f"confirm no daemon is running and remove {lock_path}"
        )
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass
    return {
        "ok": True,
        "stopped_pid": pid if terminated else None,
        "stale_pid": pid if pid and not terminated else None,
        "process_identity_verified": bool(
            current.get("process_identity_verified")
        ),
    }


def restart_daemon(
    settings: Settings,
    *,
    if_idle: bool = False,
) -> dict[str, Any]:
    if if_idle:
        connection = connect(settings.db_path)
        try:
            active = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT job_id, goal_id, task_id, role, status
                    FROM cluster_jobs
                    WHERE status IN ('starting', 'running')
                    ORDER BY started_at
                    """
                ).fetchall()
            ]
        finally:
            connection.close()
        if active:
            raise ClusterError(
                "cluster daemon is not idle; active jobs: "
                + ", ".join(str(job["job_id"]) for job in active)
            )
    stopped = stop_daemon(settings)
    daemon = ensure_daemon(settings)
    return {
        "ok": True,
        "stopped": stopped,
        "daemon": daemon,
    }
