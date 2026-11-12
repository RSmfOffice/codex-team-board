from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import subprocess
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Mapping

from .agent_pool import AgentPool, AgentPoolError
from .cache_contract import (
    cache_profile_contract_status,
    file_hash,
    workflow_epoch as build_workflow_epoch,
)
from .config import Settings
from .db import (
    connect,
    connect_readonly,
    read_dashboard_revision,
    utc_now,
)
from .goal_status import effective_goal_status
from .read_model import (
    CLAIM_WAIT_CRITICAL_SECONDS,
    CLAIM_WAIT_WARNING_SECONDS,
    age_seconds,
    claim_wait_state,
    goal_health,
    goal_next_action,
    goal_queue_reason,
    heartbeat_state,
    is_historical_task,
    parse_iso,
    worker_heartbeat_age,
)
from .scheduling import (
    DEFAULT_GLOBAL_JOB_CAPACITY,
    DEFAULT_GOAL_WORKER_CAPACITY,
    DEFAULT_HEARTBEAT_CRITICAL_SECONDS,
    DEFAULT_HEARTBEAT_WARNING_SECONDS,
    MAX_GLOBAL_JOB_CAPACITY,
    MAX_GOAL_WORKER_CAPACITY,
    env_int,
)
from .usage import JOB_USAGE_FIELDS, cache_hit_rate

TERMINAL_STATUSES = frozenset({"done", "canceled"})
ACTIVE_STATUSES = frozenset({"ready", "running", "review", "blocked"})


class TeamError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RequestMutation:
    request_id: str
    operation: str
    actor: str


_CURRENT_REQUEST_MUTATION: ContextVar[RequestMutation | None] = ContextVar(
    "codex_team_request_mutation",
    default=None,
)


@contextmanager
def request_mutation_scope(
    request_id: str,
    operation: str,
    actor: str,
) -> Iterator[None]:
    """Expose trusted request metadata to same-thread service mutations."""

    token = _CURRENT_REQUEST_MUTATION.set(
        RequestMutation(
            request_id=request_id,
            operation=operation,
            actor=str(actor or "system").strip() or "system",
        )
    )
    try:
        yield
    finally:
        _CURRENT_REQUEST_MUTATION.reset(token)


class TeamService:
    ROLE_FILES = {
        "team_explorer": "team-explorer.md",
        "team_worker": "team-worker.md",
        "team_reviewer": "team-reviewer.md",
    }

    def __init__(
        self,
        settings: Settings,
        *,
        agent_pool: AgentPool | None = None,
    ) -> None:
        self.settings = settings
        # A service instance is the process-level owner of the database.
        # Migrate before any read-only dashboard query can observe an old schema.
        with self.connection():
            pass
        self.agent_pool = agent_pool or AgentPool(settings)
        self._workflow_epoch_cache: dict[
            str,
            tuple[tuple[Any, ...], str],
        ] = {}

    def operator_authority(self) -> str:
        """Load or atomically create the local privileged-operation token."""

        self.settings.ensure()
        path = self.settings.home / "operator-authority.token"
        try:
            token = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            token = secrets.token_urlsafe(32)
            try:
                descriptor = os.open(
                    path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError:
                token = path.read_text(encoding="utf-8").strip()
            else:
                with os.fdopen(
                    descriptor,
                    "w",
                    encoding="utf-8",
                    newline="\n",
                ) as stream:
                    stream.write(token + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
        if len(token) < 32:
            raise TeamError(
                "local operator authority token is missing or invalid"
            )
        return token

    @contextmanager
    def connection(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        self.settings.ensure()
        connection = (
            connect(self.settings.db_path)
            if write or not self.settings.db_path.is_file()
            else connect_readonly(self.settings.db_path)
        )
        changed_tables: set[str] = set()
        if write:
            tracked_actions = {
                sqlite3.SQLITE_INSERT,
                sqlite3.SQLITE_UPDATE,
                sqlite3.SQLITE_DELETE,
            }

            def track_changes(
                action: int,
                table_name: str | None,
                _column: str | None,
                _database: str | None,
                _trigger: str | None,
            ) -> int:
                if action in tracked_actions and table_name:
                    changed_tables.add(str(table_name))
                return sqlite3.SQLITE_OK

            connection.set_authorizer(track_changes)
        try:
            yield connection
            if write:
                request = _CURRENT_REQUEST_MUTATION.get()
                if request is not None:
                    self._record_request_mutation(
                        connection,
                        request,
                        changed_tables,
                    )
                connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _record_request_mutation(
        connection: sqlite3.Connection,
        request: RequestMutation,
        changed_tables: set[str],
    ) -> None:
        ignored = {
            "dashboard_revision",
            "external_operations",
            "metadata",
            "request_receipts",
            "schema_migrations",
        }
        domain_tables = sorted(changed_tables - ignored)
        if not domain_tables:
            return
        existing = connection.execute(
            """
            SELECT operation_type, result_json
            FROM external_operations
            WHERE operation_id = ?
            """,
            (request.request_id,),
        ).fetchone()
        if existing is not None:
            if str(existing["operation_type"]) != request.operation:
                raise TeamError(
                    "request mutation ledger conflicts with another operation"
                )
            try:
                previous = json.loads(str(existing["result_json"] or "{}"))
            except (TypeError, ValueError):
                previous = {}
            if not isinstance(previous, dict):
                previous = {}
            previous_tables = previous.get("changed_tables", [])
            if isinstance(previous_tables, list):
                domain_tables = sorted(
                    set(domain_tables)
                    | {str(value) for value in previous_tables if value}
                )
            previous["changed_tables"] = domain_tables
            result_json = json.dumps(
                previous,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            connection.execute(
                """
                UPDATE external_operations
                SET result_json = ?, updated_at = ?
                WHERE operation_id = ? AND operation_type = ?
                """,
                (
                    result_json,
                    utc_now(),
                    request.request_id,
                    request.operation,
                ),
            )
            return
        now = utc_now()
        result_json = json.dumps(
            {"changed_tables": domain_tables},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        connection.execute(
            """
            INSERT INTO external_operations(
                operation_id, operation_type, status, actor, payload_json,
                result_json, created_at, updated_at, completed_at
            ) VALUES (?, ?, 'completed', ?, '{}', ?, ?, ?, ?)
            """,
            (
                request.request_id,
                request.operation,
                request.actor,
                result_json,
                now,
                now,
                now,
            ),
        )

    @staticmethod
    def _record_request_result(
        connection: sqlite3.Connection,
        result: Mapping[str, Any],
        *,
        task_id: str | None = None,
        goal_id: str | None = None,
        job_id: str | None = None,
    ) -> None:
        request = _CURRENT_REQUEST_MUTATION.get()
        if request is None:
            return
        existing = connection.execute(
            """
            SELECT operation_type, result_json
            FROM external_operations
            WHERE operation_id = ?
            """,
            (request.request_id,),
        ).fetchone()
        result_json = json.dumps(
            dict(result),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        now = utc_now()
        if existing is None:
            connection.execute(
                """
                INSERT INTO external_operations(
                    operation_id, operation_type, status, goal_id, task_id,
                    job_id, actor, payload_json, result_json, created_at,
                    updated_at, completed_at
                ) VALUES (?, ?, 'completed', ?, ?, ?, ?, '{}', ?, ?, ?, ?)
                """,
                (
                    request.request_id,
                    request.operation,
                    goal_id,
                    task_id,
                    job_id,
                    request.actor,
                    result_json,
                    now,
                    now,
                    now,
                ),
            )
            return
        if str(existing["operation_type"]) != request.operation:
            raise TeamError(
                "request mutation ledger conflicts with another operation"
            )
        connection.execute(
            """
            UPDATE external_operations
            SET result_json = ?, updated_at = ?
            WHERE operation_id = ? AND operation_type = ?
            """,
            (
                result_json,
                now,
                request.request_id,
                request.operation,
            ),
        )

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        task_id: str,
        event_type: str,
        actor: str,
        data: dict[str, Any] | None = None,
        *,
        thread_id: str | None = None,
        job_id: str | None = None,
        correlation_id: str | None = None,
        assignment_id: str | None = None,
    ) -> None:
        event_data = dict(data or {})
        request = _CURRENT_REQUEST_MUTATION.get()
        if request is not None:
            event_data["request_id"] = request.request_id
        context = TeamService._event_context(
            connection,
            task_id,
            actor,
        )
        connection.execute(
            """
            INSERT INTO task_events(
                task_id, event_type, actor, thread_id, job_id,
                correlation_id, assignment_id, data_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                event_type,
                actor,
                thread_id or context.get("thread_id"),
                job_id or context.get("job_id"),
                correlation_id or context.get("correlation_id"),
                assignment_id or context.get("assignment_id"),
                json.dumps(event_data, ensure_ascii=False, sort_keys=True),
                utc_now(),
            ),
        )

    @staticmethod
    def _managed_caller_binding() -> tuple[str, str]:
        role = os.environ.get("CODEX_TEAM_ROLE", "").strip().lower()
        actor = os.environ.get("CODEX_TEAM_ACTOR", "").strip()
        return role, actor

    def authorize_privileged_actor(
        self,
        requested_actor: str,
        *,
        operation: str,
        allowed_actors: frozenset[str] = frozenset(
            {"lead", "main", "human"}
        ),
        authority_token: str | None = None,
    ) -> str:
        """Bind privileged mutations to the trusted local process role."""

        if not isinstance(requested_actor, str):
            raise TeamError("actor must be a string")
        normalized_actor = requested_actor.strip().lower()
        if not normalized_actor:
            raise TeamError("actor is required")
        if normalized_actor not in allowed_actors:
            raise TeamError(
                f"only the lead or human reviewer can perform {operation}"
            )
        token = (
            authority_token.strip()
            if isinstance(authority_token, str)
            else ""
        )
        if token:
            if secrets.compare_digest(token, self.operator_authority()):
                return normalized_actor
            raise TeamError(
                f"{operation} requires a valid local authority token"
            )
        role, bound_actor = self._managed_caller_binding()
        if not role:
            return normalized_actor
        if role not in allowed_actors:
            raise TeamError(
                f"managed role {role} cannot perform {operation}"
            )
        if (
            role == "lead"
            and normalized_actor in {"lead", "main"}
        ) or (
            role == "main"
            and normalized_actor == "main"
        ) or (
            role == "human"
            and normalized_actor == "human"
        ):
            if (
                bound_actor
                and role != "lead"
                and normalized_actor != bound_actor.lower()
            ):
                raise TeamError(
                    "managed caller identity does not match its binding"
                )
            return normalized_actor
        raise TeamError(
            "managed caller identity does not match its binding"
        )

    def require_mcp_authority(
        self,
        requested_actor: str,
        *,
        operation: str,
        authority_token: str | None = None,
        allowed_actors: frozenset[str] = frozenset(
            {"lead", "main", "human"}
        ),
    ) -> str:
        """Require a capability or trusted role at the MCP boundary."""

        if not isinstance(requested_actor, str):
            raise TeamError("actor must be a string")
        normalized_actor = requested_actor.strip().lower()
        if normalized_actor not in allowed_actors:
            raise TeamError(
                f"only an authorized actor can perform {operation}"
            )
        token = (
            authority_token.strip()
            if isinstance(authority_token, str)
            else ""
        )
        if token and secrets.compare_digest(
            token,
            self.operator_authority(),
        ):
            return normalized_actor
        role, _actor = self._managed_caller_binding()
        if role:
            try:
                return self.authorize_privileged_actor(
                    normalized_actor,
                    operation=operation,
                    allowed_actors=allowed_actors,
                )
            except TeamError as exc:
                raise TeamError(
                    f"{operation} requires a valid local authority token"
                ) from exc
        raise TeamError(
            f"{operation} requires a valid local authority token"
        )

    def privileged_actor(
        self,
        operation: str,
        *,
        authority_token: str | None = None,
    ) -> str:
        role, _actor = self._managed_caller_binding()
        if not role:
            return self.require_mcp_authority(
                "human",
                operation=operation,
                authority_token=authority_token,
            )
        if role == "lead":
            return self.require_mcp_authority(
                "lead",
                operation=operation,
                authority_token=authority_token,
            )
        if role == "main":
            return self.require_mcp_authority(
                "main",
                operation=operation,
                authority_token=authority_token,
            )
        if role == "human":
            return self.require_mcp_authority(
                "human",
                operation=operation,
                authority_token=authority_token,
            )
        raise TeamError(f"managed role {role} cannot perform {operation}")

    @staticmethod
    def _event_context(
        connection: sqlite3.Connection,
        task_id: str,
        actor: str,
    ) -> dict[str, str]:
        task = connection.execute(
            "SELECT project_id FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if task is None:
            return {}
        context: dict[str, str] = {}
        job = connection.execute(
            """
            SELECT job_id, thread_id, correlation_id
            FROM cluster_jobs
            WHERE task_id = ? AND actor = ?
            ORDER BY started_at DESC, job_id DESC
            LIMIT 1
            """,
            (task_id, actor),
        ).fetchone()
        if job is not None:
            context["job_id"] = str(job["job_id"])
            if job["thread_id"]:
                context["thread_id"] = str(job["thread_id"])
            if job["correlation_id"]:
                context["correlation_id"] = str(job["correlation_id"])
        if "thread_id" not in context:
            agent = connection.execute(
                """
                SELECT thread_id
                FROM cluster_agents
                WHERE project_id = ? AND actor = ?
                ORDER BY
                    CASE WHEN current_task_id = ? THEN 0 ELSE 1 END,
                    updated_at DESC,
                    agent_id
                LIMIT 1
                """,
                (task["project_id"], actor, task_id),
            ).fetchone()
            if agent is not None:
                thread_id = str(agent["thread_id"])
                context["thread_id"] = thread_id
                context["correlation_id"] = f"native:{thread_id}"
                context["assignment_id"] = thread_id
        return context

    @staticmethod
    def _cache_hit_rate(
        input_tokens: object,
        cached_input_tokens: object,
        uncached_input_tokens: object,
    ) -> float | None:
        """Return cached input share, or None when usage is unavailable."""

        return cache_hit_rate(
            {
                "input": input_tokens,
                "cached": cached_input_tokens,
                "uncached": uncached_input_tokens,
            }
        )

    @staticmethod
    def _worktree_git(
        cwd: Path,
        *args: str,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ["git", "-C", str(cwd), *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TeamError(f"could not inspect task worktree: {exc}") from exc

    def _validate_write_worktree_assignment(
        self,
        connection: sqlite3.Connection,
        task: Mapping[str, Any],
    ) -> Path:
        values = dict(task)
        if values.get("kind") != "write":
            raise TeamError("worktree validation requires a write task")
        worktree_value = str(values.get("worktree_path") or "").strip()
        branch = str(values.get("branch") or "").strip()
        if not worktree_value:
            raise TeamError(
                "write task requires an existing worktree; "
                "call team_create_worktree first"
            )
        if not branch:
            raise TeamError("write task worktree has no assigned branch")
        try:
            worktree = Path(worktree_value).expanduser().resolve(strict=True)
        except OSError as exc:
            raise TeamError(
                "write task requires an existing worktree; "
                "call team_create_worktree first"
            ) from exc
        if not worktree.is_dir():
            raise TeamError(
                "write task requires an existing worktree; "
                "call team_create_worktree first"
            )
        project = connection.execute(
            "SELECT root_path FROM projects WHERE project_id = ?",
            (values["project_id"],),
        ).fetchone()
        if project is None:
            raise TeamError(f"project not found: {values['project_id']}")
        root = Path(str(project["root_path"])).expanduser().resolve()
        if worktree == root:
            raise TeamError(
                "write task worktree must be isolated from the main checkout"
            )
        target_branch = str(values.get("target_branch") or "").strip()
        if target_branch and branch == target_branch:
            raise TeamError(
                "write task branch must differ from its target branch"
            )
        other_tasks = connection.execute(
            """
            SELECT task_id, worktree_path, branch
            FROM tasks
            WHERE project_id = ? AND task_id != ?
              AND (worktree_path IS NOT NULL OR branch IS NOT NULL)
            """,
            (values["project_id"], values["task_id"]),
        ).fetchall()
        for other in other_tasks:
            other_path = str(other["worktree_path"] or "").strip()
            if other_path:
                try:
                    resolved_other = Path(
                        other_path
                    ).expanduser().resolve()
                except OSError:
                    resolved_other = Path(
                        other_path
                    ).expanduser().absolute()
                if resolved_other == worktree:
                    raise TeamError(
                        "worktree is already assigned to task "
                        f"{other['task_id']}"
                    )
            if (
                other["branch"]
                and str(other["branch"]).strip() == branch
            ):
                raise TeamError(
                    "worktree branch is already assigned to task "
                    f"{other['task_id']}"
                )
        top = self._worktree_git(
            worktree,
            "rev-parse",
            "--show-toplevel",
        )
        if top.returncode != 0:
            raise TeamError(
                top.stderr.strip()
                or "write task worktree is not a Git worktree"
            )
        try:
            top_path = Path(top.stdout.strip()).expanduser().resolve()
        except OSError as exc:
            raise TeamError("could not resolve task worktree root") from exc
        if top_path != worktree:
            raise TeamError(
                "write task worktree path does not match its Git worktree root"
            )
        current_branch = self._worktree_git(
            worktree,
            "symbolic-ref",
            "--short",
            "HEAD",
        )
        if current_branch.returncode != 0:
            raise TeamError(
                current_branch.stderr.strip()
                or "write task worktree has no checked-out branch"
            )
        if current_branch.stdout.strip() != branch:
            raise TeamError(
                "write task worktree branch does not match its assignment"
            )
        status = self._worktree_git(worktree, "status", "--porcelain")
        if status.returncode != 0:
            raise TeamError(
                status.stderr.strip() or "could not inspect task worktree"
            )
        if status.stdout.strip():
            raise TeamError(
                "write task worktree is dirty; it must be clean before assignment"
            )
        return worktree

    @staticmethod
    def _project_unknown_usage(job: dict[str, Any]) -> None:
        try:
            parsed = json.loads(str(job.get("usage_missing_fields") or "[]"))
        except (TypeError, ValueError):
            parsed = []
        missing = (
            {
                field
                for field in parsed
                if isinstance(field, str) and field in JOB_USAGE_FIELDS
            }
            if isinstance(parsed, list)
            else set()
        )
        if not job.get("usage_source") and not missing:
            missing.update(
                field
                for field in JOB_USAGE_FIELDS
                if not int(job.get(field) or 0)
            )
        for field in missing:
            job[field] = None

    @staticmethod
    def _root(path: str | Path) -> Path:
        candidate = Path(path).expanduser().resolve()
        try:
            result = subprocess.run(
                ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TeamError(f"could not inspect Git repository: {exc}") from exc
        if result.returncode != 0:
            raise TeamError(f"not a Git repository: {candidate}")
        return Path(result.stdout.strip()).resolve()

    def register_project(self, repo: str | Path) -> dict[str, Any]:
        root = self._root(repo)
        project_id = hashlib.sha256(str(root).casefold().encode("utf-8")).hexdigest()[:12]
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO projects(project_id, name, root_path, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    name = excluded.name,
                    root_path = excluded.root_path,
                    updated_at = excluded.updated_at
                WHERE projects.name IS NOT excluded.name
                   OR projects.root_path IS NOT excluded.root_path
                """,
                (project_id, root.name, str(root), now, now),
            )
            row = connection.execute(
                "SELECT * FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        return dict(row)

    def ensure_project_active(self, project: Mapping[str, Any]) -> None:
        """Reject archived projects without mutating prompt-loaded files."""

        self._ensure_project_active(dict(project))

    def project_for_repo(self, repo: str | Path) -> dict[str, Any]:
        return self.register_project(repo)

    def get_project(self, project_id: str) -> dict[str, Any]:
        with self.connection(write=False) as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        if row is None:
            raise TeamError(f"project not found: {project_id}")
        return dict(row)

    def reset_agent(
        self,
        agent_id: str,
        *,
        actor: str = "human",
        reason: str = "manual reset requested through dashboard",
    ) -> dict[str, Any]:
        if not isinstance(agent_id, str):
            raise TeamError("agent_id must be a string")
        normalized_id = agent_id.strip()
        if not normalized_id:
            raise TeamError("agent_id is required")
        if not isinstance(actor, str):
            raise TeamError("actor must be a string")
        normalized_actor = actor.strip()
        if not normalized_actor:
            raise TeamError("actor is required")
        if not isinstance(reason, str):
            raise TeamError("reset reason must be a string")
        normalized_reason = reason.strip()
        if not normalized_reason:
            normalized_reason = "manual reset requested through dashboard"
        if len(normalized_reason) > 2000:
            raise TeamError("reset reason must not exceed 2000 characters")
        try:
            return self.agent_pool.request_reset(
                normalized_id,
                normalized_reason,
                normalized_actor,
            )
        except AgentPoolError as exc:
            raise TeamError(str(exc)) from exc

    def archive_stale_agents(
        self,
        *,
        actor: str = "human",
        reason: str = "archive failed and non-reusable managed agents",
        apply: bool = False,
    ) -> dict[str, Any]:
        try:
            return self.agent_pool.archive_stale_managed_agents(
                actor=actor,
                reason=reason,
                apply=apply,
            )
        except AgentPoolError as exc:
            raise TeamError(str(exc)) from exc

    def list_projects(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        with self.connection(write=False) as connection:
            where = "" if include_archived else "WHERE archived_at IS NULL"
            rows = connection.execute(
                f"SELECT * FROM projects {where} ORDER BY updated_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _ensure_project_active(project: dict[str, Any]) -> None:
        if project.get("archived_at"):
            raise TeamError("project is archived; restore it before making changes")

    def set_project_archived(
        self,
        project_id: str,
        archived: bool,
        *,
        actor: str = "human",
        authority_token: str | None = None,
    ) -> dict[str, Any]:
        actor = self.authorize_privileged_actor(
            actor,
            operation="project archive changes",
            allowed_actors=frozenset({"lead", "main", "human"}),
            authority_token=authority_token,
        )
        now = utc_now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            project = connection.execute(
                "SELECT * FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if project is None:
                raise TeamError(f"project not found: {project_id}")
            if bool(project["archived_at"]) == bool(archived):
                return dict(project)
            if archived:
                active_goal = connection.execute(
                    """
                    SELECT goal_id FROM cluster_goals
                    WHERE project_id = ? AND status IN ('planning', 'active', 'paused')
                    LIMIT 1
                    """,
                    (project_id,),
                ).fetchone()
                running_task = connection.execute(
                    """
                    SELECT task_id FROM tasks
                    WHERE project_id = ? AND status = 'running'
                    LIMIT 1
                    """,
                    (project_id,),
                ).fetchone()
                active_job = connection.execute(
                    """
                    SELECT j.job_id
                    FROM cluster_jobs j
                    JOIN cluster_goals g ON g.goal_id = j.goal_id
                    WHERE g.project_id = ? AND j.status IN ('starting', 'running')
                    LIMIT 1
                    """,
                    (project_id,),
                ).fetchone()
                if active_goal:
                    raise TeamError(
                        "cannot archive project with unfinished goal: "
                        f"{active_goal['goal_id']}"
                    )
                if running_task:
                    raise TeamError(
                        "cannot archive project with running task: "
                        f"{running_task['task_id']}"
                    )
                if active_job:
                    raise TeamError(
                        "cannot archive project with active job: "
                        f"{active_job['job_id']}"
                    )
            connection.execute(
                """
                UPDATE projects
                SET archived_at = ?, updated_at = ?
                WHERE project_id = ?
                """,
                (now if archived else None, now, project_id),
            )
        return self.get_project(str(project["project_id"]))

    def set_task_archived(
        self,
        task_id: str,
        archived: bool,
        *,
        actor: str = "human",
        authority_token: str | None = None,
    ) -> dict[str, Any]:
        actor = self.authorize_privileged_actor(
            actor,
            operation="task archive changes",
            allowed_actors=frozenset({"lead", "main", "human"}),
            authority_token=authority_token,
        )
        now = utc_now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = connection.execute(
                """
                SELECT t.*, p.archived_at AS project_archived_at
                FROM tasks t JOIN projects p ON p.project_id = t.project_id
                WHERE t.task_id = ?
                """,
                (task_id,),
            ).fetchone()
            if task is None:
                raise TeamError(f"task not found: {task_id}")
            if bool(task["archived_at"]) == bool(archived):
                pass
            else:
                if archived:
                    if task["project_archived_at"]:
                        raise TeamError(
                            "project is archived; restore it before archiving tasks"
                        )
                    if task["status"] == "running":
                        raise TeamError(f"cannot archive running task: {task_id}")
                    active_job = connection.execute(
                        """
                        SELECT job_id FROM cluster_jobs
                        WHERE task_id = ? AND status IN ('starting', 'running')
                        LIMIT 1
                        """,
                        (task_id,),
                    ).fetchone()
                    active_goal = None
                    if task["goal_id"]:
                        active_goal = connection.execute(
                            """
                            SELECT goal_id FROM cluster_goals
                            WHERE goal_id = ? AND status IN ('planning', 'active', 'paused')
                            """,
                            (task["goal_id"],),
                        ).fetchone()
                    if active_job:
                        raise TeamError(
                            "cannot archive task with active job: "
                            f"{active_job['job_id']}"
                        )
                    if active_goal:
                        raise TeamError(
                            "cannot archive task in unfinished goal: "
                            f"{active_goal['goal_id']}"
                        )
                else:
                    if task["project_archived_at"]:
                        raise TeamError(
                            "project is archived; restore it before restoring tasks"
                        )
                    if task["archived_by_goal"]:
                        raise TeamError(
                            "task was archived by its goal; restore the goal first"
                        )
                    if task["goal_id"]:
                        goal = connection.execute(
                            """
                            SELECT archived_at FROM cluster_goals
                            WHERE goal_id = ?
                            """,
                            (task["goal_id"],),
                        ).fetchone()
                        if goal is not None and goal["archived_at"]:
                            raise TeamError(
                                "goal is archived; restore it before restoring tasks"
                            )
                connection.execute(
                    """
                    UPDATE tasks
                    SET archived_at = ?, archived_by_goal = 0,
                        updated_at = ?, version = version + 1
                    WHERE task_id = ?
                    """,
                    (now if archived else None, now, task_id),
                )
                self._event(
                    connection,
                    task_id,
                    "archived" if archived else "restored",
                    actor,
                    {"archived": archived},
                )
        return self.get_task(task_id)

    def init_project(
        self,
        repo: str | Path,
        *,
        max_threads: int | None = None,
    ) -> dict[str, Any]:
        project = self.register_project(repo)
        self._ensure_project_active(project)
        root = Path(project["root_path"])
        roles_dir = root / ".codex" / "agents"
        roles_dir.mkdir(parents=True, exist_ok=True)
        prompt_roles_dir = root / ".codex" / "team-roles"
        prompt_roles_dir.mkdir(parents=True, exist_ok=True)
        marker_path = root / ".codex" / "team-project.json"
        existing_marker: dict[str, Any] = {}
        if marker_path.is_file():
            try:
                loaded = json.loads(marker_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    existing_marker = loaded
            except (OSError, ValueError):
                pass
        if max_threads is None:
            try:
                max_threads = int(existing_marker.get("max_threads", 4))
            except (TypeError, ValueError):
                max_threads = 4
        max_threads = int(max_threads)
        if max_threads < 1 or max_threads > 8:
            raise TeamError("max_threads must be between 1 and 8")

        template_dir = self.settings.project_root / "templates" / "agents"
        prompt_template_dir = self.settings.project_root / "templates" / "roles"
        created_roles: list[str] = []
        for template_name, target_name in (
            ("team-explorer.toml", "team-explorer.toml"),
            ("team-worker.toml", "team-worker.toml"),
            ("team-reviewer.toml", "team-reviewer.toml"),
        ):
            source = template_dir / template_name
            target = roles_dir / target_name
            expected = source.read_bytes()
            if not target.exists() or target.read_bytes() != expected:
                target.write_bytes(expected)
                created_roles.append(str(target))
        for template_name in self.ROLE_FILES.values():
            source = prompt_template_dir / template_name
            target = prompt_roles_dir / template_name
            expected = source.read_bytes()
            if not target.exists() or target.read_bytes() != expected:
                target.write_bytes(expected)
                created_roles.append(str(target))

        agents_path = root / "AGENTS.md"
        existing = agents_path.read_text(encoding="utf-8") if agents_path.exists() else ""
        block = _agents_block(
            project["project_id"],
            max_threads,
            self.settings.project_root / "templates" / "PROJECT_AGENTS.md",
        )
        updated = _replace_managed_block(
            existing,
            "CODEX_TEAM_BOARD",
            block,
        )
        if updated != existing:
            agents_path.write_bytes((updated.rstrip() + "\n").encode("utf-8"))

        marker_path.write_bytes(
            (
                json.dumps(
                    {
                        "project_id": project["project_id"],
                        "project_name": project["name"],
                        "root_path": project["root_path"],
                        "max_threads": max_threads,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            ).encode("utf-8")
        )
        return {
            **project,
            "agents_md": str(agents_path),
            "roles_created": created_roles,
            "team_marker": str(marker_path),
            "max_threads": max_threads,
        }

    def role_prompt(self, role: str, repo: str | Path | None = None) -> dict[str, Any]:
        normalized = role.strip()
        project_root = self._root(repo) if repo else None
        return self._role_prompt_from_root(normalized, project_root)

    def _role_prompt_from_root(
        self,
        role: str,
        project_root: Path | None,
    ) -> dict[str, Any]:
        normalized, path, source = self._role_prompt_path(
            role,
            project_root,
        )
        return {
            "role": normalized,
            "prompt": path.read_text(encoding="utf-8"),
            "source": str(path),
            "scope": source,
        }

    def _role_prompt_path(
        self,
        role: str,
        project_root: Path | None,
    ) -> tuple[str, Path, str]:
        normalized = role.strip()
        template_name = self.ROLE_FILES.get(normalized)
        if template_name is None:
            raise TeamError(
                "unknown role; expected one of: " + ", ".join(sorted(self.ROLE_FILES))
            )
        path = self.settings.project_root / "templates" / "roles" / template_name
        source = "global"
        if project_root is not None:
            project_path = project_root / ".codex" / "team-roles" / template_name
            if project_path.is_file():
                path = project_path
                source = "project"
        if not path.is_file():
            raise TeamError(f"role prompt template missing: {path}")
        return normalized, path, source

    @staticmethod
    def _path_stamp(path: Path) -> tuple[str, int, int]:
        try:
            stat = path.stat()
            return str(path), stat.st_mtime_ns, stat.st_size
        except OSError:
            return str(path), 0, 0

    def current_workflow_epoch(
        self,
        repo: str | Path,
        *,
        resolved_root: bool = False,
    ) -> str:
        root = (
            Path(repo).expanduser().resolve()
            if resolved_root
            else self._root(repo)
        )
        role_paths = {
            role: self._role_prompt_path(role, root)[1]
            for role in sorted(self.ROLE_FILES)
        }
        project_agent_dir = root / ".codex" / "agents"
        global_agent_dir = (
            self.settings.project_root / "templates" / "agents"
        )
        native_role_hashes: dict[str, str] = {}
        native_paths: list[Path] = []
        for role, filename in {
            "lead": None,
            "team_explorer": "team-explorer.toml",
            "team_worker": "team-worker.toml",
            "team_reviewer": "team-reviewer.toml",
        }.items():
            if filename is None:
                native_role_hashes[role] = file_hash(None)
                continue
            project_path = project_agent_dir / filename
            global_path = global_agent_dir / filename
            selected_path = (
                project_path if project_path.is_file() else global_path
            )
            native_paths.append(selected_path)
            native_role_hashes[role] = file_hash(selected_path)
        runtime_mode = (
            os.environ.get("CODEX_TEAM_AGENT_RUNTIME", "app_server")
            .strip()
            .lower()
            or "app_server"
        )
        command_identity = os.environ.get(
            "CODEX_TEAM_CODEX_COMMAND",
            "",
        ).strip()
        policy_path = root / "AGENTS.md"
        environment_signature = (
            runtime_mode,
            command_identity,
            os.environ.get("CODEX_TEAM_MODEL", ""),
            os.environ.get("CODEX_TEAM_MODEL_PROVIDER", ""),
            os.environ.get("CODEX_TEAM_MCP_SURFACE", "core"),
            os.environ.get("CODEX_TEAM_MCP_ACCESS", "full"),
        )
        signature = (
            *environment_signature,
            *(self._path_stamp(path) for path in role_paths.values()),
            *(self._path_stamp(path) for path in native_paths),
            self._path_stamp(policy_path),
        )
        cache_key = str(root)
        cached = self._workflow_epoch_cache.get(cache_key)
        if cached is not None and cached[0] == signature:
            return cached[1]
        role_prompts = {
            role: path.read_text(encoding="utf-8")
            for role, path in role_paths.items()
        }
        epoch = build_workflow_epoch(
            policy_hash=file_hash(root / "AGENTS.md"),
            role_prompts=role_prompts,
            native_role_hashes=native_role_hashes,
            runtime=f"{runtime_mode}:{command_identity}",
            model=os.environ.get("CODEX_TEAM_MODEL", ""),
            model_provider=os.environ.get(
                "CODEX_TEAM_MODEL_PROVIDER",
                "",
            ),
        )
        self._workflow_epoch_cache[cache_key] = (signature, epoch)
        return epoch

    def list_roles(self) -> list[dict[str, Any]]:
        return [
            {
                "role": role,
                "prompt_file": str(
                    self.settings.project_root / "templates" / "roles" / filename
                ),
            }
            for role, filename in sorted(self.ROLE_FILES.items())
        ]

    def create_task(
        self,
        repo: str | Path,
        *,
        title: str,
        description: str,
        acceptance: str,
        kind: str,
        owner_role: str,
        actor: str = "lead",
        priority: int = 2,
        depends_on: list[str] | None = None,
        goal_id: str | None = None,
        continuity_mode: str = "auto",
    ) -> dict[str, Any]:
        if kind not in {"read", "write", "review", "ops"}:
            raise TeamError("kind must be read, write, review, or ops")
        if not title.strip():
            raise TeamError("title must not be empty")
        if not owner_role.strip():
            raise TeamError("owner_role must not be empty")
        if isinstance(priority, bool) or priority not in {0, 1, 2, 3}:
            raise TeamError("priority must be between 0 and 3")
        if not isinstance(continuity_mode, str):
            raise TeamError("continuity_mode must be auto, reuse, or fresh")
        continuity_mode = continuity_mode.strip().lower()
        if continuity_mode not in {"auto", "reuse", "fresh"}:
            raise TeamError("continuity_mode must be auto, reuse, or fresh")
        project = self.register_project(repo)
        self._ensure_project_active(project)
        workflow_epoch_value = self.current_workflow_epoch(
            project["root_path"],
            resolved_root=True,
        )
        dependencies = list(dict.fromkeys(depends_on or []))
        task_id = self._new_task_id()
        now = utc_now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if goal_id:
                goal = connection.execute(
                    """
                    SELECT project_id, status, archived_at, workflow_epoch
                    FROM cluster_goals
                    WHERE goal_id = ?
                    """,
                    (goal_id,),
                ).fetchone()
                if goal is None or goal["project_id"] != project["project_id"]:
                    raise TeamError("goal does not belong to this project")
                if goal["archived_at"]:
                    raise TeamError("goal is archived; restore it before creating tasks")
                if goal["status"] in {"completed", "failed"}:
                    raise TeamError(
                        f"goal is terminal ({goal['status']}); create or resume an active goal"
                    )
                if goal["workflow_epoch"] != workflow_epoch_value:
                    raise TeamError(
                        "goal workflow epoch is incompatible with current "
                        "architecture; migrate or archive the goal"
                    )
            for dependency in dependencies:
                row = connection.execute(
                    "SELECT project_id FROM tasks WHERE task_id = ?",
                    (dependency,),
                ).fetchone()
                if row is None or row["project_id"] != project["project_id"]:
                    raise TeamError(f"dependency not found in this project: {dependency}")
            connection.execute(
                """
                INSERT INTO tasks(
                    task_id, project_id, goal_id, title, description, acceptance, kind,
                    status, priority, owner_role, continuity_mode, workflow_epoch,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    project["project_id"],
                    goal_id,
                    title.strip(),
                    description.strip(),
                    acceptance.strip(),
                    kind,
                    priority,
                    owner_role.strip(),
                    continuity_mode,
                    workflow_epoch_value,
                    now,
                    now,
                ),
            )
            for dependency in dependencies:
                connection.execute(
                    """
                    INSERT INTO task_dependencies(task_id, depends_on)
                    VALUES (?, ?)
                    """,
                    (task_id, dependency),
                )
            self._event(
                connection,
                task_id,
                "created",
                actor,
                {
                    "kind": kind,
                    "owner_role": owner_role,
                    "depends_on": dependencies,
                    "goal_id": goal_id,
                    "continuity_mode": continuity_mode,
                    "workflow_epoch": workflow_epoch_value,
                },
            )
            self._record_request_result(
                connection,
                {"task_id": task_id},
                task_id=task_id,
                goal_id=goal_id,
            )
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> dict[str, Any]:
        with self.connection(write=False) as connection:
            row = connection.execute(
                """
                SELECT t.*, p.name AS project_name, p.root_path
                FROM tasks t JOIN projects p ON p.project_id = t.project_id
                WHERE t.task_id = ?
                """,
                (task_id,),
            ).fetchone()
            if row is None:
                raise TeamError(f"task not found: {task_id}")
            task = dict(row)
            dependencies = connection.execute(
                """
                SELECT d.depends_on, t.status, t.title
                FROM task_dependencies d
                JOIN tasks t ON t.task_id = d.depends_on
                WHERE d.task_id = ?
                ORDER BY d.depends_on
                """,
                (task_id,),
            ).fetchall()
            dependents = connection.execute(
                """
                SELECT d.task_id, t.status, t.title
                FROM task_dependencies d
                JOIN tasks t ON t.task_id = d.task_id
                WHERE d.depends_on = ?
                ORDER BY d.task_id
                """,
                (task_id,),
            ).fetchall()
            task["depends_on"] = [dict(item) for item in dependencies]
            task["dependents"] = [dict(item) for item in dependents]
            task["ready"] = self._is_ready(connection, task_id)
            task["blocked_by"] = self._blocking_tasks(connection, task_id)
            task["lease_expired"] = self._lease_expired(task.get("lease_until"))
            task.pop("claim_token", None)
            return task

    def get_task_events(
        self,
        task_id: str,
        *,
        limit: int = 100,
        before_event_id: int | None = None,
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit), 500))
        where = ["task_id = ?"]
        parameters: list[Any] = [task_id]
        if before_event_id is not None:
            where.append("event_id < ?")
            parameters.append(int(before_event_id))
        with self.connection(write=False) as connection:
            task = connection.execute(
                "SELECT task_id, title FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if task is None:
                raise TeamError(f"task not found: {task_id}")
            rows = connection.execute(
                f"""
                SELECT event_id, event_type, actor, thread_id, job_id,
                       correlation_id, assignment_id, data_json, created_at
                FROM task_events
                WHERE {' AND '.join(where)}
                ORDER BY event_id DESC
                LIMIT ?
                """,
                (*parameters, limit + 1),
            ).fetchall()
        has_more = len(rows) > limit
        page = rows[:limit]
        events = []
        for row in reversed(page):
            item = dict(row)
            try:
                item["data"] = json.loads(item.pop("data_json"))
            except (TypeError, ValueError):
                item["data"] = {}
                item.pop("data_json", None)
            events.append(item)
        return {
            "task": dict(task),
            "events": events,
            "has_more": has_more,
            "next_cursor": (
                min(int(row["event_id"]) for row in page)
                if has_more and page
                else None
            ),
        }

    def list_tasks(
        self,
        repo: str | Path | None = None,
        *,
        statuses: list[str] | None = None,
        owner_role: str | None = None,
        goal_id: str | None = None,
        include_done: bool = True,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        project = self.register_project(repo or Path.cwd())
        where = ["t.project_id = ?"]
        parameters: list[Any] = [project["project_id"]]
        if statuses:
            where.append(f"t.status IN ({','.join('?' for _ in statuses)})")
            parameters.extend(statuses)
        elif not include_done:
            where.append("t.status NOT IN ('done', 'canceled')")
        if owner_role:
            where.append("t.owner_role = ?")
            parameters.append(owner_role)
        if goal_id:
            where.append("t.goal_id = ?")
            parameters.append(goal_id)
        if not include_archived:
            where.append("t.archived_at IS NULL")
            where.append(
                "(g.goal_id IS NULL OR g.archived_at IS NULL)"
            )
        with self.connection(write=False) as connection:
            rows = connection.execute(
                f"""
                SELECT t.*, p.name AS project_name, p.root_path
                FROM tasks t
                JOIN projects p ON p.project_id = t.project_id
                LEFT JOIN cluster_goals g ON g.goal_id = t.goal_id
                WHERE {' AND '.join(where)}
                ORDER BY
                    CASE t.status
                        WHEN 'running' THEN 0
                        WHEN 'review' THEN 1
                        WHEN 'ready' THEN 2
                        WHEN 'blocked' THEN 3
                        WHEN 'backlog' THEN 4
                        WHEN 'done' THEN 5
                        ELSE 6
                    END,
                    t.priority,
                    t.created_at
                """,
                parameters,
            ).fetchall()
            task_ids = [str(row["task_id"]) for row in rows]
            blocked_by = self._blocking_tasks_map(connection, task_ids)
            result = []
            for row in rows:
                item = dict(row)
                item.pop("claim_token", None)
                blockers = blocked_by.get(str(item["task_id"]), [])
                item["blocked_by"] = blockers
                item["ready"] = (
                    item["status"] in {"ready", "backlog"} and not blockers
                )
                item["lease_expired"] = self._lease_expired(item.get("lease_until"))
                result.append(item)
            return result

    def board_snapshot(
        self,
        repo: str | Path | None = None,
        *,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        project = self.register_project(repo or Path.cwd())
        tasks = self.list_tasks(
            repo,
            include_done=True,
            include_archived=include_archived,
        )
        counts: dict[str, int] = {}
        for task in tasks:
            counts[task["status"]] = counts.get(task["status"], 0) + 1
        return {
            "project": project,
            "counts": counts,
            "tasks": tasks,
            "generated_at": utc_now(),
        }

    @staticmethod
    def _begin_snapshot(connection: sqlite3.Connection) -> None:
        """Pin all reads in this method to one SQLite snapshot."""

        connection.execute("BEGIN DEFERRED")

    @staticmethod
    def _dashboard_revision(
        connection: sqlite3.Connection,
        project_id: str | None,
        include_archived: bool,
    ) -> str:
        del project_id, include_archived
        return f"v2:{read_dashboard_revision(connection)}"

    def dashboard_revision(
        self,
        project_id: str | None = None,
        *,
        include_archived: bool = False,
    ) -> str:
        with self.connection(write=False) as connection:
            self._begin_snapshot(connection)
            return self._dashboard_revision(
                connection,
                project_id,
                include_archived,
            )

    def dashboard_summary(
        self,
        project_id: str | None = None,
        *,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        """Lightweight summary sharing dashboard_snapshot's exact semantics.

        The cluster_summary numbers must match dashboard_snapshot for the same
        scope; only the heavy row payloads (tasks/goals/agents/events) are
        omitted here so the polling probe stays cheap.
        """
        system_capacity = env_int(
            "CODEX_TEAM_CLUSTER_MAX_JOBS",
            DEFAULT_GLOBAL_JOB_CAPACITY,
            maximum=MAX_GLOBAL_JOB_CAPACITY,
        )
        goal_capacity = env_int(
            "CODEX_TEAM_CLUSTER_MAX_WORKERS",
            DEFAULT_GOAL_WORKER_CAPACITY,
            maximum=MAX_GOAL_WORKER_CAPACITY,
        )
        heartbeat_warning_seconds = env_int(
            "CODEX_TEAM_HEARTBEAT_WARNING_SECONDS",
            DEFAULT_HEARTBEAT_WARNING_SECONDS,
        )
        with self.connection(write=False) as connection:
            self._begin_snapshot(connection)
            if project_id:
                projects = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT * FROM projects WHERE project_id = ?",
                        (project_id,),
                    ).fetchall()
                ]
            else:
                where = "" if include_archived else "WHERE archived_at IS NULL"
                projects = [
                    dict(row)
                    for row in connection.execute(
                        f"SELECT * FROM projects {where} ORDER BY updated_at DESC"
                    ).fetchall()
                ]
            task_where = ["1 = 1"]
            task_params: list[Any] = []
            goal_where = ["1 = 1"]
            goal_params: list[Any] = []
            if project_id:
                task_where.append("t.project_id = ?")
                task_params.append(project_id)
                goal_where.append("g.project_id = ?")
                goal_params.append(project_id)
            else:
                task_where.append("t.status != 'canceled'")
                task_where.append("p.archived_at IS NULL")
                goal_where.append("p.archived_at IS NULL")
            if not include_archived:
                task_where.append("t.archived_at IS NULL")
                task_where.append(
                    "(g.goal_id IS NULL OR g.archived_at IS NULL)"
                )
                goal_where.append("g.archived_at IS NULL")
            task_status_rows = connection.execute(
                f"""
                SELECT t.status, COUNT(*) AS count
                FROM tasks t
                JOIN projects p ON p.project_id = t.project_id
                LEFT JOIN cluster_goals g ON g.goal_id = t.goal_id
                WHERE {' AND '.join(task_where)}
                GROUP BY t.status
                """,
                tuple(task_params),
            ).fetchall()
            counts = {
                str(row["status"]): int(row["count"])
                for row in task_status_rows
            }
            goal_rows = connection.execute(
                f"""
                SELECT g.goal_id, g.project_id, g.status, g.last_error,
                       g.archived_at
                FROM cluster_goals g
                JOIN projects p ON p.project_id = g.project_id
                WHERE {' AND '.join(goal_where)}
                """,
                tuple(goal_params),
            ).fetchall()
            goals = [dict(row) for row in goal_rows]
            for goal in goals:
                goal["status"] = effective_goal_status(
                    goal["status"], goal.get("last_error")
                )
            goal_counts: dict[str, int] = {}
            for goal in goals:
                status = str(goal["status"])
                goal_counts[status] = goal_counts.get(status, 0) + 1
            job_row = connection.execute(
                f"""
                SELECT COUNT(*) AS active_jobs,
                       SUM(CASE WHEN j.kind = 'worker' THEN 1 ELSE 0 END)
                           AS active_workers,
                       SUM(CASE WHEN j.kind = 'lead' THEN 1 ELSE 0 END)
                           AS active_leads
                FROM cluster_jobs j
                JOIN cluster_goals g ON g.goal_id = j.goal_id
                JOIN projects p ON p.project_id = g.project_id
                WHERE j.status IN ('starting', 'running')
                  AND {' AND '.join(goal_where)}
                """,
                tuple(goal_params),
            ).fetchone()
            active_job_rows = connection.execute(
                f"""
                SELECT j.job_id, j.goal_id, j.kind, j.task_id, j.started_at
                FROM cluster_jobs j
                JOIN cluster_goals g ON g.goal_id = j.goal_id
                JOIN projects p ON p.project_id = g.project_id
                WHERE j.status IN ('starting', 'running')
                  AND {' AND '.join(goal_where)}
                """,
                tuple(goal_params),
            ).fetchall()
            active_jobs = [dict(row) for row in active_job_rows]
            active_task_ids = [
                str(row["task_id"]) for row in active_jobs if row["task_id"]
            ]
            latest_activity: dict[str, str] = {}
            if active_task_ids:
                placeholders = ",".join("?" for _ in active_task_ids)
                activity_rows = connection.execute(
                    f"""
                    SELECT task_id, MAX(created_at) AS latest_at
                    FROM task_events
                    WHERE task_id IN ({placeholders})
                      AND event_type IN ('claimed', 'heartbeat', 'progress')
                    GROUP BY task_id
                    """,
                    active_task_ids,
                ).fetchall()
                latest_activity = {
                    str(row["task_id"]): str(row["latest_at"])
                    for row in activity_rows
                }
            now_utc = datetime.now(UTC)
            worker_ages = [
                worker_heartbeat_age(job, latest_activity, now_utc)
                for job in active_jobs
                if job["kind"] == "worker"
            ]
            schedulable_goal_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) AS goal_count
                    FROM cluster_goals g
                    JOIN projects p ON p.project_id = g.project_id
                    WHERE g.status IN ('planning', 'active')
                      AND g.archived_at IS NULL
                      AND p.archived_at IS NULL
                    """
                ).fetchone()["goal_count"]
            )
            task_rows = connection.execute(
                f"""
                SELECT t.task_id, t.goal_id, t.status, t.created_at,
                       t.updated_at, t.claimed_by, t.archived_at,
                       t.project_id
                FROM tasks t
                JOIN projects p ON p.project_id = t.project_id
                LEFT JOIN cluster_goals g ON g.goal_id = t.goal_id
                WHERE {' AND '.join(task_where)}
                """,
                tuple(task_params),
            ).fetchall()
            tasks = [dict(row) for row in task_rows]
            archived_project_ids = {
                str(project["project_id"])
                for project in projects
                if project.get("archived_at")
            }
            archived_goal_ids = {
                str(goal["goal_id"])
                for goal in goals
                if goal.get("archived_at")
            }
            task_ids = [str(task["task_id"]) for task in tasks]
            blocked_ids: set[str] = set()
            if task_ids:
                placeholders = ",".join("?" for _ in task_ids)
                dependency_rows = connection.execute(
                    f"""
                    SELECT d.task_id
                    FROM task_dependencies d
                    JOIN tasks t ON t.task_id = d.depends_on
                    WHERE d.task_id IN ({placeholders}) AND t.status != 'done'
                    """,
                    task_ids,
                ).fetchall()
                blocked_ids = {str(row["task_id"]) for row in dependency_rows}
            active_jobs_by_task = {
                str(job["task_id"]) for job in active_jobs if job.get("task_id")
            }
            claimed_actors = sorted(
                {
                    str(task["claimed_by"])
                    for task in tasks
                    if str(task["status"]) == "running" and task.get("claimed_by")
                }
            )
            native_actors: set[str] = set()
            if claimed_actors:
                placeholders = ",".join("?" for _ in claimed_actors)
                native_rows = connection.execute(
                    f"""
                    SELECT DISTINCT actor
                    FROM cluster_agents
                    WHERE runtime = 'native' AND archived_at IS NULL
                      AND actor IN ({placeholders})
                    """,
                    claimed_actors,
                ).fetchall()
                native_actors = {str(row["actor"]) for row in native_rows}
            active_jobs_by_goal: dict[str, int] = {}
            active_workers_by_goal: dict[str, int] = {}
            for job in active_jobs:
                goal_id = str(job.get("goal_id") or "")
                if not goal_id:
                    continue
                active_jobs_by_goal[goal_id] = (
                    active_jobs_by_goal.get(goal_id, 0) + 1
                )
                if job.get("kind") == "worker":
                    active_workers_by_goal[goal_id] = (
                        active_workers_by_goal.get(goal_id, 0) + 1
                    )
            attention_count = 0
            external_executions = 0
            native_executions = 0
            goal_task_flags: dict[str, dict[str, bool]] = {}
            for task in tasks:
                if is_historical_task(
                    task, archived_project_ids, archived_goal_ids
                ):
                    continue
                status = str(task["status"])
                execution_source = ""
                if str(task["task_id"]) in active_jobs_by_task:
                    execution_source = "managed_job"
                elif status == "running" and task.get("claimed_by"):
                    claimed_by = str(task["claimed_by"])
                    if claimed_by.startswith("auto-"):
                        execution_source = "dispatch_reservation"
                    elif claimed_by in native_actors:
                        execution_source = "native_claim"
                    else:
                        execution_source = "external_claim"
                if execution_source and execution_source != "managed_job":
                    external_executions += 1
                    if execution_source == "native_claim":
                        native_executions += 1
                wait_seconds = 0
                if status == "ready" and str(task["task_id"]) not in blocked_ids:
                    wait_seconds = age_seconds(task.get("created_at"), now_utc)
                wait_state = claim_wait_state(wait_seconds)
                task_attention = (
                    status in {"blocked", "review"} or wait_state == "critical"
                )
                goal_id = str(task.get("goal_id") or "")
                if not goal_id:
                    if task_attention:
                        attention_count += 1
                    continue
                if task_attention:
                    attention_count += 1
                flags = goal_task_flags.setdefault(
                    goal_id,
                    {
                        "has_blocked": False,
                        "has_review": False,
                        "has_critical_wait": False,
                        "has_warning_wait": False,
                        "has_ready": False,
                        "has_dispatchable_ready": False,
                        "has_running": False,
                    },
                )
                flags["has_blocked"] = flags["has_blocked"] or status == "blocked"
                flags["has_review"] = flags["has_review"] or status == "review"
                flags["has_critical_wait"] = (
                    flags["has_critical_wait"] or wait_state == "critical"
                )
                flags["has_warning_wait"] = (
                    flags["has_warning_wait"] or wait_state == "warning"
                )
                flags["has_ready"] = flags["has_ready"] or status == "ready"
                flags["has_dispatchable_ready"] = flags[
                    "has_dispatchable_ready"
                ] or (
                    status == "ready"
                    and str(task["task_id"]) not in blocked_ids
                )
                flags["has_running"] = flags["has_running"] or status == "running"
            queued_goals = 0
            fairness_wait_count = 0
            total_active_jobs = len(active_jobs)
            for goal in goals:
                goal_id = str(goal["goal_id"])
                goal_status = str(goal["status"])
                is_historical_goal = bool(goal.get("archived_at")) or (
                    str(goal["project_id"]) in archived_project_ids
                )
                if is_historical_goal:
                    continue
                flags = goal_task_flags.get(goal_id, {})
                goal_active_jobs = active_jobs_by_goal.get(goal_id, 0)
                goal_active_workers = active_workers_by_goal.get(goal_id, 0)
                queue_reason = goal_queue_reason(
                    goal_status,
                    has_dispatchable_ready=bool(
                        flags.get("has_dispatchable_ready")
                    ),
                    has_ready=bool(flags.get("has_ready")),
                    has_review=bool(flags.get("has_review")),
                    has_blocked=bool(flags.get("has_blocked")),
                    has_running=bool(flags.get("has_running")),
                    goal_active_jobs=goal_active_jobs,
                    goal_active_workers=goal_active_workers,
                    total_active_jobs=total_active_jobs,
                    system_capacity=system_capacity,
                    goal_capacity=goal_capacity,
                )
                if goal_status in {"planning", "active"} and queue_reason not in {
                    "running",
                    "none",
                }:
                    queued_goals += 1
                if (
                    goal_status in {"planning", "active"}
                    and queue_reason == "global_capacity"
                ):
                    fairness_wait_count += 1
                if goal_status in {"paused", "failed"}:
                    attention_count += 1
            heartbeat_overdue = sum(
                age >= heartbeat_warning_seconds for age in worker_ages
            )
            revision = self._dashboard_revision(
                connection,
                project_id,
                include_archived,
            )
            generated_at = utc_now()
        cluster_summary = {
            "goals_total": len(goals),
            "goals_active": goal_counts.get("active", 0),
            "goals_paused": goal_counts.get("paused", 0),
            "goals_failed": goal_counts.get("failed", 0),
            "ready_tasks": counts.get("ready", 0),
            "running_tasks": counts.get("running", 0),
            "review_tasks": counts.get("review", 0),
            "blocked_tasks": counts.get("blocked", 0),
            "active_jobs": int(job_row["active_jobs"] or 0),
            "active_executions": int(job_row["active_jobs"] or 0)
            + external_executions,
            "external_executions": external_executions,
            "native_executions": native_executions,
            "active_workers": int(job_row["active_workers"] or 0),
            "active_leads": int(job_row["active_leads"] or 0),
            "global_jobs": int(job_row["active_workers"] or 0),
            "global_capacity": schedulable_goal_count * goal_capacity,
            "worker_capacity_per_goal": goal_capacity,
            "system_jobs": int(job_row["active_jobs"] or 0),
            "system_capacity": system_capacity,
            "queued_goals": queued_goals,
            "heartbeat_overdue": heartbeat_overdue,
            "oldest_heartbeat_age_seconds": max(worker_ages, default=0),
            "fairness_wait_count": fairness_wait_count,
            "attention_count": attention_count,
            "oldest_claim_wait_seconds": max(
                (
                    age_seconds(task.get("created_at"), now_utc)
                    for task in tasks
                    if str(task["status"]) == "ready"
                    and not is_historical_task(
                        task, archived_project_ids, archived_goal_ids
                    )
                ),
                default=0,
            ),
        }
        return {
            "projects": projects,
            "selected_project_id": project_id,
            "counts": counts,
            "cluster_summary": cluster_summary,
            "revision": revision,
            "generated_at": generated_at,
        }

    def dashboard_events(
        self,
        project_id: str | None = None,
        *,
        include_archived: bool = False,
        limit: int = 100,
        before_event_id: int | None = None,
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit), 500))
        where = ["1 = 1"]
        parameters: list[Any] = []
        if project_id:
            where.append("t.project_id = ?")
            parameters.append(project_id)
        # Apply the same default visibility rules regardless of scope: canceled
        # tasks and archived projects stay hidden unless explicitly included.
        where.append("t.status != 'canceled'")
        if not include_archived:
            where.append("p.archived_at IS NULL")
            where.append("t.archived_at IS NULL")
            where.append("(g.goal_id IS NULL OR g.archived_at IS NULL)")
        if before_event_id is not None:
            where.append("e.event_id < ?")
            parameters.append(int(before_event_id))
        with self.connection(write=False) as connection:
            self._begin_snapshot(connection)
            rows = connection.execute(
                f"""
                SELECT e.event_id, e.task_id, e.event_type, e.actor,
                       e.thread_id, e.job_id, e.correlation_id,
                       e.assignment_id, e.data_json, e.created_at
                FROM task_events e
                JOIN tasks t ON t.task_id = e.task_id
                JOIN projects p ON p.project_id = t.project_id
                LEFT JOIN cluster_goals g ON g.goal_id = t.goal_id
                WHERE {' AND '.join(where)}
                ORDER BY e.event_id DESC
                LIMIT ?
                """,
                (*parameters, limit + 1),
            ).fetchall()
            revision = self._dashboard_revision(
                connection,
                project_id,
                include_archived,
            )
            generated_at = utc_now()
        has_more = len(rows) > limit
        page = rows[:limit]
        events = []
        for row in reversed(page):
            item = dict(row)
            try:
                item["data"] = json.loads(item.pop("data_json"))
            except (TypeError, ValueError):
                item["data"] = {}
                item.pop("data_json", None)
            events.append(item)
        return {
            "events": events,
            "has_more": has_more,
            "next_cursor": (
                min(int(row["event_id"]) for row in page)
                if has_more and page
                else None
            ),
            "revision": revision,
            "generated_at": generated_at,
        }

    def dashboard_partition(
        self,
        partition: str,
        project_id: str | None = None,
        *,
        include_archived: bool = False,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        partitions = {
            "tasks": ("tasks", "task_id"),
            "goals": ("cluster_goals", "goal_id"),
            "agents": ("cluster_agents", "agent_id"),
            "attempts": ("cluster_jobs", "job_id"),
        }
        if partition not in partitions:
            raise ValueError(f"unknown dashboard partition: {partition}")
        data_key, id_field = partitions[partition]
        limit = max(1, min(int(limit), 500))
        snapshot = self.dashboard_snapshot(
            project_id,
            include_archived=include_archived,
        )
        items = list(snapshot[data_key])
        start = 0
        if cursor:
            positions = {
                str(item.get(id_field)): index
                for index, item in enumerate(items)
                if item.get(id_field) is not None
            }
            if cursor not in positions:
                raise TeamError(f"dashboard cursor not found: {cursor}")
            start = positions[cursor] + 1
        page = items[start : start + limit]
        has_more = start + len(page) < len(items)
        return {
            "items": page,
            "total": len(items),
            "has_more": has_more,
            "next_cursor": (
                str(page[-1].get(id_field))
                if has_more and page and page[-1].get(id_field) is not None
                else None
            ),
            "selected_project_id": project_id,
            "include_archived": include_archived,
            "revision": snapshot["revision"],
            "generated_at": snapshot["generated_at"],
        }

    def dashboard_snapshot(
        self,
        project_id: str | None = None,
        *,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        system_capacity = env_int(
            "CODEX_TEAM_CLUSTER_MAX_JOBS",
            DEFAULT_GLOBAL_JOB_CAPACITY,
            maximum=MAX_GLOBAL_JOB_CAPACITY,
        )
        goal_capacity = env_int(
            "CODEX_TEAM_CLUSTER_MAX_WORKERS",
            DEFAULT_GOAL_WORKER_CAPACITY,
            maximum=MAX_GOAL_WORKER_CAPACITY,
        )
        heartbeat_warning_seconds = env_int(
            "CODEX_TEAM_HEARTBEAT_WARNING_SECONDS",
            DEFAULT_HEARTBEAT_WARNING_SECONDS,
        )
        heartbeat_critical_seconds = max(
            heartbeat_warning_seconds,
            env_int(
                "CODEX_TEAM_HEARTBEAT_CRITICAL_SECONDS",
                DEFAULT_HEARTBEAT_CRITICAL_SECONDS,
            ),
        )
        with self.connection(write=False) as connection:
            self._begin_snapshot(connection)
            if project_id:
                project_rows = connection.execute(
                    "SELECT * FROM projects WHERE project_id = ?",
                    (project_id,),
                ).fetchall()
            else:
                where = "" if include_archived else "WHERE archived_at IS NULL"
                project_rows = connection.execute(
                    f"SELECT * FROM projects {where} ORDER BY updated_at DESC"
                ).fetchall()
            projects = [dict(row) for row in project_rows]
            if project_id:
                goal_archive_filter = (
                    "" if include_archived else "AND g.archived_at IS NULL"
                )
                task_goal_filter = (
                    ""
                    if include_archived
                    else "AND (tg.goal_id IS NULL OR tg.archived_at IS NULL)"
                )
                goal_rows = connection.execute(
                    f"""
                    SELECT g.*, p.name AS project_name, p.root_path
                    FROM cluster_goals g
                    JOIN projects p ON p.project_id = g.project_id
                    WHERE g.project_id = ?
                    {goal_archive_filter}
                    ORDER BY g.updated_at DESC
                    """,
                    (project_id,),
                ).fetchall()
                job_rows = connection.execute(
                    f"""
                    SELECT j.*, g.objective
                    FROM cluster_jobs j JOIN cluster_goals g ON g.goal_id = j.goal_id
                    WHERE g.project_id = ?
                    {goal_archive_filter}
                    ORDER BY j.started_at DESC LIMIT 100
                    """,
                    (project_id,),
                ).fetchall()
            else:
                goal_archive_filter = (
                    ""
                    if include_archived
                    else "WHERE p.archived_at IS NULL AND g.archived_at IS NULL"
                )
                task_goal_filter = (
                    ""
                    if include_archived
                    else "AND (tg.goal_id IS NULL OR tg.archived_at IS NULL)"
                )
                goal_rows = connection.execute(
                    f"""
                    SELECT g.*, p.name AS project_name, p.root_path
                    FROM cluster_goals g
                    JOIN projects p ON p.project_id = g.project_id
                    {goal_archive_filter}
                    ORDER BY g.updated_at DESC
                    """
                ).fetchall()
                job_rows = connection.execute(
                    f"""
                    SELECT j.*, g.objective
                    FROM cluster_jobs j
                    JOIN cluster_goals g ON g.goal_id = j.goal_id
                    JOIN projects p ON p.project_id = g.project_id
                    {goal_archive_filter}
                    ORDER BY j.started_at DESC LIMIT 100
                    """
                ).fetchall()
            cluster_goals = [dict(row) for row in goal_rows]
            for goal in cluster_goals:
                raw_status = str(goal["status"])
                effective_status = effective_goal_status(
                    raw_status,
                    goal.get("last_error"),
                )
                goal["raw_status"] = raw_status
                goal["effective_status"] = effective_status
                goal["status"] = effective_status
            cluster_jobs = [dict(row) for row in job_rows]
            for job in cluster_jobs:
                self._project_unknown_usage(job)
                job["cache_hit_rate"] = self._cache_hit_rate(
                    job.get("input_tokens"),
                    job.get("cached_input_tokens"),
                    job.get("uncached_input_tokens"),
                )
            agent_where = ["1 = 1"]
            agent_parameters: list[Any] = []
            if project_id:
                agent_where.append("a.project_id = ?")
                agent_parameters.append(project_id)
            if not include_archived:
                agent_where.append("a.archived_at IS NULL")
                agent_where.append("p.archived_at IS NULL")
            agent_rows = connection.execute(
                f"""
                SELECT a.*, g.objective AS current_goal_objective,
                       t.title AS current_task_title,
                       j.kind AS current_job_kind,
                       j.status AS current_job_status,
                       latest_job.job_id AS latest_job_id,
                       latest_job.status AS latest_job_status,
                       latest_job.thread_action AS latest_job_thread_action,
                       latest_job.workflow_epoch AS latest_job_workflow_epoch,
                       latest_job.outcome AS latest_job_outcome,
                       latest_job.input_tokens AS latest_job_input_tokens,
                       latest_job.cached_input_tokens AS latest_job_cached_input_tokens,
                       latest_job.uncached_input_tokens AS latest_job_uncached_input_tokens,
                       latest_job.output_tokens AS latest_job_output_tokens,
                       latest_job.first_input_tokens AS latest_job_first_input_tokens,
                       latest_job.first_cached_input_tokens AS latest_job_first_cached_input_tokens,
                       latest_job.first_uncached_input_tokens AS latest_job_first_uncached_input_tokens,
                       latest_job.context_tokens AS latest_job_context_tokens,
                       latest_job.model_context_window AS latest_job_model_context_window,
                       latest_job.cache_write_input_tokens AS latest_job_cache_write_input_tokens,
                       latest_job.reasoning_output_tokens AS latest_job_reasoning_output_tokens,
                       latest_job.usage_source AS latest_job_usage_source,
                       latest_job.telemetry_complete AS latest_job_telemetry_complete,
                       latest_job.usage_missing_fields AS latest_job_usage_missing_fields,
                       latest_job.started_at AS latest_job_started_at,
                       latest_job.finished_at AS latest_job_finished_at,
                       ledger.ledger_total_input_tokens,
                       ledger.ledger_total_cached_input_tokens,
                       ledger.ledger_total_uncached_input_tokens,
                       ledger.ledger_total_output_tokens
                FROM cluster_agents a
                JOIN projects p ON p.project_id = a.project_id
                LEFT JOIN cluster_goals g ON g.goal_id = a.current_goal_id
                LEFT JOIN tasks t ON t.task_id = a.current_task_id
                LEFT JOIN cluster_jobs j ON j.job_id = a.current_job_id
                LEFT JOIN cluster_jobs latest_job
                  ON latest_job.job_id = (
                    SELECT j2.job_id
                    FROM cluster_jobs j2
                    WHERE j2.agent_id = a.agent_id
                    ORDER BY j2.started_at DESC, j2.job_id DESC
                    LIMIT 1
                  )
                LEFT JOIN (
                    SELECT agent_id,
                           SUM(input_tokens) AS ledger_total_input_tokens,
                           SUM(cached_input_tokens) AS ledger_total_cached_input_tokens,
                           SUM(uncached_input_tokens) AS ledger_total_uncached_input_tokens,
                           SUM(output_tokens) AS ledger_total_output_tokens
                    FROM cluster_jobs
                    WHERE agent_id IS NOT NULL
                    GROUP BY agent_id
                ) ledger ON ledger.agent_id = a.agent_id
                WHERE {" AND ".join(agent_where)}
                ORDER BY
                    CASE a.status
                        WHEN 'busy' THEN 0
                        WHEN 'starting' THEN 1
                        WHEN 'loaded' THEN 2
                        WHEN 'idle' THEN 3
                        ELSE 4
                    END,
                    a.last_used_at DESC,
                    a.agent_id
                LIMIT 200
                """,
                agent_parameters,
            ).fetchall()
            cluster_agents = []
            for row in agent_rows:
                agent = dict(row)
                agent["profile_contract_status"] = (
                    cache_profile_contract_status(agent.get("profile"))
                )
                if agent.get("runtime") == "native":
                    try:
                        missing_fields = json.loads(
                            str(
                                agent.get(
                                    "native_usage_missing_fields",
                                    "[]",
                                )
                                or "[]"
                            )
                        )
                    except (TypeError, ValueError):
                        missing_fields = []
                    if not isinstance(missing_fields, list):
                        missing_fields = []
                    agent["native_usage_missing_fields"] = sorted(
                        {
                            str(field)
                            for field in missing_fields
                            if str(field).strip()
                        }
                    )
                    try:
                        candidates = json.loads(
                            str(
                                agent.get(
                                    "task_candidates_json",
                                    "[]",
                                )
                                or "[]"
                            )
                        )
                    except (TypeError, ValueError):
                        candidates = []
                    if not isinstance(candidates, list):
                        candidates = []
                    agent["task_candidates"] = sorted(
                        {
                            str(value).strip().upper()
                            for value in candidates
                            if str(value).strip()
                        }
                    )
                    agent.pop("task_candidates_json", None)
                    agent["task_assignment_ambiguous"] = bool(
                        int(agent.get("task_assignment_ambiguous") or 0)
                    )
                    agent["session_stale"] = bool(
                        int(agent.get("session_stale") or 0)
                    )
                    effective_agent_status = str(
                        agent.get("status") or "unknown"
                    )
                    if (
                        agent["session_stale"]
                        and effective_agent_status
                        in {"loaded", "idle"}
                    ):
                        effective_agent_status = "stored"
                    agent["effective_status"] = effective_agent_status
                    usage_status = str(
                        agent.get("native_usage_status") or "unavailable"
                    )
                    agent["usage_stale"] = usage_status == "stale"
                    token_fields = (
                        "last_input_tokens",
                        "last_cached_input_tokens",
                        "last_uncached_input_tokens",
                        "total_input_tokens",
                        "total_cached_input_tokens",
                        "total_uncached_input_tokens",
                        "context_tokens",
                        "output_tokens",
                    )
                    if usage_status in {"unavailable", "unknown"}:
                        for field in (
                            *token_fields,
                            "turn_count",
                        ):
                            agent[field] = None
                        agent["usage_available"] = False
                    else:
                        for field in agent["native_usage_missing_fields"]:
                            if field in token_fields or field == "turn_count":
                                agent[field] = None
                        agent["usage_available"] = bool(
                            agent.get("turn_count") is not None
                            or any(
                                agent.get(field) is not None
                                for field in token_fields
                            )
                        )
                else:
                    agent["effective_status"] = agent.get(
                        "status",
                        "unknown",
                    )
                ledger_values = {
                    "input": agent.get("ledger_total_input_tokens"),
                    "cached": agent.get(
                        "ledger_total_cached_input_tokens"
                    ),
                    "uncached": agent.get(
                        "ledger_total_uncached_input_tokens"
                    ),
                    "output": agent.get("ledger_total_output_tokens"),
                }
                ledger_has_evidence = any(
                    value is not None and int(value) > 0
                    for value in ledger_values.values()
                )
                if ledger_has_evidence:
                    agent["total_input_tokens"] = ledger_values["input"]
                    agent["total_cached_input_tokens"] = (
                        ledger_values["cached"]
                    )
                    agent["total_uncached_input_tokens"] = (
                        ledger_values["uncached"]
                    )
                    agent["output_tokens"] = ledger_values["output"]
                total_rate = self._cache_hit_rate(
                    agent.get("total_input_tokens"),
                    agent.get("total_cached_input_tokens"),
                    agent.get("total_uncached_input_tokens"),
                )
                latest_usage = {
                    field: agent.get(f"latest_job_{field}")
                    for field in JOB_USAGE_FIELDS
                }
                latest_usage["usage_source"] = agent.get(
                    "latest_job_usage_source"
                )
                latest_usage["usage_missing_fields"] = agent.get(
                    "latest_job_usage_missing_fields"
                )
                self._project_unknown_usage(latest_usage)
                for field, value in latest_usage.items():
                    if field in JOB_USAGE_FIELDS:
                        agent[f"latest_job_{field}"] = value
                latest_rate = self._cache_hit_rate(
                    latest_usage.get("input_tokens"),
                    latest_usage.get("cached_input_tokens"),
                    latest_usage.get("uncached_input_tokens"),
                )
                last_rate = self._cache_hit_rate(
                    agent.get("last_input_tokens"),
                    agent.get("last_cached_input_tokens"),
                    agent.get("last_uncached_input_tokens"),
                )
                agent["cumulative_input_tokens"] = agent.get(
                    "total_input_tokens"
                )
                agent["cumulative_cached_input_tokens"] = agent.get(
                    "total_cached_input_tokens"
                )
                agent["cumulative_uncached_input_tokens"] = agent.get(
                    "total_uncached_input_tokens"
                )
                agent["cumulative_output_tokens"] = agent.get(
                    "output_tokens"
                )
                agent["cumulative_cache_hit_rate"] = total_rate
                agent["last_cache_hit_rate"] = (
                    last_rate if last_rate is not None else latest_rate
                )
                agent["cumulative_usage_source"] = (
                    "native_session"
                    if agent.get("runtime") == "native"
                    and agent.get("usage_available")
                    else "job_ledger"
                    if ledger_has_evidence
                    else "agent_total"
                    if total_rate is not None
                    else None
                )
                agent["cache_usage_source"] = (
                    "native_session"
                    if agent.get("runtime") == "native"
                    and agent.get("usage_available")
                    else "latest_job"
                    if latest_rate is not None
                    else "job_ledger"
                    if ledger_has_evidence
                    else "agent_total"
                    if total_rate is not None
                    else None
                )
                agent["cache_hit_rate"] = (
                    total_rate
                    if total_rate is not None
                    else latest_rate
                )
                cluster_agents.append(agent)
            cluster_agent_events: list[dict[str, Any]] = []
            agent_ids = [agent["agent_id"] for agent in cluster_agents]
            if agent_ids:
                placeholders = ",".join("?" for _ in agent_ids)
                agent_event_rows = connection.execute(
                    f"""
                    SELECT *
                    FROM cluster_agent_events
                    WHERE agent_id IN ({placeholders})
                    ORDER BY event_id DESC
                    LIMIT 200
                    """,
                    agent_ids,
                ).fetchall()
                for row in reversed(agent_event_rows):
                    item = dict(row)
                    try:
                        item["data"] = json.loads(item.pop("data_json"))
                    except (TypeError, ValueError):
                        item["data"] = {}
                        item.pop("data_json", None)
                    cluster_agent_events.append(item)
            schedulable_goal_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) AS goal_count
                    FROM cluster_goals g
                    JOIN projects p ON p.project_id = g.project_id
                    WHERE g.status IN ('planning', 'active')
                      AND g.archived_at IS NULL
                      AND p.archived_at IS NULL
                    """
                ).fetchone()["goal_count"]
            )
            global_capacity = schedulable_goal_count * goal_capacity
            task_archive_filter = (
                "" if include_archived else "AND t.archived_at IS NULL"
            )
            if project_id:
                task_rows = connection.execute(
                    f"""
                    SELECT t.*, p.name AS project_name, p.root_path
                    FROM tasks t
                    JOIN projects p ON p.project_id = t.project_id
                    LEFT JOIN cluster_goals tg ON tg.goal_id = t.goal_id
                    WHERE t.project_id = ?
                    {task_archive_filter}
                    {task_goal_filter}
                    ORDER BY
                        CASE t.status
                            WHEN 'running' THEN 0
                            WHEN 'review' THEN 1
                            WHEN 'ready' THEN 2
                            WHEN 'blocked' THEN 3
                            WHEN 'backlog' THEN 4
                            WHEN 'done' THEN 5
                            ELSE 6
                        END,
                        t.priority,
                        t.created_at
                    """,
                    (project_id,),
                ).fetchall()
                message_task_filter = (
                    ""
                    if include_archived
                    else "AND (m.task_id IS NULL OR t.archived_at IS NULL)"
                )
                message_rows = connection.execute(
                    f"""
                    SELECT m.*
                    FROM messages m
                    LEFT JOIN tasks t ON t.task_id = m.task_id
                    WHERE m.project_id = ?
                    {message_task_filter}
                    ORDER BY m.message_id DESC LIMIT 40
                    """,
                    (project_id,),
                ).fetchall()
            else:
                archive_filter = (
                    "" if include_archived else "AND p.archived_at IS NULL"
                )
                task_rows = connection.execute(
                    f"""
                    SELECT t.*, p.name AS project_name, p.root_path
                    FROM tasks t
                    JOIN projects p ON p.project_id = t.project_id
                    LEFT JOIN cluster_goals tg ON tg.goal_id = t.goal_id
                    WHERE t.status != 'canceled'
                    {archive_filter}
                    {task_archive_filter}
                    {task_goal_filter}
                    ORDER BY
                        CASE t.status
                            WHEN 'running' THEN 0
                            WHEN 'review' THEN 1
                            WHEN 'ready' THEN 2
                            WHEN 'blocked' THEN 3
                            WHEN 'backlog' THEN 4
                            ELSE 5
                        END,
                        t.priority,
                        t.created_at
                    """
                ).fetchall()
                message_task_filter = (
                    ""
                    if include_archived
                    else "AND (m.task_id IS NULL OR t.archived_at IS NULL)"
                )
                message_rows = connection.execute(
                    f"""
                    SELECT m.*
                    FROM messages m
                    JOIN projects p ON p.project_id = m.project_id
                    LEFT JOIN tasks t ON t.task_id = m.task_id
                    WHERE 1 = 1
                    {archive_filter}
                    {message_task_filter}
                    ORDER BY m.message_id DESC LIMIT 40
                    """
                ).fetchall()

            tasks = [dict(row) for row in task_rows]
            task_ids = [task["task_id"] for task in tasks]
            blocked_by: dict[str, list[dict[str, Any]]] = {task_id: [] for task_id in task_ids}
            archived_project_ids = {
                str(project["project_id"])
                for project in projects
                if project.get("archived_at")
            }
            archived_goal_ids = {
                str(goal["goal_id"])
                for goal in cluster_goals
                if goal.get("archived_at")
            }
            if task_ids:
                placeholders = ",".join("?" for _ in task_ids)
                dependency_rows = connection.execute(
                    f"""
                    SELECT d.task_id, d.depends_on, t.status, t.title
                    FROM task_dependencies d
                    JOIN tasks t ON t.task_id = d.depends_on
                    WHERE d.task_id IN ({placeholders}) AND t.status != 'done'
                    ORDER BY d.depends_on
                    """,
                    task_ids,
                ).fetchall()
                for row in dependency_rows:
                    blocked_by[row["task_id"]].append(
                        {
                            "task_id": row["depends_on"],
                            "status": row["status"],
                            "title": row["title"],
                        }
                    )
            for task in tasks:
                task.pop("claim_token", None)
                task["blocked_by"] = blocked_by.get(task["task_id"], [])
                task["lease_expired"] = self._lease_expired(task.get("lease_until"))
            active_job_rows = connection.execute(
                """
                SELECT j.*, g.objective, g.project_id
                FROM cluster_jobs j
                JOIN cluster_goals g ON g.goal_id = j.goal_id
                JOIN projects p ON p.project_id = g.project_id
                WHERE j.status IN ('starting', 'running')
                  AND g.archived_at IS NULL
                  AND p.archived_at IS NULL
                ORDER BY j.started_at, j.job_id
                """
            ).fetchall()
            active_jobs = [dict(row) for row in active_job_rows]
            for job in active_jobs:
                self._project_unknown_usage(job)
                job["cache_hit_rate"] = self._cache_hit_rate(
                    job.get("input_tokens"),
                    job.get("cached_input_tokens"),
                    job.get("uncached_input_tokens"),
                )
            active_jobs_by_task = {
                job["task_id"]: {
                    "job_id": job["job_id"],
                    "actor": job.get("actor"),
                    "kind": job["kind"],
                    "role": job["role"],
                    "status": job["status"],
                    "pid": job.get("pid"),
                    "attempt": job.get("attempt"),
                    "started_at": job.get("started_at"),
                    "thread_id": job.get("thread_id"),
                    "turn_id": job.get("turn_id"),
                    "runtime": job.get("runtime"),
                    "thread_action": job.get("thread_action"),
                    "outcome": job.get("outcome"),
                    "workflow_epoch": job.get("workflow_epoch"),
                    "input_tokens": job.get("input_tokens", 0),
                    "cached_input_tokens": job.get("cached_input_tokens", 0),
                    "uncached_input_tokens": job.get("uncached_input_tokens", 0),
                    "output_tokens": job.get("output_tokens", 0),
                    "context_tokens": job.get("context_tokens", 0),
                    "model_context_window": job.get("model_context_window"),
                    "cache_hit_rate": job["cache_hit_rate"],
                }
                for job in active_jobs
                if job.get("task_id")
            }
            claimed_running_tasks = [
                task
                for task in tasks
                if task["status"] == "running" and task.get("claimed_by")
            ]
            claimed_actors = sorted(
                {
                    str(task["claimed_by"])
                    for task in claimed_running_tasks
                }
            )
            native_claim_bindings: dict[str, dict[str, Any]] = {}
            if claimed_actors:
                placeholders = ",".join("?" for _ in claimed_actors)
                native_rows = connection.execute(
                    f"""
                    SELECT agent_id, thread_id, actor, role, display_name
                    FROM cluster_agents
                    WHERE runtime = 'native' AND archived_at IS NULL
                      AND actor IN ({placeholders})
                    ORDER BY updated_at DESC, agent_id
                    """,
                    claimed_actors,
                ).fetchall()
                for row in native_rows:
                    native_claim_bindings.setdefault(
                        str(row["actor"]),
                        dict(row),
                    )
            active_jobs_by_goal: dict[str, list[dict[str, Any]]] = {}
            active_worker_jobs_by_goal: dict[str, list[dict[str, Any]]] = {}
            active_task_ids: list[str] = []
            for job in active_jobs:
                goal_id = job.get("goal_id")
                if goal_id:
                    active_jobs_by_goal.setdefault(goal_id, []).append(job)
                    if job.get("kind") == "worker":
                        active_worker_jobs_by_goal.setdefault(goal_id, []).append(job)
                task_id = job.get("task_id")
                if task_id:
                    active_task_ids.append(task_id)
            active_task_ids.extend(
                str(task["task_id"]) for task in claimed_running_tasks
            )
            active_task_ids = list(dict.fromkeys(active_task_ids))
            latest_task_activity: dict[str, str] = {}
            if active_task_ids:
                placeholders = ",".join("?" for _ in active_task_ids)
                activity_rows = connection.execute(
                    f"""
                    SELECT e.task_id, e.created_at
                    FROM task_events e
                    WHERE e.task_id IN ({placeholders})
                      AND e.event_type IN ('claimed', 'heartbeat', 'progress')
                    ORDER BY e.task_id, e.created_at DESC, e.event_id DESC
                    """,
                    active_task_ids,
                ).fetchall()
                for row in activity_rows:
                    latest_task_activity.setdefault(
                        str(row["task_id"]),
                        str(row["created_at"]),
                    )
            now_utc = datetime.now(UTC)
            worker_heartbeat_ages = {
                str(job["job_id"]): worker_heartbeat_age(
                    job, latest_task_activity, now_utc
                )
                for job in active_jobs
                if job.get("kind") == "worker"
            }
            for task in tasks:
                task["active_job"] = active_jobs_by_task.get(task["task_id"])
                claim_activity_at = latest_task_activity.get(task["task_id"])
                active_job = task["active_job"]
                execution: dict[str, Any] | None = None
                if active_job is not None:
                    execution = {
                        "source": "managed_job",
                        "label": "Managed Job",
                        "actor": active_job.get("actor"),
                        "job_id": active_job.get("job_id"),
                        "role": active_job.get("role"),
                        "status": active_job.get("status"),
                        "started_at": active_job.get("started_at"),
                    }
                elif task["status"] == "running" and task.get("claimed_by"):
                    claimed_by = str(task["claimed_by"])
                    native_binding = native_claim_bindings.get(claimed_by)
                    if claimed_by.startswith("auto-"):
                        source = "dispatch_reservation"
                        label = "等待 Worker 启动"
                    elif native_binding is not None:
                        source = "native_claim"
                        label = "Native Agent"
                    else:
                        source = "external_claim"
                        label = "外部 Agent"
                    execution = {
                        "source": source,
                        "label": label,
                        "actor": claimed_by,
                        "agent_id": (
                            native_binding.get("agent_id")
                            if native_binding is not None
                            else None
                        ),
                        "thread_id": (
                            native_binding.get("thread_id")
                            if native_binding is not None
                            else None
                        ),
                        "display_name": (
                            native_binding.get("display_name")
                            if native_binding is not None
                            else claimed_by
                        ),
                        "started_at": claim_activity_at or task.get("updated_at"),
                        "lease_until": task.get("lease_until"),
                        "lease_expired": bool(task.get("lease_expired")),
                    }
                task["execution"] = execution
                task["active_execution"] = execution is not None
                wait_seconds = 0
                if task["status"] == "ready" and not task["blocked_by"]:
                    wait_seconds = age_seconds(task.get("created_at"), now_utc)
                task["claim_wait_seconds"] = wait_seconds
                task["claim_wait_state"] = claim_wait_state(wait_seconds)
            tasks_by_goal: dict[str, list[dict[str, Any]]] = {}
            for task in tasks:
                if is_historical_task(
                    task, archived_project_ids, archived_goal_ids
                ):
                    continue
                goal_id = task.get("goal_id")
                if goal_id:
                    tasks_by_goal.setdefault(str(goal_id), []).append(task)
            goal_states: dict[str, dict[str, Any]] = {}
            attention_count = 0
            for task in tasks:
                if is_historical_task(
                    task, archived_project_ids, archived_goal_ids
                ):
                    continue
                goal_id = task.get("goal_id")
                if not goal_id:
                    if (
                        task["status"] in {"blocked", "review"}
                        or task["claim_wait_state"] == "critical"
                    ):
                        attention_count += 1
                    continue
                state = goal_states.setdefault(
                    goal_id,
                    {
                        "attention_task_count": 0,
                        "has_blocked": False,
                        "has_review": False,
                        "has_critical_wait": False,
                        "has_warning_wait": False,
                        "has_ready": False,
                        "has_running": False,
                    },
                )
                task_status = task["status"]
                task_attention = (
                    task_status in {"blocked", "review"}
                    or task["claim_wait_state"] == "critical"
                )
                if task_attention:
                    state["attention_task_count"] += 1
                    attention_count += 1
                state["has_blocked"] = state["has_blocked"] or task_status == "blocked"
                state["has_review"] = state["has_review"] or task_status == "review"
                state["has_critical_wait"] = (
                    state["has_critical_wait"]
                    or task["claim_wait_state"] == "critical"
                )
                state["has_warning_wait"] = (
                    state["has_warning_wait"]
                    or task["claim_wait_state"] == "warning"
                )
                state["has_ready"] = state["has_ready"] or task_status == "ready"
                state["has_running"] = state["has_running"] or task_status == "running"
            active_goal_ids = {
                job["goal_id"]
                for job in active_jobs
                if job.get("goal_id")
            }
            for goal in cluster_goals:
                state = goal_states.get(goal["goal_id"], {})
                goal_status = goal["status"]
                is_historical_goal = bool(goal.get("archived_at")) or (
                    str(goal["project_id"]) in archived_project_ids
                )
                has_active_job = goal["goal_id"] in active_goal_ids
                has_blocked = bool(state.get("has_blocked"))
                has_review = bool(state.get("has_review"))
                has_critical_wait = bool(state.get("has_critical_wait"))
                has_warning_wait = bool(state.get("has_warning_wait"))
                has_ready = bool(state.get("has_ready"))
                has_running = bool(state.get("has_running"))
                goal["attention_task_count"] = int(
                    state.get("attention_task_count", 0)
                )
                is_stalled_goal = (
                    goal_status == "paused"
                    and bool(goal.get("stalled_at"))
                )
                if is_historical_goal:
                    goal["health"] = "archived"
                    goal["next_action"] = "none"
                    goal["attention_task_count"] = 0
                    goal["active_job_count"] = 0
                    goal["active_execution_count"] = 0
                    goal["external_execution_count"] = 0
                    goal["heartbeat_age_seconds"] = 0
                    goal["heartbeat_state"] = "idle"
                    goal["queue_reason"] = "none"
                    goal["estimated_wait_seconds"] = 0
                    continue
                goal["health"] = goal_health(
                    goal_status,
                    is_historical=False,
                    is_stalled=is_stalled_goal,
                    has_active_job=has_active_job,
                    has_blocked=has_blocked,
                    has_review=has_review,
                    has_critical_wait=has_critical_wait,
                    has_warning_wait=has_warning_wait,
                    has_running=has_running,
                )
                goal["next_action"] = goal_next_action(
                    goal_status,
                    is_stalled=is_stalled_goal,
                    has_blocked=has_blocked,
                    has_critical_wait=has_critical_wait,
                    has_review=has_review,
                    has_active_job=has_active_job,
                    has_running=has_running,
                    has_ready=has_ready,
                )
                goal_jobs = active_jobs_by_goal.get(goal["goal_id"], [])
                goal["active_job_count"] = len(goal_jobs)
                goal_tasks = tasks_by_goal.get(goal["goal_id"], [])
                external_executions = [
                    task
                    for task in goal_tasks
                    if task.get("active_execution")
                    and task.get("execution", {}).get("source")
                    != "managed_job"
                ]
                goal["active_execution_count"] = (
                    len(goal_jobs) + len(external_executions)
                )
                goal["external_execution_count"] = len(external_executions)
                goal_heartbeat_age = max(
                    (
                        worker_heartbeat_ages[str(job["job_id"])]
                        for job in goal_jobs
                        if str(job["job_id"]) in worker_heartbeat_ages
                    ),
                    default=0,
                )
                goal["heartbeat_age_seconds"] = goal_heartbeat_age
                goal["heartbeat_state"] = (
                    heartbeat_state(
                        goal_heartbeat_age,
                        heartbeat_warning_seconds,
                        heartbeat_critical_seconds,
                    )
                    if goal_jobs
                    else "idle"
                )
                review_tasks = [
                    task for task in goal_tasks if task["status"] == "review"
                ]
                blocked_tasks = [
                    task for task in goal_tasks if task["status"] == "blocked"
                ]
                ready_tasks = [
                    task for task in goal_tasks if task["status"] == "ready"
                ]
                dispatchable_ready = [
                    task for task in ready_tasks if not task["blocked_by"]
                ]
                active_worker_jobs = active_worker_jobs_by_goal.get(
                    goal["goal_id"],
                    [],
                )
                queue_reason = goal_queue_reason(
                    goal_status,
                    has_dispatchable_ready=bool(dispatchable_ready),
                    has_ready=bool(ready_tasks),
                    has_review=bool(review_tasks),
                    has_blocked=bool(blocked_tasks),
                    has_running=any(
                        task["status"] == "running" for task in goal_tasks
                    ),
                    goal_active_jobs=len(goal_jobs),
                    goal_active_workers=len(active_worker_jobs),
                    total_active_jobs=len(active_jobs),
                    system_capacity=system_capacity,
                    goal_capacity=goal_capacity,
                )
                goal["queue_reason"] = queue_reason
                if queue_reason in {"global_capacity", "goal_capacity"}:
                    wait_tasks = dispatchable_ready
                    wait_field = "created_at"
                elif queue_reason == "review_pending":
                    wait_tasks = review_tasks
                    wait_field = "updated_at"
                elif queue_reason == "blocked":
                    wait_tasks = blocked_tasks
                    wait_field = "updated_at"
                elif queue_reason == "ready":
                    wait_tasks = ready_tasks
                    wait_field = "created_at"
                elif queue_reason == "dependency_wait":
                    wait_tasks = ready_tasks
                    wait_field = "updated_at"
                elif queue_reason == "waiting_for_lead":
                    wait_tasks = []
                    wait_field = "updated_at"
                else:
                    wait_tasks = []
                    wait_field = "updated_at"
                goal["estimated_wait_seconds"] = max(
                    (
                        age_seconds(task.get(wait_field), now_utc)
                        for task in wait_tasks
                    ),
                    default=(
                        age_seconds(goal.get("updated_at"), now_utc)
                        if queue_reason == "waiting_for_lead"
                        else 0
                    ),
                )
                if goal_status in {"paused", "failed"}:
                    attention_count += 1
            messages = [dict(row) for row in reversed(message_rows)]
            if project_id:
                event_rows = connection.execute(
                    f"""
                    SELECT e.event_id, e.task_id, e.event_type, e.actor,
                           e.thread_id, e.job_id, e.correlation_id,
                           e.assignment_id, e.data_json, e.created_at
                    FROM task_events e
                    JOIN tasks t ON t.task_id = e.task_id
                    WHERE t.project_id = ?
                    {task_archive_filter}
                    ORDER BY e.event_id DESC
                    LIMIT 500
                    """,
                    (project_id,),
                ).fetchall()
            else:
                archive_filter = (
                    "" if include_archived else "AND p.archived_at IS NULL"
                )
                event_rows = connection.execute(
                    f"""
                    SELECT e.event_id, e.task_id, e.event_type, e.actor,
                           e.thread_id, e.job_id, e.correlation_id,
                           e.assignment_id, e.data_json, e.created_at
                    FROM task_events e
                    JOIN tasks t ON t.task_id = e.task_id
                    JOIN projects p ON p.project_id = t.project_id
                    WHERE t.status != 'canceled'
                    {archive_filter}
                    {task_archive_filter}
                    ORDER BY e.event_id DESC
                    LIMIT 500
                    """
                ).fetchall()
            events = []
            for row in reversed(event_rows):
                item = dict(row)
                try:
                    item["data"] = json.loads(item.pop("data_json"))
                except (TypeError, ValueError):
                    item["data"] = {}
                    item.pop("data_json", None)
                events.append(item)
            counts: dict[str, int] = {}
            for task in tasks:
                counts[task["status"]] = counts.get(task["status"], 0) + 1
            cluster_summary = {
                "goals_total": len(cluster_goals),
                "goals_active": sum(
                    goal["status"] == "active" for goal in cluster_goals
                ),
                "goals_paused": sum(
                    goal["status"] == "paused" for goal in cluster_goals
                ),
                "goals_failed": sum(
                    goal["status"] == "failed" for goal in cluster_goals
                ),
                "ready_tasks": counts.get("ready", 0),
                "running_tasks": counts.get("running", 0),
                "review_tasks": counts.get("review", 0),
                "blocked_tasks": counts.get("blocked", 0),
                "active_jobs": len(active_jobs),
                "active_executions": len(active_jobs)
                + sum(
                    task.get("active_execution")
                    and (task.get("execution") or {}).get("source")
                    != "managed_job"
                    for task in tasks
                    if not is_historical_task(
                        task, archived_project_ids, archived_goal_ids
                    )
                ),
                "external_executions": sum(
                    task.get("active_execution")
                    and (task.get("execution") or {}).get("source")
                    in {"external_claim", "native_claim", "dispatch_reservation"}
                    for task in tasks
                    if not is_historical_task(
                        task, archived_project_ids, archived_goal_ids
                    )
                ),
                "native_executions": sum(
                    (task.get("execution") or {}).get("source")
                    == "native_claim"
                    for task in tasks
                    if not is_historical_task(
                        task, archived_project_ids, archived_goal_ids
                    )
                ),
                "active_workers": len(worker_heartbeat_ages),
                "active_leads": sum(
                    job.get("kind") == "lead" for job in active_jobs
                ),
                "global_jobs": len(worker_heartbeat_ages),
                "global_capacity": global_capacity,
                "worker_capacity_per_goal": goal_capacity,
                "system_jobs": len(active_jobs),
                "system_capacity": system_capacity,
                "queued_goals": sum(
                    goal["status"] in {"planning", "active"}
                    and goal["queue_reason"]
                    not in {"running", "none"}
                    for goal in cluster_goals
                ),
                "heartbeat_overdue": sum(
                    age >= heartbeat_warning_seconds
                    for age in worker_heartbeat_ages.values()
                ),
                "oldest_heartbeat_age_seconds": max(
                    worker_heartbeat_ages.values(),
                    default=0,
                ),
                "fairness_wait_count": sum(
                    goal["status"] in {"planning", "active"}
                    and goal["queue_reason"] == "global_capacity"
                    for goal in cluster_goals
                ),
                "attention_count": attention_count,
                "oldest_claim_wait_seconds": max(
                    (
                        task["claim_wait_seconds"]
                        for task in tasks
                        if task["status"] == "ready"
                        and not is_historical_task(
                            task, archived_project_ids, archived_goal_ids
                        )
                    ),
                    default=0,
                ),
            }
            revision = self._dashboard_revision(
                connection,
                project_id,
                include_archived,
            )
            generated_at = utc_now()
            return {
                "projects": projects,
                "selected_project_id": project_id,
                "counts": counts,
                "tasks": tasks,
                "messages": messages,
                "events": events,
                "cluster_goals": cluster_goals,
                "cluster_jobs": cluster_jobs,
                "cluster_agents": cluster_agents,
                "cluster_agent_events": cluster_agent_events,
                "cluster_summary": cluster_summary,
                "include_archived": include_archived,
                "revision": revision,
                "generated_at": generated_at,
            }

    def claim_task(
        self,
        task_id: str,
        *,
        actor: str,
        lease_minutes: int = 30,
    ) -> dict[str, Any]:
        if not actor.strip():
            raise TeamError("actor is required")
        lease_minutes = max(5, min(int(lease_minutes), 240))
        claim_token = secrets.token_urlsafe(24)
        lease_until = (
            datetime.now(UTC) + timedelta(minutes=lease_minutes)
        ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = connection.execute(
                """
                SELECT t.*, p.root_path, p.archived_at AS project_archived_at
                FROM tasks t JOIN projects p ON p.project_id = t.project_id
                WHERE t.task_id = ?
                """,
                (task_id,),
            ).fetchone()
            if task is None:
                raise TeamError(f"task not found: {task_id}")
            if task["archived_at"]:
                raise TeamError("task is archived; restore it before claiming")
            if task["project_archived_at"]:
                raise TeamError("project is archived; restore it before claiming tasks")
            native = connection.execute(
                """
                SELECT agent_id, prompt_status, current_task_id, status
                FROM cluster_agents
                WHERE project_id = ? AND actor = ? AND runtime = 'native'
                ORDER BY updated_at DESC, agent_id
                LIMIT 1
                """,
                (task["project_id"], actor),
            ).fetchone()
            if native is not None:
                if (
                    native["current_task_id"]
                    and native["current_task_id"] != task_id
                ):
                    raise TeamError(
                        "native thread is same_task_only and cannot claim another task"
                    )
                if native["prompt_status"] != "delivered":
                    raise TeamError(
                        "native thread prompt has not been verified as delivered; "
                        "resend the task and refresh native registration"
                    )
            if task["status"] in TERMINAL_STATUSES:
                raise TeamError(f"cannot claim terminal task: {task['status']}")
            if task["status"] == "review":
                raise TeamError("task is in review")
            if task["status"] == "blocked":
                raise TeamError("task is blocked; unblock it before claiming")
            if (
                task["status"] == "running"
                and not self._lease_expired(task["lease_until"])
                and task["claimed_by"] != actor
            ):
                raise TeamError(f"task is already running under {task['claimed_by']}")
            blockers = self._blocking_tasks(connection, task_id)
            if blockers:
                raise TeamError("task is blocked by: " + ", ".join(
                    item["task_id"] for item in blockers
                ))
            if (
                task["claimed_by"]
                and task["claim_token"]
                and not self._lease_expired(task["lease_until"])
                and task["claimed_by"] != actor
            ):
                raise TeamError(f"task is leased by {task['claimed_by']}")
            if task["kind"] == "write":
                self._validate_write_worktree_assignment(connection, task)
            now = utc_now()
            expected_status = str(task["status"])
            expected_version = int(task["version"])
            updated = connection.execute(
                """
                UPDATE tasks
                SET status = 'running', claimed_by = ?, claim_token = ?,
                    lease_until = ?, version = version + 1, updated_at = ?
                WHERE task_id = ? AND status = ? AND version = ?
                  AND claimed_by IS ? AND claim_token IS ?
                """,
                (
                    actor,
                    claim_token,
                    lease_until,
                    now,
                    task_id,
                    expected_status,
                    expected_version,
                    task["claimed_by"],
                    task["claim_token"],
                ),
            )
            if updated.rowcount != 1:
                raise TeamError("task claim conflicted with another update; reload and retry")
            if native is not None:
                connection.execute(
                    """
                    UPDATE cluster_agents
                    SET current_task_id = ?, current_goal_id = ?,
                        status = CASE
                            WHEN status IN ('loaded', 'stored', 'idle')
                            THEN 'loaded'
                            ELSE status
                        END,
                        assignment_source = 'claim',
                        assignment_confidence = 'high',
                        lifecycle = 'active',
                        lifecycle_source = 'claim',
                        last_seen_at = ?, session_stale = 0, updated_at = ?
                    WHERE agent_id = ? AND archived_at IS NULL
                    """,
                    (
                        task_id,
                        task["goal_id"],
                        now,
                        now,
                        native["agent_id"],
                    ),
                )
                self.agent_pool._event(
                    connection,
                    str(native["agent_id"]),
                    "native_task_bound",
                    actor,
                    {
                        "task_id": task_id,
                        "goal_id": task["goal_id"],
                        "source": "claim",
                    },
                    created_at=now,
                )
            self._event(
                connection,
                task_id,
                "claimed",
                actor,
                {"lease_until": lease_until},
            )
        result = self.get_task(task_id)
        result["claim_token"] = claim_token
        result["lease_until"] = lease_until
        return result

    def heartbeat(
        self,
        task_id: str,
        *,
        claim_token: str,
        actor: str,
        lease_minutes: int = 30,
    ) -> dict[str, Any]:
        lease_until = (
            datetime.now(UTC) + timedelta(minutes=max(5, min(lease_minutes, 240)))
        ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = self._authorized_task(connection, task_id, claim_token, actor)
            if task["status"] != "running":
                raise TeamError("heartbeat requires a running task")
            expected_version = int(task["version"])
            updated = connection.execute(
                """
                UPDATE tasks
                SET lease_until = ?, updated_at = ?, version = version + 1
                WHERE task_id = ? AND status = 'running' AND version = ?
                  AND claimed_by = ? AND claim_token = ?
                """,
                (
                    lease_until,
                    utc_now(),
                    task_id,
                    expected_version,
                    actor,
                    claim_token,
                ),
            )
            if updated.rowcount != 1:
                raise TeamError("heartbeat conflicted with another update; reload and retry")
            heartbeat_at = utc_now()
            connection.execute(
                """
                UPDATE cluster_agents
                SET last_seen_at = ?, session_stale = 0, updated_at = ?
                WHERE project_id = ? AND actor = ? AND runtime = 'native'
                  AND current_task_id = ? AND archived_at IS NULL
                """,
                (
                    heartbeat_at,
                    heartbeat_at,
                    task["project_id"],
                    actor,
                    task_id,
                ),
            )
            self._event(
                connection,
                task_id,
                "heartbeat",
                actor,
                {"lease_until": lease_until},
            )
        result = self.get_task(task_id)
        result["claim_token"] = claim_token
        return result

    def update_task(
        self,
        task_id: str,
        *,
        claim_token: str,
        actor: str,
        note: str,
        result: str | None = None,
        tests: str | None = None,
        commit_sha: str | None = None,
    ) -> dict[str, Any]:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = self._authorized_task(connection, task_id, claim_token, actor)
            if task["status"] != "running":
                raise TeamError("task is not running")
            fields = ["updated_at = ?", "version = version + 1"]
            parameters: list[Any] = [utc_now()]
            if result is not None:
                fields.append("result = ?")
                parameters.append(result)
            if tests is not None:
                fields.append("tests = ?")
                parameters.append(tests)
            if commit_sha is not None:
                if not isinstance(commit_sha, str):
                    raise TeamError("commit_sha must be a string")
                fields.append("commit_sha = ?")
                parameters.append(commit_sha.strip())
            expected_version = int(task["version"])
            parameters.append(task_id)
            parameters.extend(
                (
                    expected_version,
                    actor,
                    claim_token,
                )
            )
            updated = connection.execute(
                f"""
                UPDATE tasks SET {', '.join(fields)}
                WHERE task_id = ? AND status = 'running' AND version = ?
                  AND claimed_by = ? AND claim_token = ?
                """,
                parameters,
            )
            if updated.rowcount != 1:
                raise TeamError("task progress conflicted with another update; reload and retry")
            self._event(
                connection,
                task_id,
                "progress",
                actor,
                {
                    "note": note,
                    "result": result,
                    "tests": tests,
                    "commit_sha": commit_sha,
                },
            )
        return self.get_task(task_id)

    def block_task(
        self,
        task_id: str,
        *,
        actor: str,
        reason: str,
        claim_token: str | None = None,
        authority_token: str | None = None,
    ) -> dict[str, Any]:
        actor = actor.strip()
        reason = reason.strip()
        if not actor:
            raise TeamError("actor is required")
        if not reason:
            raise TeamError("block reason is required")
        normalized_token = claim_token.strip() if claim_token is not None else ""
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if task is None:
                raise TeamError(f"task not found: {task_id}")
            if normalized_token:
                task = self._authorized_task(
                    connection,
                    task_id,
                    normalized_token,
                    actor,
                )
            elif task["status"] == "running":
                actor = self.authorize_privileged_actor(
                    actor,
                    operation="task block",
                    allowed_actors=frozenset(
                        {"lead", "main", "human", "teamd"}
                    ),
                    authority_token=authority_token,
                )
            if task["status"] in TERMINAL_STATUSES:
                raise TeamError("cannot block terminal task")
            expected_status = str(task["status"])
            expected_version = int(task["version"])
            updated = connection.execute(
                """
                UPDATE tasks
                SET status = 'blocked', blocker = ?, claimed_by = NULL,
                    claim_token = NULL, lease_until = NULL,
                    updated_at = ?, version = version + 1
                WHERE task_id = ? AND status = ? AND version = ?
                  AND claimed_by IS ? AND claim_token IS ?
                """,
                (
                    reason,
                    utc_now(),
                    task_id,
                    expected_status,
                    expected_version,
                    task["claimed_by"],
                    task["claim_token"],
                ),
            )
            if updated.rowcount != 1:
                raise TeamError("task block conflicted with another update; reload and retry")
            self._event(connection, task_id, "blocked", actor, {"reason": reason})
        return self.get_task(task_id)

    def unblock_task(
        self,
        task_id: str,
        *,
        actor: str,
        authority_token: str | None = None,
    ) -> dict[str, Any]:
        actor = actor.strip()
        if not actor:
            raise TeamError("actor is required")
        actor = self.authorize_privileged_actor(
            actor,
            operation="task unblock",
            authority_token=authority_token,
        )
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if task is None:
                raise TeamError(f"task not found: {task_id}")
            if task["status"] != "blocked":
                raise TeamError("task is not blocked")
            expected_version = int(task["version"])
            updated = connection.execute(
                """
                UPDATE tasks
                SET status = 'ready', blocker = '', claimed_by = NULL,
                    claim_token = NULL, lease_until = NULL,
                    updated_at = ?, version = version + 1
                WHERE task_id = ? AND status = 'blocked' AND version = ?
                """,
                (utc_now(), task_id, expected_version),
            )
            if updated.rowcount != 1:
                raise TeamError("task unblock conflicted with another update; reload and retry")
            self._event(connection, task_id, "unblocked", actor, {})
        return self.get_task(task_id)

    def cancel_task(
        self,
        task_id: str,
        *,
        actor: str,
        reason: str,
        authority_token: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(actor, str):
            raise TeamError("actor must be a string")
        actor = self.authorize_privileged_actor(
            actor,
            operation="task cancellation",
            authority_token=authority_token,
        )
        reason = reason.strip()
        if not reason:
            raise TeamError("cancel reason is required")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if task is None:
                raise TeamError(f"task not found: {task_id}")
            if task["status"] in TERMINAL_STATUSES:
                raise TeamError(f"task is already terminal: {task['status']}")
            if task["status"] == "running":
                raise TeamError("running tasks must be submitted or blocked before cancellation")
            active_job = connection.execute(
                """
                SELECT job_id FROM cluster_jobs
                WHERE task_id = ? AND status IN ('starting', 'running')
                LIMIT 1
                """,
                (task_id,),
            ).fetchone()
            if active_job:
                raise TeamError(
                    f"cannot cancel task with active job: {active_job['job_id']}"
                )
            now = utc_now()
            expected_status = str(task["status"])
            expected_version = int(task["version"])
            updated = connection.execute(
                """
                UPDATE tasks
                SET status = 'canceled', blocker = ?, claimed_by = NULL,
                    claim_token = NULL, lease_until = NULL, completed_at = ?,
                    updated_at = ?, version = version + 1
                WHERE task_id = ? AND status = ? AND version = ?
                """,
                (
                    f"canceled: {reason}",
                    now,
                    now,
                    task_id,
                    expected_status,
                    expected_version,
                ),
            )
            if updated.rowcount != 1:
                raise TeamError("task cancel conflicted with another update; reload and retry")
            self._event(
                connection,
                task_id,
                "canceled",
                actor,
                {"reason": reason},
            )
        return self.get_task(task_id)

    def submit_task(
        self,
        task_id: str,
        *,
        claim_token: str,
        actor: str,
        result: str,
        tests: str,
        commit_sha: str = "",
    ) -> dict[str, Any]:
        resolved_commit = commit_sha.strip()
        with self.connection(write=False) as connection:
            preflight = self._authorized_task(
                connection,
                task_id,
                claim_token,
                actor,
            )
            if preflight["status"] != "running":
                raise TeamError("only running tasks can be submitted")
            if preflight["kind"] == "write":
                if not resolved_commit:
                    raise TeamError("write task submission requires commit_sha")
                worktree = str(
                    self._validate_write_worktree_assignment(
                        connection,
                        preflight,
                    )
                )
        if preflight["kind"] == "write":
            status = self._worktree_git(worktree, "status", "--porcelain")
            if status.returncode != 0:
                raise TeamError(status.stderr.strip() or "could not inspect worktree")
            if status.stdout.strip():
                raise TeamError("worktree is dirty; commit changes before submission")
            head = self._worktree_git(worktree, "rev-parse", "HEAD")
            if head.returncode != 0:
                raise TeamError(head.stderr.strip() or "could not read worktree HEAD")
            actual = head.stdout.strip().lower()
            if len(resolved_commit) < 7 or not actual.startswith(resolved_commit.lower()):
                raise TeamError("commit_sha does not match the worktree HEAD")
            resolved_commit = actual
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = self._authorized_task(connection, task_id, claim_token, actor)
            if task["status"] != "running":
                raise TeamError("only running tasks can be submitted")
            expected_version = int(task["version"])
            updated = connection.execute(
                """
                UPDATE tasks
                SET status = 'review', result = ?, tests = ?, commit_sha = ?,
                    claim_token = NULL,
                    lease_until = NULL, updated_at = ?, version = version + 1
                WHERE task_id = ? AND status = 'running' AND version = ?
                  AND claimed_by = ? AND claim_token = ?
                """,
                (
                    result,
                    tests,
                    resolved_commit,
                    utc_now(),
                    task_id,
                    expected_version,
                    actor,
                    claim_token,
                ),
            )
            if updated.rowcount != 1:
                raise TeamError("task submit conflicted with another update; reload and retry")
            self._event(
                connection,
                task_id,
                "submitted",
                actor,
                {"result": result, "tests": tests, "commit_sha": resolved_commit},
            )
        return self.get_task(task_id)

    def review_task(
        self,
        task_id: str,
        *,
        actor: str,
        approve: bool,
        notes: str,
        merge_commit: str = "",
        authority_token: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(actor, str):
            raise TeamError("actor must be a string")
        if not isinstance(approve, bool):
            raise TeamError("approve must be a boolean")
        if not isinstance(notes, str):
            raise TeamError("notes must be a string")
        if not isinstance(merge_commit, str):
            raise TeamError("merge_commit must be a string")
        actor = self.authorize_privileged_actor(
            actor,
            operation="task review",
            authority_token=authority_token,
        )
        with self.connection(write=False) as connection:
            task = connection.execute(
                """
                SELECT t.*, p.root_path, p.archived_at AS project_archived_at
                FROM tasks t JOIN projects p ON p.project_id = t.project_id
                WHERE t.task_id = ?
                """,
                (task_id,),
            ).fetchone()
        if task is None:
            raise TeamError(f"task not found: {task_id}")
        if task["status"] != "review":
            raise TeamError("task is not in review")
        if task["archived_at"]:
            raise TeamError("task is archived; restore it before review")
        if task["project_archived_at"]:
            raise TeamError("project is archived; restore it before review")

        resolved_merge = merge_commit.strip()
        worker_commit = ""
        target_commit = ""
        if approve and task["kind"] == "write":
            with self.connection(write=False) as connection:
                worktree = str(
                    self._validate_write_worktree_assignment(
                        connection,
                        task,
                    )
                )
            status = self._worktree_git(worktree, "status", "--porcelain")
            if status.returncode != 0:
                raise TeamError(status.stderr.strip() or "could not inspect worktree")
            if status.stdout.strip():
                raise TeamError(
                    "worktree is dirty; commit all accepted changes before approval"
                )
            if not resolved_merge:
                raise TeamError("write task approval requires merge_commit")
            submitted_commit = str(task["commit_sha"] or "").strip()
            if not submitted_commit:
                raise TeamError("write task approval requires submitted commit_sha")
            worker_result = self._worktree_git(
                Path(str(task["root_path"])),
                "rev-parse",
                "--verify",
                f"{submitted_commit}^{{commit}}",
            )
            if worker_result.returncode != 0:
                raise TeamError("submitted commit_sha does not resolve")
            worker_commit = worker_result.stdout.strip()
            merge_result = self._worktree_git(
                Path(str(task["root_path"])),
                "rev-parse",
                "--verify",
                f"{resolved_merge}^{{commit}}",
            )
            if merge_result.returncode != 0:
                raise TeamError("merge_commit does not resolve in the main repository")
            resolved_merge = merge_result.stdout.strip()
            target_branch = str(task["target_branch"] or "")
            if not target_branch:
                raise TeamError("write task has no target branch")
            target_result = self._worktree_git(
                Path(str(task["root_path"])),
                "rev-parse",
                "--verify",
                f"refs/heads/{target_branch}^{{commit}}",
            )
            if target_result.returncode != 0:
                raise TeamError(f"target branch not found: {target_branch}")
            target_commit = target_result.stdout.strip()
            merge_on_target = self._worktree_git(
                Path(str(task["root_path"])),
                "merge-base",
                "--is-ancestor",
                resolved_merge,
                target_commit,
            )
            if merge_on_target.returncode != 0:
                raise TeamError(
                    f"merge_commit is not reachable from target branch {target_branch}"
                )
            worker_in_merge = self._worktree_git(
                Path(str(task["root_path"])),
                "merge-base",
                "--is-ancestor",
                worker_commit,
                resolved_merge,
            )
            if worker_in_merge.returncode != 0:
                raise TeamError(
                    "submitted commit_sha is not contained in merge_commit"
                )

        expected_version = int(task["version"])
        new_status = "done" if approve else "ready"
        stored_merge = resolved_merge if approve else ""
        stored_target_commit = target_commit if approve else ""
        now = utc_now()
        completed_at = now if approve else None
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                """
                SELECT t.status, t.version, t.archived_at, t.commit_sha,
                       t.target_branch,
                       p.archived_at AS project_archived_at
                FROM tasks t JOIN projects p ON p.project_id = t.project_id
                WHERE t.task_id = ?
                """,
                (task_id,),
            ).fetchone()
            if current is None:
                raise TeamError(f"task not found: {task_id}")
            if current["status"] != "review":
                raise TeamError("task is no longer in review")
            if current["archived_at"] or current["project_archived_at"]:
                raise TeamError("task or project was archived during review")
            if int(current["version"]) != expected_version:
                raise TeamError("task changed during review; reload and retry")
            if approve and task["kind"] == "write" and (
                str(current["commit_sha"] or "") != str(task["commit_sha"] or "")
                or str(current["target_branch"] or "")
                != str(task["target_branch"] or "")
            ):
                raise TeamError(
                    "reviewed task snapshot changed during review; reload and retry"
                )
            updated = connection.execute(
                """
                UPDATE tasks
                SET status = ?, review_notes = ?, merge_commit = ?,
                    review_target_commit = ?, claimed_by = NULL, claim_token = NULL,
                    lease_until = NULL, completed_at = ?, updated_at = ?,
                    version = version + 1
                WHERE task_id = ? AND status = 'review' AND version = ?
                  AND archived_at IS NULL
                """,
                (
                    new_status,
                    notes,
                    stored_merge,
                    stored_target_commit,
                    completed_at,
                    now,
                    task_id,
                    expected_version,
                ),
            )
            if updated.rowcount != 1:
                raise TeamError("task review conflicted with another update")
            self._event(
                connection,
                task_id,
                "reviewed",
                actor,
                {
                    "approve": approve,
                    "notes": notes,
                    "task_version": expected_version,
                    "target_branch": str(task["target_branch"] or "")
                    if approve and task["kind"] == "write"
                    else "",
                    "target_commit": target_commit,
                    "merge_commit": stored_merge,
                    "worker_commit": worker_commit,
                },
            )
        return self.get_task(task_id)

    def add_message(
        self,
        repo: str | Path,
        *,
        sender: str,
        body: str,
        recipient: str | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        project = self.register_project(repo)
        with self.connection() as connection:
            if task_id:
                task = connection.execute(
                    "SELECT project_id FROM tasks WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                if task is None or task["project_id"] != project["project_id"]:
                    raise TeamError("task does not belong to this project")
            cursor = connection.execute(
                """
                INSERT INTO messages(project_id, task_id, sender, recipient, body, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (project["project_id"], task_id, sender, recipient, body, utc_now()),
            )
            message_id = int(cursor.lastrowid)
            if task_id:
                self._event(
                    connection,
                    task_id,
                    "message",
                    sender,
                    {"recipient": recipient, "message_id": message_id},
                )
            message = {
                "message_id": message_id,
                "project_id": project["project_id"],
                "task_id": task_id,
                "sender": sender,
                "recipient": recipient,
                "body": body,
            }
            self._record_request_result(
                connection,
                message,
                task_id=task_id,
            )
        return message

    def list_messages(
        self,
        repo: str | Path,
        *,
        task_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        project = self.register_project(repo)
        limit = max(1, min(int(limit), 200))
        with self.connection(write=False) as connection:
            if task_id:
                rows = connection.execute(
                    """
                    SELECT * FROM messages
                    WHERE project_id = ? AND task_id = ?
                    ORDER BY message_id DESC LIMIT ?
                    """,
                    (project["project_id"], task_id, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM messages WHERE project_id = ?
                    ORDER BY message_id DESC LIMIT ?
                    """,
                    (project["project_id"], limit),
                ).fetchall()
        return list(reversed([dict(row) for row in rows]))

    @staticmethod
    def _authorized_task(
        connection: sqlite3.Connection,
        task_id: str,
        claim_token: str,
        actor: str,
    ) -> sqlite3.Row:
        task = connection.execute(
            "SELECT * FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if task is None:
            raise TeamError(f"task not found: {task_id}")
        if not claim_token or not secrets.compare_digest(
            str(task["claim_token"] or ""),
            claim_token,
        ):
            raise TeamError("invalid claim token")
        if task["claimed_by"] != actor:
            raise TeamError(f"task is claimed by {task['claimed_by']}")
        if ServiceTime.lease_expired(task["lease_until"]):
            raise TeamError("task lease expired; claim it again")
        return task

    @staticmethod
    def _lease_expired(value: str | None) -> bool:
        return ServiceTime.lease_expired(value)

    @staticmethod
    def _is_ready(connection: sqlite3.Connection, task_id: str) -> bool:
        task = connection.execute(
            "SELECT status FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if task is None or task["status"] not in {"ready", "backlog"}:
            return False
        return not TeamService._blocking_tasks(connection, task_id)

    @staticmethod
    def _blocking_tasks(
        connection: sqlite3.Connection,
        task_id: str,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT t.task_id, t.title, t.status, t.archived_at
            FROM task_dependencies d
            JOIN tasks t ON t.task_id = d.depends_on
            WHERE d.task_id = ? AND t.status != 'done'
            ORDER BY t.task_id
            """,
            (task_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    # Stay well below SQLite's default 32766 host-parameter limit so a large
    # board never raises "too many SQL variables". Chunking preserves the
    # per-task ordering because each chunk is ordered and a task's dependencies
    # all live in the single chunk that contains its id.
    _BLOCKING_QUERY_CHUNK_SIZE = 1000

    @classmethod
    def _blocking_tasks_map(
        cls,
        connection: sqlite3.Connection,
        task_ids: list[str],
    ) -> dict[str, list[dict[str, Any]]]:
        """Batch-load unfinished dependencies for many tasks in chunked queries."""
        blocked_by: dict[str, list[dict[str, Any]]] = {
            task_id: [] for task_id in task_ids
        }
        if not task_ids:
            return blocked_by
        chunk_size = cls._BLOCKING_QUERY_CHUNK_SIZE
        for start in range(0, len(task_ids), chunk_size):
            chunk = task_ids[start : start + chunk_size]
            placeholders = ",".join("?" for _ in chunk)
            rows = connection.execute(
                f"""
                SELECT d.task_id, t.task_id AS depends_on, t.title, t.status,
                       t.archived_at
                FROM task_dependencies d
                JOIN tasks t ON t.task_id = d.depends_on
                WHERE d.task_id IN ({placeholders}) AND t.status != 'done'
                ORDER BY d.task_id, t.task_id
                """,
                chunk,
            ).fetchall()
            for row in rows:
                blocked_by[str(row["task_id"])].append(
                    {
                        "task_id": row["depends_on"],
                        "title": row["title"],
                        "status": row["status"],
                        "archived_at": row["archived_at"],
                    }
                )
        return blocked_by

    def _new_task_id(self) -> str:
        for _ in range(20):
            candidate = datetime.now(UTC).strftime("T-%Y%m%d-") + secrets.token_hex(3).upper()
            with self.connection(write=False) as connection:
                exists = connection.execute(
                    "SELECT 1 FROM tasks WHERE task_id = ?",
                    (candidate,),
                ).fetchone()
            if not exists:
                return candidate
        raise TeamError("could not allocate task id")


class ServiceTime:
    @staticmethod
    def lease_expired(value: str | None) -> bool:
        if not value:
            return False
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return True
        return parsed <= datetime.now(UTC)


def _replace_managed_block(text: str, name: str, block: str) -> str:
    start = f"<!-- {name}:START -->"
    end = f"<!-- {name}:END -->"
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
    if pattern.search(text):
        return pattern.sub(lambda _match: block.strip(), text, count=1)
    if text.strip():
        return text.rstrip() + "\n\n" + block.strip()
    return block.strip()


def _agents_block(
    project_id: str,
    max_threads: int,
    template_path: Path,
) -> str:
    template = template_path.read_text(encoding="utf-8")
    return template.strip().format(
        project_id=project_id,
        max_threads=max_threads,
    )
