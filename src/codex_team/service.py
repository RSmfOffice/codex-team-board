from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import subprocess
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Mapping

from .agent_pool import AgentPool, AgentPoolError
from .config import Settings
from .db import connect, connect_readonly, utc_now
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
CLAIM_WAIT_WARNING_SECONDS = 60
CLAIM_WAIT_CRITICAL_SECONDS = 300


class TeamError(RuntimeError):
    pass


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

    @contextmanager
    def connection(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        self.settings.ensure()
        connection = (
            connect(self.settings.db_path)
            if write or not self.settings.db_path.is_file()
            else connect_readonly(self.settings.db_path)
        )
        try:
            yield connection
            if write:
                connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        task_id: str,
        event_type: str,
        actor: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO task_events(task_id, event_type, actor, data_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                task_id,
                event_type,
                actor,
                json.dumps(data or {}, ensure_ascii=False, sort_keys=True),
                utc_now(),
            ),
        )

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
        result = subprocess.run(
            ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
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
    ) -> dict[str, Any]:
        project = self.get_project(project_id)
        now = utc_now()
        if archived:
            with self.connection(write=False) as connection:
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
                    f"cannot archive project with unfinished goal: {active_goal['goal_id']}"
                )
            if running_task:
                raise TeamError(
                    f"cannot archive project with running task: {running_task['task_id']}"
                )
            if active_job:
                raise TeamError(
                    f"cannot archive project with active job: {active_job['job_id']}"
                )
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE projects
                SET archived_at = ?, updated_at = ?
                WHERE project_id = ?
                """,
                (now if archived else None, now, project_id),
            )
        return self.get_project(project["project_id"])

    def set_task_archived(
        self,
        task_id: str,
        archived: bool,
    ) -> dict[str, Any]:
        task = self.get_task(task_id)
        if archived:
            if task["status"] == "running":
                raise TeamError(f"cannot archive running task: {task_id}")
            with self.connection(write=False) as connection:
                active_job = connection.execute(
                    """
                    SELECT job_id FROM cluster_jobs
                    WHERE task_id = ? AND status IN ('starting', 'running')
                    LIMIT 1
                    """,
                    (task_id,),
                ).fetchone()
                active_goal = None
                if task.get("goal_id"):
                    active_goal = connection.execute(
                        """
                        SELECT goal_id FROM cluster_goals
                        WHERE goal_id = ? AND status IN ('planning', 'active', 'paused')
                        """,
                        (task["goal_id"],),
                    ).fetchone()
            if active_job:
                raise TeamError(
                    f"cannot archive task with active job: {active_job['job_id']}"
                )
            if active_goal:
                raise TeamError(
                    f"cannot archive task in unfinished goal: {active_goal['goal_id']}"
                )
        now = utc_now()
        with self.connection() as connection:
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
                "human",
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
        template_name = self.ROLE_FILES.get(normalized)
        if template_name is None:
            raise TeamError(
                "unknown role; expected one of: " + ", ".join(sorted(self.ROLE_FILES))
            )
        path = self.settings.project_root / "templates" / "roles" / template_name
        source = "global"
        if repo:
            project_root = self._root(repo)
            project_path = project_root / ".codex" / "team-roles" / template_name
            if project_path.is_file():
                path = project_path
                source = "project"
        if not path.is_file():
            raise TeamError(f"role prompt template missing: {path}")
        return {
            "role": normalized,
            "prompt": path.read_text(encoding="utf-8"),
            "source": str(path),
            "scope": source,
        }

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
        dependencies = list(dict.fromkeys(depends_on or []))
        task_id = self._new_task_id()
        now = utc_now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if goal_id:
                goal = connection.execute(
                    """
                    SELECT project_id, status, archived_at
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
                    status, priority, owner_role, continuity_mode, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?, ?, ?, ?)
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
                },
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

    def get_task_events(self, task_id: str, *, limit: int = 100) -> dict[str, Any]:
        limit = max(1, min(int(limit), 500))
        with self.connection(write=False) as connection:
            task = connection.execute(
                "SELECT task_id, title FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if task is None:
                raise TeamError(f"task not found: {task_id}")
            rows = connection.execute(
                """
                SELECT event_id, event_type, actor, data_json, created_at
                FROM task_events
                WHERE task_id = ?
                ORDER BY event_id DESC
                LIMIT ?
                """,
                (task_id, limit),
            ).fetchall()
        events = []
        for row in reversed(rows):
            item = dict(row)
            try:
                item["data"] = json.loads(item.pop("data_json"))
            except (TypeError, ValueError):
                item["data"] = {}
                item.pop("data_json", None)
            events.append(item)
        return {"task": dict(task), "events": events}

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
            result = []
            for row in rows:
                item = dict(row)
                item.pop("claim_token", None)
                item["blocked_by"] = self._blocking_tasks(connection, item["task_id"])
                item["ready"] = self._is_ready(connection, item["task_id"])
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
                       j.status AS current_job_status
                FROM cluster_agents a
                JOIN projects p ON p.project_id = a.project_id
                LEFT JOIN cluster_goals g ON g.goal_id = a.current_goal_id
                LEFT JOIN tasks t ON t.task_id = a.current_task_id
                LEFT JOIN cluster_jobs j ON j.job_id = a.current_job_id
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
                agent["cache_hit_rate"] = self._cache_hit_rate(
                    agent.get("total_input_tokens"),
                    agent.get("total_cached_input_tokens"),
                    agent.get("total_uncached_input_tokens"),
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

            def is_historical_task(task: dict[str, Any]) -> bool:
                if task.get("archived_at"):
                    return True
                if str(task.get("project_id")) in archived_project_ids:
                    return True
                goal_id = task.get("goal_id")
                return bool(goal_id and str(goal_id) in archived_goal_ids)

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
                    "kind": job["kind"],
                    "role": job["role"],
                    "status": job["status"],
                    "pid": job.get("pid"),
                    "attempt": job.get("attempt"),
                    "started_at": job.get("started_at"),
                    "thread_id": job.get("thread_id"),
                    "turn_id": job.get("turn_id"),
                    "runtime": job.get("runtime"),
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

            def parse_time(value: str | None) -> datetime | None:
                if not value:
                    return None
                try:
                    parsed = datetime.fromisoformat(
                        str(value).replace("Z", "+00:00")
                    )
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=UTC)
                except (TypeError, ValueError):
                    return None
                return parsed

            def age_seconds(value: str | None) -> int:
                parsed = parse_time(value)
                if parsed is None:
                    return 0
                return max(0, int((now_utc - parsed).total_seconds()))

            def heartbeat_state(age: int) -> str:
                if age >= heartbeat_critical_seconds:
                    return "critical"
                if age >= heartbeat_warning_seconds:
                    return "warning"
                return "normal"

            worker_heartbeat_ages: dict[str, int] = {}
            for job in active_jobs:
                if job.get("kind") != "worker":
                    continue
                heartbeat_at = job.get("started_at")
                if job.get("task_id"):
                    activity_at = latest_task_activity.get(
                        str(job["task_id"]),
                    )
                    started_at = parse_time(heartbeat_at)
                    activity_time = parse_time(activity_at)
                    if activity_time and (
                        started_at is None or activity_time >= started_at
                    ):
                        heartbeat_at = activity_at
                worker_heartbeat_ages[str(job["job_id"])] = age_seconds(
                    heartbeat_at
                )
            for task in tasks:
                task["active_job"] = active_jobs_by_task.get(task["task_id"])
                wait_seconds = 0
                if task["status"] == "ready" and not task["blocked_by"]:
                    try:
                        created = datetime.fromisoformat(
                            str(task["created_at"]).replace("Z", "+00:00")
                        )
                        wait_seconds = max(0, int((now_utc - created).total_seconds()))
                    except (TypeError, ValueError):
                        wait_seconds = 0
                task["claim_wait_seconds"] = wait_seconds
                task["claim_wait_state"] = (
                    "critical"
                    if wait_seconds >= CLAIM_WAIT_CRITICAL_SECONDS
                    else "warning"
                    if wait_seconds >= CLAIM_WAIT_WARNING_SECONDS
                    else "normal"
                )
            tasks_by_goal: dict[str, list[dict[str, Any]]] = {}
            for task in tasks:
                if is_historical_task(task):
                    continue
                goal_id = task.get("goal_id")
                if goal_id:
                    tasks_by_goal.setdefault(str(goal_id), []).append(task)
            goal_states: dict[str, dict[str, Any]] = {}
            attention_count = 0
            for task in tasks:
                if is_historical_task(task):
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
                    goal["heartbeat_age_seconds"] = 0
                    goal["heartbeat_state"] = "idle"
                    goal["queue_reason"] = "none"
                    goal["estimated_wait_seconds"] = 0
                    continue
                if is_stalled_goal:
                    goal["health"] = "stalled"
                elif (
                    goal_status == "failed"
                    or has_blocked
                    or has_critical_wait
                ):
                    goal["health"] = "critical"
                elif (
                    goal_status == "paused"
                    or has_review
                    or has_warning_wait
                ):
                    goal["health"] = "warning"
                elif has_active_job or has_running:
                    goal["health"] = "active"
                elif goal_status == "planning":
                    goal["health"] = "planning"
                else:
                    goal["health"] = "healthy"
                if is_stalled_goal:
                    goal["next_action"] = "inspect_stalled_goal"
                elif goal_status == "failed":
                    goal["next_action"] = "inspect_failed_goal"
                elif goal_status == "paused":
                    goal["next_action"] = "resume_or_cancel_goal"
                elif has_blocked:
                    goal["next_action"] = "resolve_blocked_tasks"
                elif has_critical_wait:
                    goal["next_action"] = "assign_ready_tasks"
                elif has_review:
                    goal["next_action"] = "review_completed_tasks"
                elif has_active_job:
                    goal["next_action"] = "monitor_active_jobs"
                elif has_running:
                    goal["next_action"] = "monitor_running_tasks"
                elif has_ready:
                    goal["next_action"] = "schedule_ready_tasks"
                elif goal_status == "planning":
                    goal["next_action"] = "wait_for_lead"
                elif goal_status == "completed":
                    goal["next_action"] = "none"
                else:
                    goal["next_action"] = "monitor_goal"
                goal_jobs = active_jobs_by_goal.get(goal["goal_id"], [])
                goal["active_job_count"] = len(goal_jobs)
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
                    heartbeat_state(goal_heartbeat_age)
                    if goal_jobs
                    else "idle"
                )
                goal_tasks = tasks_by_goal.get(goal["goal_id"], [])
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
                if goal_status not in {"planning", "active"}:
                    queue_reason = "none"
                elif (
                    len(active_jobs) >= system_capacity
                    and dispatchable_ready
                ):
                    queue_reason = "global_capacity"
                elif (
                    len(active_worker_jobs) >= goal_capacity
                    and dispatchable_ready
                ):
                    queue_reason = "goal_capacity"
                elif review_tasks:
                    queue_reason = "review_pending"
                elif blocked_tasks:
                    queue_reason = "blocked"
                elif dispatchable_ready:
                    queue_reason = "ready"
                elif ready_tasks:
                    queue_reason = "dependency_wait"
                elif goal_jobs or any(
                    task["status"] == "running" for task in goal_tasks
                ):
                    queue_reason = "running"
                elif goal_status == "planning" or not goal_jobs:
                    queue_reason = "waiting_for_lead"
                else:
                    queue_reason = "none"
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
                        age_seconds(task.get(wait_field))
                        for task in wait_tasks
                    ),
                    default=(
                        age_seconds(goal.get("updated_at"))
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
                    SELECT e.event_id, e.task_id, e.event_type, e.actor, e.data_json,
                           e.created_at
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
                    SELECT e.event_id, e.task_id, e.event_type, e.actor, e.data_json,
                           e.created_at
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
                        and not is_historical_task(task)
                    ),
                    default=0,
                ),
            }
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
                "generated_at": utc_now(),
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
                worktree = task["worktree_path"]
                if not worktree or not Path(worktree).is_dir():
                    raise TeamError(
                        "write task requires an existing worktree; "
                        "call team_create_worktree first"
                    )
            now = utc_now()
            connection.execute(
                """
                UPDATE tasks
                SET status = 'running', claimed_by = ?, claim_token = ?,
                    lease_until = ?, version = version + 1, updated_at = ?
                WHERE task_id = ?
                """,
                (actor, claim_token, lease_until, now, task_id),
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
            task = self._authorized_task(connection, task_id, claim_token, actor)
            if task["status"] != "running":
                raise TeamError("heartbeat requires a running task")
            connection.execute(
                """
                UPDATE tasks
                SET lease_until = ?, updated_at = ?, version = version + 1
                WHERE task_id = ?
                """,
                (lease_until, utc_now(), task_id),
            )
            self._event(connection, task_id, "heartbeat", actor, {"lease_until": lease_until})
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
                fields.append("commit_sha = ?")
                parameters.append(commit_sha.strip())
            parameters.append(task_id)
            connection.execute(
                f"UPDATE tasks SET {', '.join(fields)} WHERE task_id = ?",
                parameters,
            )
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
    ) -> dict[str, Any]:
        with self.connection() as connection:
            task = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if task is None:
                raise TeamError(f"task not found: {task_id}")
            if claim_token:
                self._authorized_task(connection, task_id, claim_token, actor)
            if task["status"] in TERMINAL_STATUSES:
                raise TeamError("cannot block terminal task")
            connection.execute(
                """
                UPDATE tasks
                SET status = 'blocked', blocker = ?, claim_token = NULL,
                    lease_until = NULL, updated_at = ?, version = version + 1
                WHERE task_id = ?
                """,
                (reason, utc_now(), task_id),
            )
            self._event(connection, task_id, "blocked", actor, {"reason": reason})
        return self.get_task(task_id)

    def unblock_task(self, task_id: str, *, actor: str) -> dict[str, Any]:
        with self.connection() as connection:
            task = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if task is None:
                raise TeamError(f"task not found: {task_id}")
            if task["status"] != "blocked":
                raise TeamError("task is not blocked")
            connection.execute(
                """
                UPDATE tasks
                SET status = 'ready', blocker = '', claimed_by = NULL,
                    updated_at = ?, version = version + 1
                WHERE task_id = ?
                """,
                (utc_now(), task_id),
            )
            self._event(connection, task_id, "unblocked", actor, {})
        return self.get_task(task_id)

    def cancel_task(
        self,
        task_id: str,
        *,
        actor: str,
        reason: str,
    ) -> dict[str, Any]:
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
            connection.execute(
                """
                UPDATE tasks
                SET status = 'canceled', blocker = ?, claimed_by = NULL,
                    claim_token = NULL, lease_until = NULL, completed_at = ?,
                    updated_at = ?, version = version + 1
                WHERE task_id = ?
                """,
                (f"canceled: {reason}", now, now, task_id),
            )
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
        with self.connection() as connection:
            task = self._authorized_task(connection, task_id, claim_token, actor)
            if task["status"] != "running":
                raise TeamError("only running tasks can be submitted")
            resolved_commit = commit_sha.strip()
            if task["kind"] == "write":
                if not resolved_commit:
                    raise TeamError("write task submission requires commit_sha")
                worktree = task["worktree_path"]
                if not worktree or not Path(worktree).is_dir():
                    raise TeamError("write task worktree is missing")
                status = subprocess.run(
                    ["git", "-C", str(worktree), "status", "--porcelain"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
                if status.returncode != 0:
                    raise TeamError(status.stderr.strip() or "could not inspect worktree")
                if status.stdout.strip():
                    raise TeamError("worktree is dirty; commit changes before submission")
                head = subprocess.run(
                    ["git", "-C", str(worktree), "rev-parse", "HEAD"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
                if head.returncode != 0:
                    raise TeamError(head.stderr.strip() or "could not read worktree HEAD")
                actual = head.stdout.strip().lower()
                if len(resolved_commit) < 7 or not actual.startswith(resolved_commit.lower()):
                    raise TeamError("commit_sha does not match the worktree HEAD")
                resolved_commit = actual
            connection.execute(
                """
                UPDATE tasks
                SET status = 'review', result = ?, tests = ?, commit_sha = ?,
                    claim_token = NULL,
                    lease_until = NULL, updated_at = ?, version = version + 1
                WHERE task_id = ?
                """,
                (result, tests, resolved_commit, utc_now(), task_id),
            )
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
    ) -> dict[str, Any]:
        if not isinstance(actor, str):
            raise TeamError("actor must be a string")
        if not isinstance(approve, bool):
            raise TeamError("approve must be a boolean")
        if not isinstance(notes, str):
            raise TeamError("notes must be a string")
        if not isinstance(merge_commit, str):
            raise TeamError("merge_commit must be a string")
        actor = actor.strip()
        if actor.lower() not in {"lead", "main", "human"}:
            raise TeamError("only the lead or human reviewer can review tasks")
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
        if approve and task["kind"] == "write":
            worktree = task["worktree_path"]
            if not worktree or not Path(worktree).is_dir():
                raise TeamError("write task worktree is missing")
            status = subprocess.run(
                ["git", "-C", str(worktree), "status", "--porcelain"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
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
            worker_result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(task["root_path"]),
                    "rev-parse",
                    "--verify",
                    f"{submitted_commit}^{{commit}}",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if worker_result.returncode != 0:
                raise TeamError("submitted commit_sha does not resolve")
            worker_commit = worker_result.stdout.strip()
            merge_result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(task["root_path"]),
                    "rev-parse",
                    "--verify",
                    f"{resolved_merge}^{{commit}}",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if merge_result.returncode != 0:
                raise TeamError("merge_commit does not resolve in the main repository")
            resolved_merge = merge_result.stdout.strip()
            target_branch = str(task["target_branch"] or "")
            if not target_branch:
                raise TeamError("write task has no target branch")
            target_result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(task["root_path"]),
                    "rev-parse",
                    "--verify",
                    f"refs/heads/{target_branch}^{{commit}}",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if target_result.returncode != 0:
                raise TeamError(f"target branch not found: {target_branch}")
            target_commit = target_result.stdout.strip()
            merge_on_target = subprocess.run(
                [
                    "git",
                    "-C",
                    str(task["root_path"]),
                    "merge-base",
                    "--is-ancestor",
                    resolved_merge,
                    target_commit,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if merge_on_target.returncode != 0:
                raise TeamError(
                    f"merge_commit is not reachable from target branch {target_branch}"
                )
            worker_in_merge = subprocess.run(
                [
                    "git",
                    "-C",
                    str(task["root_path"]),
                    "merge-base",
                    "--is-ancestor",
                    worker_commit,
                    resolved_merge,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if worker_in_merge.returncode != 0:
                raise TeamError(
                    "submitted commit_sha is not contained in merge_commit"
                )

        expected_version = int(task["version"])
        new_status = "done" if approve else "ready"
        now = utc_now()
        completed_at = now if approve else None
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                """
                SELECT t.status, t.version, t.archived_at,
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
            updated = connection.execute(
                """
                UPDATE tasks
                SET status = ?, review_notes = ?, merge_commit = ?, claimed_by = NULL,
                    claim_token = NULL, lease_until = NULL, completed_at = ?,
                    updated_at = ?, version = version + 1
                WHERE task_id = ? AND status = 'review' AND version = ?
                  AND archived_at IS NULL
                """,
                (
                    new_status,
                    notes,
                    resolved_merge,
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
                    "merge_commit": resolved_merge,
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
        return {
            "message_id": message_id,
            "project_id": project["project_id"],
            "task_id": task_id,
            "sender": sender,
            "recipient": recipient,
            "body": body,
        }

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
