from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from .cache_contract import cache_profile_contract_status
from .db import connect, connect_readonly, utc_now
from .native_threads import (
    ASSIGNMENT_CONFIDENCES,
    ASSIGNMENT_SOURCES,
    IDENTITY_CONFIDENCES,
    IDENTITY_SOURCES,
    NATIVE_LIFECYCLES,
)
from .usage import normalize_usage as _shared_normalize_usage


# Bump whenever stable thread instructions or context architecture change.
CURRENT_AGENT_PROFILE = "v0.5"


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
    def _enum_value(
        value: str,
        name: str,
        allowed: frozenset[str],
    ) -> str:
        normalized = str(value or "").strip().lower()
        if normalized not in allowed:
            raise AgentPoolError(
                f"{name} must be one of: {', '.join(sorted(allowed))}"
            )
        return normalized

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
        agent = dict(row)
        for field in (
            "task_candidates_json",
            "native_usage_missing_fields",
        ):
            target = (
                "task_candidates"
                if field == "task_candidates_json"
                else "native_usage_missing_fields"
            )
            try:
                value = json.loads(str(agent.get(field) or "[]"))
            except (TypeError, ValueError):
                value = []
            agent[target] = value if isinstance(value, list) else []
        for field in (
            "task_assignment_ambiguous",
            "session_stale",
        ):
            agent[field] = bool(int(agent.get(field) or 0))
        for field in (
            "native_session_mtime_ns",
            "native_session_size",
        ):
            agent[field] = int(agent.get(field) or 0)
        return agent

    @staticmethod
    def _usage_value(usage: Mapping[str, Any], *names: str) -> Any:
        for name in names:
            if name in usage:
                return usage[name]
        return 0

    @staticmethod
    def _usage_field_is_present(usage: Mapping[str, Any], *names: str) -> bool:
        return any(
            name in usage and usage[name] is not None
            for name in names
        )

    @classmethod
    def _normalize_usage(cls, usage: Mapping[str, Any] | None) -> dict[str, int | None]:
        # Reuse the shared alias resolution/derivation, then apply the pool's
        # stored-usage semantics: missing counts collapse to 0 and an explicit
        # zero context window is treated as unknown.
        normalized = _shared_normalize_usage(usage)
        input_tokens = normalized["input_tokens"] or 0
        cached_input_tokens = min(
            normalized["cached_input_tokens"] or 0, input_tokens
        )
        raw_window = cls._usage_value(
            usage or {},
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
            "output_tokens": normalized["output_tokens"] or 0,
            "context_tokens": normalized["context_tokens"] or 0,
            "model_context_window": model_context_window,
        }

    @staticmethod
    def _optional_non_negative_int(value: Any) -> int | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed >= 0 else None

    @classmethod
    def _update_native_usage(
        cls,
        connection: sqlite3.Connection,
        agent_id: str,
        usage: Mapping[str, Any] | None,
    ) -> None:
        values = usage or {}
        model_context_window = cls._optional_non_negative_int(
            values.get("model_context_window")
        )
        missing_fields = sorted(
            {
                str(field).strip()
                for field in values.get("missing_fields", [])
                if str(field).strip()
            }
        )
        if not values.get("available"):
            existing = connection.execute(
                """
                SELECT native_usage_status, native_usage_missing_fields
                FROM cluster_agents
                WHERE agent_id = ?
                """,
                (agent_id,),
            ).fetchone()
            previous_status = (
                str(existing["native_usage_status"] or "")
                if existing is not None
                else ""
            )
            usage_status = (
                "stale"
                if previous_status in {"available", "partial", "stale"}
                else "unavailable"
            )
            if usage_status == "stale" and existing is not None:
                stored_missing = str(
                    existing["native_usage_missing_fields"] or "[]"
                )
                try:
                    parsed_missing = json.loads(stored_missing)
                except (TypeError, ValueError):
                    parsed_missing = []
                if isinstance(parsed_missing, list):
                    missing_fields = sorted(
                        str(field) for field in parsed_missing
                    )
            if model_context_window is not None:
                connection.execute(
                    """
                    UPDATE cluster_agents
                    SET model_context_window = ?,
                        native_usage_status = ?,
                        native_usage_missing_fields = ?
                    WHERE agent_id = ?
                    """,
                    (
                        model_context_window,
                        usage_status,
                        json.dumps(missing_fields, separators=(",", ":")),
                        agent_id,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE cluster_agents
                    SET native_usage_status = ?,
                        native_usage_missing_fields = ?
                    WHERE agent_id = ?
                    """,
                    (
                        usage_status,
                        json.dumps(missing_fields, separators=(",", ":")),
                        agent_id,
                    ),
                )
            return

        def count(name: str) -> int:
            return cls._optional_non_negative_int(values.get(name)) or 0

        raw_turn_id = values.get("last_turn_id")
        last_turn_id = (
            str(raw_turn_id).strip()
            if raw_turn_id is not None and str(raw_turn_id).strip()
            else None
        )
        connection.execute(
            """
            UPDATE cluster_agents
            SET last_input_tokens = ?,
                last_cached_input_tokens = ?,
                last_uncached_input_tokens = ?,
                total_input_tokens = ?,
                total_cached_input_tokens = ?,
                total_uncached_input_tokens = ?,
                context_tokens = ?,
                output_tokens = ?,
                model_context_window = COALESCE(?, model_context_window),
                turn_count = ?,
                last_turn_id = COALESCE(?, last_turn_id),
                native_usage_status = ?,
                native_usage_missing_fields = ?
            WHERE agent_id = ?
            """,
            (
                count("last_input_tokens"),
                count("last_cached_input_tokens"),
                count("last_uncached_input_tokens"),
                count("total_input_tokens"),
                count("total_cached_input_tokens"),
                count("total_uncached_input_tokens"),
                count("context_tokens"),
                count("output_tokens"),
                model_context_window,
                count("turn_count"),
                last_turn_id,
                "partial" if missing_fields else "available",
                json.dumps(missing_fields, separators=(",", ":")),
                agent_id,
            ),
        )

    def archive_stale_managed_agents(
        self,
        *,
        actor: str = "human",
        reason: str = "archive failed and non-reusable managed agents",
        apply: bool = False,
    ) -> dict[str, Any]:
        actor = self._require_text(actor, "actor")
        reason = self._require_text(reason, "reason")
        if len(reason) > 2000:
            raise AgentPoolError("reason must not exceed 2000 characters")
        now = utc_now()
        candidates: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        archived: list[str] = []
        with self._connection(write=apply) as connection:
            if apply:
                connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT a.*, p.archived_at AS project_archived_at
                FROM cluster_agents a
                JOIN projects p ON p.project_id = a.project_id
                WHERE a.runtime = 'managed' AND a.archived_at IS NULL
                ORDER BY a.updated_at, a.agent_id
                """
            ).fetchall()
            for row in rows:
                agent_id = str(row["agent_id"])
                contract = cache_profile_contract_status(row["profile"])
                reasons: list[str] = []
                if row["status"] == "failed":
                    reasons.append("failed")
                if row["status"] == "superseded":
                    reasons.append("superseded")
                if contract != "current":
                    reasons.append(f"profile_{contract}")
                if not reasons:
                    continue
                if row["status"] in {"starting", "busy"}:
                    skipped.append(
                        {
                            "agent_id": agent_id,
                            "reason": f"active_status:{row['status']}",
                        }
                    )
                    continue
                if row["project_archived_at"] is not None:
                    skipped.append(
                        {
                            "agent_id": agent_id,
                            "reason": "project_archived",
                        }
                    )
                    continue
                if any(
                    row[field] is not None
                    for field in (
                        "current_goal_id",
                        "current_task_id",
                        "current_job_id",
                    )
                ):
                    skipped.append(
                        {
                            "agent_id": agent_id,
                            "reason": "active_reference",
                        }
                    )
                    continue
                candidates.append(
                    {
                        "agent_id": agent_id,
                        "thread_id": str(row["thread_id"]),
                        "role": str(row["role"]),
                        "status": str(row["status"]),
                        "contract": contract,
                        "reasons": reasons,
                    }
                )
            if apply:
                for candidate in candidates:
                    agent_id = str(candidate["agent_id"])
                    updated = connection.execute(
                        """
                        UPDATE cluster_agents
                        SET archived_at = ?, updated_at = ?
                        WHERE agent_id = ? AND archived_at IS NULL
                        """,
                        (now, now, agent_id),
                    )
                    if updated.rowcount != 1:
                        continue
                    archived.append(agent_id)
                    self._event(
                        connection,
                        agent_id,
                        "archived_stale",
                        actor,
                        {
                            "reason": reason,
                            "reasons": candidate["reasons"],
                            "contract": candidate["contract"],
                        },
                        created_at=now,
                    )
        return {
            "apply": apply,
            "candidate_count": len(candidates),
            "candidates": candidates,
            "skipped": skipped,
            "archived_count": len(archived),
            "archived": archived,
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
        *,
        profile: str | None = None,
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
        profile = str(profile or CURRENT_AGENT_PROFILE).strip()
        if not profile:
            raise AgentPoolError("profile must not be empty")
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
                if existing["runtime"] != "managed":
                    raise AgentPoolError(
                        f"thread {thread_id} is already registered as native"
                    )
                identity = (
                    existing["project_id"],
                    existing["role"],
                    existing["workstream_key"],
                    existing["profile"],
                    existing["model"],
                    existing["model_provider"],
                )
                requested = (
                    project_id,
                    role,
                    workstream_key,
                    profile,
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
                    thread_id, profile, status, model, model_provider,
                    last_loaded_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'idle', ?, ?, ?, ?, ?)
                """,
                (
                    agent_id,
                    project_id,
                    role,
                    workstream_key,
                    display_name,
                    thread_id,
                    profile,
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
                    "profile": profile,
                    "model": model,
                    "model_provider": model_provider,
                },
                created_at=now,
            )
            return self._agent_dict(self._row(connection, agent_id))

    def register_native_thread(
        self,
        project_id: str,
        role: str,
        workstream_key: str,
        thread_id: str,
        actor: str,
        *,
        task_id: str | None = None,
        parent_thread_id: str | None = None,
        agent_path: str | None = None,
        agent_nickname: str | None = None,
        display_name: str | None = None,
        model: str = "",
        model_provider: str = "",
        prompt_hash: str = "",
        prompt_status: str = "unknown",
        native_session_path: str | None = None,
        native_session_mtime_ns: int = 0,
        native_session_size: int = 0,
        last_seen_at: str | None = None,
        usage: Mapping[str, Any] | None = None,
        identity_source: str = "unknown",
        identity_confidence: str = "unknown",
        lifecycle: str = "unknown",
        lifecycle_source: str = "unknown",
        assignment_source: str = "none",
        assignment_confidence: str = "unknown",
        task_candidates: list[str] | tuple[str, ...] | None = None,
        task_assignment_ambiguous: bool = False,
        session_stale: bool = True,
    ) -> dict[str, Any]:
        project_id = self._require_text(project_id, "project_id")
        role = self._require_text(role, "role").lower()
        if role not in self.ROLES and role != "unknown":
            raise AgentPoolError(f"unsupported role: {role}")
        workstream_key = self._require_text(workstream_key, "workstream_key")
        thread_id = self._require_text(thread_id, "thread_id")
        actor = self._require_text(actor, "actor")
        prompt_status = self._require_text(
            prompt_status,
            "prompt_status",
        ).lower()
        if prompt_status not in {"unknown", "delivered", "missing"}:
            raise AgentPoolError(
                "prompt_status must be unknown, delivered, or missing"
            )
        identity_source = self._enum_value(
            identity_source,
            "identity_source",
            IDENTITY_SOURCES,
        )
        identity_confidence = self._enum_value(
            identity_confidence,
            "identity_confidence",
            IDENTITY_CONFIDENCES,
        )
        lifecycle = self._enum_value(
            lifecycle,
            "lifecycle",
            NATIVE_LIFECYCLES,
        )
        lifecycle_source = self._require_text(
            lifecycle_source,
            "lifecycle_source",
        ).lower()
        assignment_source = self._enum_value(
            assignment_source,
            "assignment_source",
            ASSIGNMENT_SOURCES,
        )
        assignment_confidence = self._enum_value(
            assignment_confidence,
            "assignment_confidence",
            ASSIGNMENT_CONFIDENCES,
        )
        normalized_candidates = sorted(
            {
                str(value).strip().upper()
                for value in (task_candidates or [])
                if str(value).strip()
            }
        )
        if task_id:
            normalized_task = str(task_id).strip().upper()
            if normalized_task not in normalized_candidates:
                normalized_candidates.append(normalized_task)
                normalized_candidates.sort()
        if assignment_source == "none" and task_id:
            assignment_source = "registration"
            assignment_confidence = (
                "high"
                if assignment_confidence == "unknown"
                else assignment_confidence
            )
        candidates_json = json.dumps(
            normalized_candidates,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        model = self._normalize_model(model)
        model_provider = self._normalize_model(model_provider)
        native_status = (
            "loaded"
            if not session_stale
            and lifecycle in {"active", "idle", "unknown"}
            else "stored"
        )
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
            current_goal_id: str | None = None
            if task_id:
                task = connection.execute(
                    """
                    SELECT project_id, goal_id, archived_at
                    FROM tasks WHERE task_id = ?
                    """,
                    (task_id,),
                ).fetchone()
                if task is None or task["project_id"] != project_id:
                    raise AgentPoolError("task does not belong to this project")
                if task["archived_at"] is not None:
                    raise AgentPoolError(
                        "archived task cannot bind a native thread"
                    )
                current_goal_id = task["goal_id"]
            actor_conflict = connection.execute(
                """
                SELECT thread_id
                FROM cluster_agents
                WHERE project_id = ? AND actor = ? AND runtime = 'native'
                  AND thread_id != ?
                LIMIT 1
                """,
                (project_id, actor, thread_id),
            ).fetchone()
            if actor_conflict is not None:
                raise AgentPoolError(
                    f"native actor {actor} is already bound to thread {actor_conflict['thread_id']}"
                )
            existing = connection.execute(
                "SELECT * FROM cluster_agents WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            if existing is not None:
                if existing["runtime"] != "native":
                    raise AgentPoolError(
                        f"thread {thread_id} is already registered as managed"
                    )
                if existing["project_id"] != project_id:
                    raise AgentPoolError(
                        "native thread cannot move to a different project; "
                        "project ownership is immutable"
                    )
                if existing["workstream_key"] != workstream_key:
                    raise AgentPoolError(
                        "native thread workstream identity is immutable"
                    )
                if not (
                    existing["role"] == role
                    or (
                        existing["role"] == "unknown"
                        and role in self.ROLES
                    )
                    or (
                        role == "unknown"
                        and existing["role"] in self.ROLES
                    )
                ):
                    raise AgentPoolError(
                        f"thread {thread_id} is registered with a different "
                        "native role"
                    )
                effective_role = (
                    existing["role"] if role == "unknown" else role
                )
                if (
                    existing["current_task_id"]
                    and task_id
                    and existing["current_task_id"] != task_id
                ):
                    raise AgentPoolError(
                        "native thread is same_task_only and cannot switch tasks"
                    )
                connection.execute(
                    """
                    UPDATE cluster_agents
                    SET role = ?, display_name = ?, model = ?, model_provider = ?,
                        current_task_id = COALESCE(?, current_task_id),
                        current_goal_id = COALESCE(?, current_goal_id),
                        parent_thread_id = COALESCE(?, parent_thread_id),
                        agent_path = COALESCE(?, agent_path),
                        agent_nickname = COALESCE(?, agent_nickname),
                        actor = ?, prompt_hash = ?,
                        prompt_status = ?,
                        native_session_path = COALESCE(?, native_session_path),
                        native_session_mtime_ns = ?,
                        native_session_size = ?,
                        last_seen_at = COALESCE(?, last_seen_at),
                        last_used_at = COALESCE(?, last_used_at),
                        updated_at = ?,
                        identity_source = ?, identity_confidence = ?,
                        lifecycle = ?, lifecycle_source = ?,
                        assignment_source = ?, assignment_confidence = ?,
                        task_candidates_json = ?,
                        task_assignment_ambiguous = ?, session_stale = ?,
                        status = CASE
                            WHEN status IN ('loaded', 'stored', 'idle')
                            THEN ?
                            ELSE status
                        END
                    WHERE agent_id = ?
                    """,
                    (
                        effective_role,
                        display_name
                        or existing["display_name"]
                        or agent_nickname
                        or agent_path
                        or thread_id,
                        model or existing["model"],
                        model_provider or existing["model_provider"],
                        task_id,
                        current_goal_id,
                        parent_thread_id,
                        agent_path,
                        agent_nickname,
                        actor,
                        prompt_hash,
                        prompt_status,
                        native_session_path,
                        int(native_session_mtime_ns or 0),
                        int(native_session_size or 0),
                        last_seen_at,
                        last_seen_at,
                        now,
                        identity_source,
                        identity_confidence,
                        lifecycle,
                        lifecycle_source,
                        assignment_source,
                        assignment_confidence,
                        candidates_json,
                        int(bool(task_assignment_ambiguous)),
                        int(bool(session_stale)),
                        native_status,
                        existing["agent_id"],
                    ),
                )
                self._update_native_usage(
                    connection,
                    str(existing["agent_id"]),
                    usage,
                )
                self._event(
                    connection,
                    existing["agent_id"],
                    "native_refreshed",
                    actor,
                    {
                        "thread_id": thread_id,
                        "task_id": task_id,
                        "prompt_status": prompt_status,
                        "session_path": native_session_path,
                        "session_mtime_ns": int(
                            native_session_mtime_ns or 0
                        ),
                        "session_size": int(native_session_size or 0),
                        "identity_source": identity_source,
                        "identity_confidence": identity_confidence,
                        "lifecycle": lifecycle,
                        "session_stale": bool(session_stale),
                        "assignment_source": assignment_source,
                        "task_candidates": normalized_candidates,
                        "task_assignment_ambiguous": bool(
                            task_assignment_ambiguous
                        ),
                        "usage_available": bool(
                            (usage or {}).get("available")
                        ),
                    },
                    created_at=now,
                )
                return self._agent_dict(
                    self._row(connection, existing["agent_id"])
                )
            agent_id = f"A-{uuid.uuid4().hex[:12].upper()}"
            connection.execute(
                """
                INSERT INTO cluster_agents(
                    agent_id, project_id, role, workstream_key, display_name,
                    thread_id, runtime, parent_thread_id, agent_path,
                    agent_nickname, actor, prompt_hash, prompt_status,
                    native_session_path, native_session_mtime_ns,
                    native_session_size, last_seen_at, identity_source,
                    identity_confidence, lifecycle, lifecycle_source,
                    assignment_source, assignment_confidence,
                    task_candidates_json, task_assignment_ambiguous,
                    session_stale, profile, status,
                    model, model_provider, current_goal_id, current_task_id,
                    last_loaded_at, last_used_at, created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, 'native', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    'native', ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    agent_id,
                    project_id,
                    role,
                    workstream_key,
                    display_name
                    or agent_nickname
                    or agent_path
                    or thread_id,
                    thread_id,
                    parent_thread_id,
                    agent_path,
                    agent_nickname,
                    actor,
                    prompt_hash,
                    prompt_status,
                    native_session_path,
                    int(native_session_mtime_ns or 0),
                    int(native_session_size or 0),
                    last_seen_at or now,
                    identity_source,
                    identity_confidence,
                    lifecycle,
                    lifecycle_source,
                    assignment_source,
                    assignment_confidence,
                    candidates_json,
                    int(bool(task_assignment_ambiguous)),
                    int(bool(session_stale)),
                    native_status,
                    model,
                    model_provider,
                    current_goal_id,
                    task_id,
                    now,
                    last_seen_at or now,
                    now,
                    now,
                ),
            )
            self._update_native_usage(
                connection,
                agent_id,
                usage,
            )
            self._event(
                connection,
                agent_id,
                "native_registered",
                actor,
                {
                    "project_id": project_id,
                    "role": role,
                    "workstream_key": workstream_key,
                    "thread_id": thread_id,
                    "task_id": task_id,
                    "parent_thread_id": parent_thread_id,
                    "agent_path": agent_path,
                    "agent_nickname": agent_nickname,
                    "prompt_status": prompt_status,
                    "session_path": native_session_path,
                    "session_mtime_ns": int(
                        native_session_mtime_ns or 0
                    ),
                    "session_size": int(native_session_size or 0),
                    "identity_source": identity_source,
                    "identity_confidence": identity_confidence,
                    "lifecycle": lifecycle,
                    "session_stale": bool(session_stale),
                    "assignment_source": assignment_source,
                    "task_candidates": normalized_candidates,
                    "task_assignment_ambiguous": bool(
                        task_assignment_ambiguous
                    ),
                    "usage_available": bool(
                        (usage or {}).get("available")
                    ),
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
        if role == "lead":
            return self._goal_workstream_is_continuous(
                connection,
                project_id,
                workstream_key,
            )
        if role == "team_explorer" and task_kind == "read":
            return True
        if role == "team_reviewer" and task_kind == "review":
            return True
        return False

    @staticmethod
    def _context_reuse_ratio() -> float:
        raw = os.environ.get(
            "CODEX_TEAM_AGENT_REUSE_CONTEXT_RATIO",
            "0.85",
        ).strip()
        try:
            value = float(raw)
        except ValueError:
            value = 0.85
        return min(1.0, max(0.1, value))

    def select_agent(
        self,
        project_id: str,
        role: str,
        workstream_key: str,
        continuity_mode: str,
        task_kind: str,
        model: str,
        model_provider: str,
        *,
        profile: str | None = None,
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
        expected_profile = str(profile or CURRENT_AGENT_PROFILE).strip()
        if not expected_profile:
            raise AgentPoolError("profile must not be empty")
        context_reuse_ratio = self._context_reuse_ratio()
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
                  AND profile = ? AND model = ? AND model_provider = ?
                  AND runtime = 'managed'
                  AND status IN ({", ".join("?" for _ in self.IDLE_STATUSES)})
                  AND archived_at IS NULL
                  AND reset_requested_at IS NULL
                  AND (
                    turn_count = 0
                    OR (
                        model_context_window IS NOT NULL
                        AND model_context_window > 0
                        AND context_tokens <= model_context_window * ?
                    )
                  )
                ORDER BY
                    CASE WHEN last_used_at IS NULL THEN 1 ELSE 0 END,
                    last_used_at DESC,
                    created_at ASC,
                    agent_id ASC
                LIMIT 1
                """,
                (
                    project_id,
                    role,
                    workstream_key,
                    expected_profile,
                    model,
                    model_provider,
                    *self.IDLE_STATUSES,
                    context_reuse_ratio,
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
                    "profile": expected_profile,
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
        values = usage or {}
        context_known = self._usage_field_is_present(
            values,
            "context",
            "context_tokens",
            "contextTokens",
        )
        window_known = self._usage_field_is_present(
            values,
            "model_context_window",
            "modelContextWindow",
        )
        if not (context_known and window_known):
            normalized["model_context_window"] = None
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
                    model_context_window = ?,
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
        if len(reason) > 2000:
            raise AgentPoolError("reset reason must not exceed 2000 characters")
        now = utc_now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            agent = self._row(connection, agent_id)
            if agent["archived_at"] is not None:
                raise AgentPoolError("archived agents cannot be reset")
            if agent["runtime"] != "managed":
                raise AgentPoolError(
                    "native threads are same_task_only and cannot be reset by the managed runtime"
                )
            if agent["status"] in {"starting", "busy"}:
                raise AgentPoolError("busy agents cannot be reset")
            if agent["status"] == "superseded":
                raise AgentPoolError("superseded agents cannot be reset")
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
                {"reason": reason, "requested_at": now},
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
                if archived:
                    if agent["archived_at"] is not None:
                        continue
                    event_type = "archived_project"
                else:
                    if agent["archived_at"] is None:
                        continue
                    latest_event = connection.execute(
                        """
                        SELECT event_type, actor
                        FROM cluster_agent_events
                        WHERE agent_id = ?
                          AND event_type IN (
                              'archived_project', 'archived',
                              'archived_stale', 'restored'
                          )
                        ORDER BY event_id DESC
                        LIMIT 1
                        """,
                        (agent["agent_id"],),
                    ).fetchone()
                    if (
                        latest_event is None
                        or latest_event["actor"] != "agent_pool"
                        or latest_event["event_type"]
                        not in {"archived_project", "archived"}
                    ):
                        continue
                    event_type = "restored"
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
                    event_type,
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
