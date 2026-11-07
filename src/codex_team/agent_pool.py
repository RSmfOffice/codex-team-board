from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from .db import connect, connect_readonly, utc_now


class AgentPoolError(RuntimeError):
    pass


class AgentPool:
    """Persistent Agent lifecycle state backed only by the team database."""

    ROLES = frozenset({"lead", "team_explorer", "team_worker", "team_reviewer"})
    TASK_KINDS = frozenset({"read", "write", "review", "ops"})
    CONTINUITY_MODES = frozenset({"auto", "reuse", "fresh"})
    IDLE_STATUSES = ("loaded", "idle", "stored")

    def __init__(
        self,
        database: str | Path | Any | None = None,
        *,
        db_path: str | Path | None = None,
    ) -> None:
        source = db_path if db_path is not None else database
        settings = getattr(source, "settings", source)
        resolved_path = getattr(settings, "db_path", source)
        if not isinstance(resolved_path, (str, Path)):
            raise TypeError("AgentPool requires a database path or Settings")
        self.db_path = Path(resolved_path)

    @contextmanager
    def _connection(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        connection = (
            connect(self.db_path)
            if write or not self.db_path.is_file()
            else connect_readonly(self.db_path)
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
        agent_id: str,
        event_type: str,
        actor: str,
        data: Mapping[str, Any] | None = None,
        *,
        created_at: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO cluster_agent_events(
                agent_id, event_type, actor, data_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                agent_id,
                event_type,
                actor,
                json.dumps(data or {}, ensure_ascii=False, sort_keys=True),
                created_at or utc_now(),
            ),
        )

    @staticmethod
    def _require_text(value: str, name: str) -> str:
        result = str(value or "").strip()
        if not result:
            raise AgentPoolError(f"{name} must not be empty")
        return result

    @staticmethod
    def _normalize_model(value: str | None) -> str:
        return str(value or "").strip()

    @staticmethod
    def _row(
        connection: sqlite3.Connection,
        agent_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM cluster_agents WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
        if row is None:
            raise AgentPoolError(f"agent not found: {agent_id}")
        return row

    @staticmethod
    def _agent_dict(row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    @staticmethod
    def _usage_value(usage: Mapping[str, Any], *names: str) -> Any:
        for name in names:
            if name in usage:
                return usage[name]
        return 0

    @classmethod
    def _normalize_usage(cls, usage: Mapping[str, Any] | None) -> dict[str, int | None]:
        values = usage or {}
        input_tokens = max(
            0,
            int(
                cls._usage_value(
                    values,
                    "input",
                    "input_tokens",
                    "inputTokens",
                )
                or 0
            ),
        )
        cached_input_tokens = max(
            0,
            int(
                cls._usage_value(
                    values,
                    "cached",
                    "cached_tokens",
                    "cached_input_tokens",
                    "cachedInputTokens",
                )
                or 0
            ),
        )
        cached_input_tokens = min(cached_input_tokens, input_tokens)
        output_tokens = max(
            0,
            int(cls._usage_value(values, "output", "output_tokens", "outputTokens") or 0),
        )
        context_tokens = max(
            0,
            int(cls._usage_value(values, "context", "context_tokens", "contextTokens") or 0),
        )
        raw_window = cls._usage_value(
            values,
            "model_context_window",
            "modelContextWindow",
        )
        model_context_window = (
            max(0, int(raw_window)) if raw_window not in {None, 0} else None
        )
        return {
            "input_tokens": input_tokens,
            "cached_input_tokens": cached_input_tokens,
            "uncached_input_tokens": input_tokens - cached_input_tokens,
            "output_tokens": output_tokens,
            "context_tokens": context_tokens,
            "model_context_window": model_context_window,
        }

    def register_thread(
        self,
        project_id: str,
        role: str,
        workstream_key: str,
        thread_id: str,
        model: str,
        model_provider: str,
        display_name: str,
    ) -> dict[str, Any]:
        project_id = self._require_text(project_id, "project_id")
        role = self._require_text(role, "role").lower()
        if role not in self.ROLES:
            raise AgentPoolError(f"unsupported role: {role}")
        workstream_key = self._require_text(workstream_key, "workstream_key")
        thread_id = self._require_text(thread_id, "thread_id")
        display_name = self._require_text(display_name, "display_name")
        model = self._normalize_model(model)
        model_provider = self._normalize_model(model_provider)
        now = utc_now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            project = connection.execute(
                "SELECT project_id, archived_at FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if project is None:
                raise AgentPoolError(f"project not found: {project_id}")
            if project["archived_at"] is not None:
                raise AgentPoolError(
                    "project is archived; restore it before registering agents"
                )
            existing = connection.execute(
                "SELECT * FROM cluster_agents WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            if existing is not None:
                identity = (
                    existing["project_id"],
                    existing["role"],
                    existing["workstream_key"],
                    existing["model"],
                    existing["model_provider"],
                )
                requested = (
                    project_id,
                    role,
                    workstream_key,
                    model,
                    model_provider,
                )
                if identity != requested:
                    raise AgentPoolError(
                        f"thread {thread_id} is registered with different agent metadata"
                    )
                return self._agent_dict(existing)
            agent_id = f"A-{uuid.uuid4().hex[:12].upper()}"
            connection.execute(
                """
                INSERT INTO cluster_agents(
                    agent_id, project_id, role, workstream_key, display_name,
                    thread_id, status, model, model_provider, last_loaded_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'idle', ?, ?, ?, ?, ?)
                """,
                (
                    agent_id,
                    project_id,
                    role,
                    workstream_key,
                    display_name,
                    thread_id,
                    model,
                    model_provider,
                    now,
                    now,
                    now,
                ),
            )
            self._event(
                connection,
                agent_id,
                "registered",
                "agent_pool",
                {
                    "project_id": project_id,
                    "role": role,
                    "workstream_key": workstream_key,
                    "thread_id": thread_id,
                    "model": model,
                    "model_provider": model_provider,
                },
                created_at=now,
            )
            return self._agent_dict(self._row(connection, agent_id))

    @staticmethod
    def _goal_workstream_is_continuous(
        connection: sqlite3.Connection,
        project_id: str,
        workstream_key: str,
    ) -> bool:
        row = connection.execute(
            """
            SELECT 1
            FROM cluster_goals
            WHERE project_id = ? AND workstream_key = ?
              AND archived_at IS NULL
              AND TRIM(continuity_reason) != ''
            LIMIT 1
            """,
            (project_id, workstream_key),
        ).fetchone()
        return row is not None

    def _reuse_allowed(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: str,
        role: str,
        workstream_key: str,
        continuity_mode: str,
        task_kind: str,
    ) -> bool:
        if continuity_mode == "fresh":
            return False
        if continuity_mode == "reuse":
            return True
        if role in {"team_explorer", "team_reviewer"}:
            return True
        if role == "team_worker":
            return task_kind != "write"
        if role == "lead":
            return self._goal_workstream_is_continuous(
                connection,
                project_id,
                workstream_key,
            )
        return False

    def select_agent(
        self,
        project_id: str,
        role: str,
        workstream_key: str,
        continuity_mode: str,
        task_kind: str,
        model: str,
        model_provider: str,
    ) -> dict[str, Any] | None:
        project_id = self._require_text(project_id, "project_id")
        role = self._require_text(role, "role").lower()
        workstream_key = self._require_text(workstream_key, "workstream_key")
        continuity_mode = self._require_text(
            continuity_mode,
            "continuity_mode",
        ).lower()
        task_kind = self._require_text(task_kind, "task_kind").lower()
        if role not in self.ROLES:
            raise AgentPoolError(f"unsupported role: {role}")
        if continuity_mode not in self.CONTINUITY_MODES:
            raise AgentPoolError(f"unsupported continuity_mode: {continuity_mode}")
        if task_kind not in self.TASK_KINDS:
            raise AgentPoolError(f"unsupported task_kind: {task_kind}")
        model = self._normalize_model(model)
        model_provider = self._normalize_model(model_provider)
        now = utc_now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            project = connection.execute(
                "SELECT project_id, archived_at FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if project is None:
                raise AgentPoolError(f"project not found: {project_id}")
            if project["archived_at"] is not None:
                return None
            if not self._reuse_allowed(
                connection,
                project_id=project_id,
                role=role,
                workstream_key=workstream_key,
                continuity_mode=continuity_mode,
                task_kind=task_kind,
            ):
                return None
            row = connection.execute(
                f"""
                SELECT *
                FROM cluster_agents
                WHERE project_id = ? AND role = ? AND workstream_key = ?
                  AND model = ? AND model_provider = ?
                  AND status IN ({", ".join("?" for _ in self.IDLE_STATUSES)})
                  AND archived_at IS NULL
                  AND reset_requested_at IS NULL
                ORDER BY
                    CASE WHEN last_used_at IS NULL THEN 0 ELSE 1 END,
                    last_used_at ASC,
                    created_at ASC,
                    agent_id ASC
                LIMIT 1
                """,
                (
                    project_id,
                    role,
                    workstream_key,
                    model,
                    model_provider,
                    *self.IDLE_STATUSES,
                ),
            ).fetchone()
            if row is None:
                return None
            updated = connection.execute(
                f"""
                UPDATE cluster_agents
                SET status = 'busy', last_used_at = ?, updated_at = ?
                WHERE agent_id = ?
                  AND status IN ({", ".join("?" for _ in self.IDLE_STATUSES)})
                  AND archived_at IS NULL
                  AND reset_requested_at IS NULL
                """,
                (
                    now,
                    now,
                    row["agent_id"],
                    *self.IDLE_STATUSES,
                ),
            )
            if updated.rowcount != 1:
                return None
            self._event(
                connection,
                row["agent_id"],
                "selected",
                "agent_pool",
                {
                    "from_status": row["status"],
                    "continuity_mode": continuity_mode,
                    "task_kind": task_kind,
                    "workstream_key": workstream_key,
                },
                created_at=now,
            )
            return self._agent_dict(self._row(connection, row["agent_id"]))

    def mark_busy(
        self,
        agent_id: str,
        goal_id: str | None,
        task_id: str | None,
        job_id: str | None,
        turn_id: str | None,
    ) -> dict[str, Any]:
        agent_id = self._require_text(agent_id, "agent_id")
        now = utc_now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            agent = self._row(connection, agent_id)
            if agent["archived_at"] is not None:
                raise AgentPoolError("archived agent cannot become busy")
            if agent["status"] in {"superseded", "failed"}:
                raise AgentPoolError(
                    f"{agent['status']} agent cannot become busy"
                )
            assigned_job = agent["current_job_id"]
            if (
                agent["status"] == "busy"
                and assigned_job is not None
                and assigned_job != job_id
            ):
                raise AgentPoolError(
                    f"agent is already assigned to job {assigned_job}"
                )
            connection.execute(
                """
                UPDATE cluster_agents
                SET status = 'busy', current_goal_id = ?, current_task_id = ?,
                    current_job_id = ?, last_turn_id = COALESCE(?, last_turn_id),
                    last_used_at = ?, updated_at = ?
                WHERE agent_id = ?
                """,
                (
                    goal_id,
                    task_id,
                    job_id,
                    turn_id,
                    now,
                    now,
                    agent_id,
                ),
            )
            self._event(
                connection,
                agent_id,
                "busy",
                "agent_pool",
                {
                    "goal_id": goal_id,
                    "task_id": task_id,
                    "job_id": job_id,
                    "turn_id": turn_id,
                },
                created_at=now,
            )
            return self._agent_dict(self._row(connection, agent_id))

    def release(
        self,
        agent_id: str,
        usage: Mapping[str, Any] | None,
        cache_epoch_changed: bool = False,
    ) -> dict[str, Any]:
        agent_id = self._require_text(agent_id, "agent_id")
        normalized = self._normalize_usage(usage)
        now = utc_now()
        status = "stored" if cache_epoch_changed else "idle"
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            agent = self._row(connection, agent_id)
            if agent["archived_at"] is not None:
                raise AgentPoolError("archived agent cannot be released")
            if agent["status"] in {"superseded", "failed"}:
                raise AgentPoolError(
                    f"{agent['status']} agent cannot be released"
                )
            connection.execute(
                """
                UPDATE cluster_agents
                SET status = ?, current_goal_id = NULL, current_task_id = NULL,
                    current_job_id = NULL, last_input_tokens = ?,
                    last_cached_input_tokens = ?, last_uncached_input_tokens = ?,
                    total_input_tokens = total_input_tokens + ?,
                    total_cached_input_tokens = total_cached_input_tokens + ?,
                    total_uncached_input_tokens = total_uncached_input_tokens + ?,
                    context_tokens = ?,
                    model_context_window = COALESCE(?, model_context_window),
                    turn_count = turn_count + 1, last_used_at = ?, updated_at = ?,
                    last_unloaded_at = CASE WHEN ? THEN ? ELSE last_unloaded_at END
                WHERE agent_id = ?
                """,
                (
                    status,
                    normalized["input_tokens"],
                    normalized["cached_input_tokens"],
                    normalized["uncached_input_tokens"],
                    normalized["input_tokens"],
                    normalized["cached_input_tokens"],
                    normalized["uncached_input_tokens"],
                    normalized["context_tokens"],
                    normalized["model_context_window"],
                    now,
                    now,
                    1 if cache_epoch_changed else 0,
                    now,
                    agent_id,
                ),
            )
            self._event(
                connection,
                agent_id,
                "released",
                "agent_pool",
                {
                    "from_status": agent["status"],
                    "to_status": status,
                    "cache_epoch_changed": bool(cache_epoch_changed),
                    "usage": normalized,
                },
                created_at=now,
            )
            return self._agent_dict(self._row(connection, agent_id))

    def mark_failed(
        self,
        agent_id: str,
        reason: str,
        needs_reset: bool = False,
    ) -> dict[str, Any]:
        agent_id = self._require_text(agent_id, "agent_id")
        reason = self._require_text(reason, "reason")
        now = utc_now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            agent = self._row(connection, agent_id)
            if agent["archived_at"] is not None:
                raise AgentPoolError("archived agent cannot be marked failed")
            reset_at = now if needs_reset else None
            connection.execute(
                """
                UPDATE cluster_agents
                SET status = 'failed', current_goal_id = NULL,
                    current_task_id = NULL, current_job_id = NULL,
                    last_error = ?, updated_at = ?, last_unloaded_at = ?,
                    reset_requested_at = CASE
                        WHEN ? THEN COALESCE(reset_requested_at, ?)
                        ELSE reset_requested_at
                    END,
                    reset_reason = CASE WHEN ? THEN ? ELSE reset_reason END
                WHERE agent_id = ?
                """,
                (
                    reason,
                    now,
                    now,
                    1 if needs_reset else 0,
                    reset_at,
                    1 if needs_reset else 0,
                    reason,
                    agent_id,
                ),
            )
            self._event(
                connection,
                agent_id,
                "failed",
                "agent_pool",
                {
                    "from_status": agent["status"],
                    "reason": reason,
                    "needs_reset": bool(needs_reset),
                },
                created_at=now,
            )
            return self._agent_dict(self._row(connection, agent_id))

    def mark_superseded(
        self,
        agent_id: str,
        reason: str,
        actor: str,
    ) -> dict[str, Any]:
        agent_id = self._require_text(agent_id, "agent_id")
        reason = self._require_text(reason, "reason")
        actor = self._require_text(actor, "actor")
        now = utc_now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            agent = self._row(connection, agent_id)
            if agent["status"] == "superseded":
                return self._agent_dict(agent)
            connection.execute(
                """
                UPDATE cluster_agents
                SET status = 'superseded', current_goal_id = NULL,
                    current_task_id = NULL, current_job_id = NULL,
                    last_error = ?, reset_requested_at = NULL,
                    reset_reason = '', updated_at = ?, last_unloaded_at = ?
                WHERE agent_id = ?
                """,
                (reason, now, now, agent_id),
            )
            self._event(
                connection,
                agent_id,
                "superseded",
                actor,
                {
                    "from_status": agent["status"],
                    "reason": reason,
                },
                created_at=now,
            )
            return self._agent_dict(self._row(connection, agent_id))

    def request_reset(
        self,
        agent_id: str,
        reason: str,
        actor: str,
    ) -> dict[str, Any]:
        agent_id = self._require_text(agent_id, "agent_id")
        reason = self._require_text(reason, "reason")
        actor = self._require_text(actor, "actor")
        now = utc_now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            agent = self._row(connection, agent_id)
            if agent["status"] == "busy":
                raise AgentPoolError("busy agents cannot be reset")
            if agent["status"] == "superseded":
                raise AgentPoolError("superseded agents cannot be reset")
            if agent["archived_at"] is not None:
                raise AgentPoolError("archived agents cannot be reset")
            connection.execute(
                """
                UPDATE cluster_agents
                SET reset_requested_at = COALESCE(reset_requested_at, ?),
                    reset_reason = ?, updated_at = ?
                WHERE agent_id = ?
                """,
                (now, reason, now, agent_id),
            )
            self._event(
                connection,
                agent_id,
                "reset_requested",
                actor,
                {"reason": reason},
                created_at=now,
            )
            return self._agent_dict(self._row(connection, agent_id))

    def apply_reset_requests(
        self,
        thread_unsubscribe_callback: Any,
    ) -> list[dict[str, Any]]:
        if not callable(thread_unsubscribe_callback):
            raise TypeError("thread_unsubscribe_callback must be callable")
        with self._connection(write=False) as connection:
            agents = [
                self._agent_dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM cluster_agents
                    WHERE reset_requested_at IS NOT NULL
                      AND archived_at IS NULL
                    ORDER BY reset_requested_at, created_at, agent_id
                    """
                ).fetchall()
            ]
        applied: list[dict[str, Any]] = []
        for agent in agents:
            agent_id = str(agent["agent_id"])
            try:
                thread_unsubscribe_callback(agent["thread_id"])
            except Exception as exc:
                now = utc_now()
                with self._connection() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    current = self._row(connection, agent_id)
                    if current["reset_requested_at"] is None:
                        continue
                    connection.execute(
                        """
                        UPDATE cluster_agents
                        SET last_error = ?, updated_at = ?
                        WHERE agent_id = ?
                        """,
                        (f"reset unsubscribe failed: {exc}", now, agent_id),
                    )
                    self._event(
                        connection,
                        agent_id,
                        "reset_failed",
                        "agent_pool",
                        {"error": str(exc)},
                        created_at=now,
                    )
                continue
            now = utc_now()
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = self._row(connection, agent_id)
                if current["reset_requested_at"] is None:
                    continue
                if current["archived_at"] is not None:
                    continue
                connection.execute(
                    """
                    UPDATE cluster_agents
                    SET status = 'superseded', current_goal_id = NULL,
                        current_task_id = NULL, current_job_id = NULL,
                        reset_requested_at = NULL, reset_reason = '',
                        updated_at = ?, last_unloaded_at = ?
                    WHERE agent_id = ?
                    """,
                    (now, now, agent_id),
                )
                self._event(
                    connection,
                    agent_id,
                    "reset_applied",
                    "agent_pool",
                    {"thread_id": current["thread_id"]},
                    created_at=now,
                )
                applied.append(self._agent_dict(self._row(connection, agent_id)))
        return applied

    def list_agents(
        self,
        project_id: str | None = None,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if project_id is not None:
            clauses.append("project_id = ?")
            parameters.append(self._require_text(project_id, "project_id"))
        if not include_archived:
            clauses.append("archived_at IS NULL")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connection(write=False) as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM cluster_agents
                {where}
                ORDER BY created_at, agent_id
                """,
                parameters,
            ).fetchall()
        return [self._agent_dict(row) for row in rows]

    def archive_project(self, project_id: str, archived: bool) -> list[dict[str, Any]]:
        project_id = self._require_text(project_id, "project_id")
        archived = bool(archived)
        now = utc_now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            project = connection.execute(
                "SELECT * FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if project is None:
                raise AgentPoolError(f"project not found: {project_id}")
            project_changed = (project["archived_at"] is not None) != archived
            if archived and project_changed:
                busy = connection.execute(
                    """
                    SELECT agent_id FROM cluster_agents
                    WHERE project_id = ?
                      AND archived_at IS NULL
                      AND status IN ('starting', 'busy')
                    LIMIT 1
                    """,
                    (project_id,),
                ).fetchone()
                if busy is not None:
                    raise AgentPoolError(
                        f"project has a busy agent: {busy['agent_id']}"
                    )
            agents = connection.execute(
                "SELECT * FROM cluster_agents WHERE project_id = ? ORDER BY agent_id",
                (project_id,),
            ).fetchall()
            archived_at = now if archived else None
            if project_changed:
                connection.execute(
                    """
                    UPDATE projects
                    SET archived_at = ?, updated_at = ?
                    WHERE project_id = ?
                    """,
                    (archived_at, now, project_id),
                )
            for agent in agents:
                if (agent["archived_at"] is not None) == archived:
                    continue
                connection.execute(
                    """
                    UPDATE cluster_agents
                    SET archived_at = ?, updated_at = ?
                    WHERE agent_id = ?
                    """,
                    (archived_at, now, agent["agent_id"]),
                )
                self._event(
                    connection,
                    agent["agent_id"],
                    "archived" if archived else "restored",
                    "agent_pool",
                    {"project_id": project_id},
                    created_at=now,
                )
            rows = connection.execute(
                """
                SELECT * FROM cluster_agents
                WHERE project_id = ?
                ORDER BY created_at, agent_id
                """,
                (project_id,),
            ).fetchall()
            return [self._agent_dict(row) for row in rows]
