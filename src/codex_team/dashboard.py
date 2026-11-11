from __future__ import annotations

import json
import hashlib
import ipaddress
import re
import threading
import webbrowser
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from . import __version__
from .cluster import (
    ClusterError,
    ClusterManager,
    daemon_identity,
    daemon_status,
    ensure_daemon,
)
from .config import Settings
from .db import effective_schema_version, utc_now
from .service import TeamError, TeamService

WEB_ROOT = Path(__file__).resolve().parent / "web"
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
CACHE_TIME_BUCKET_SECONDS = 30
TIME_DERIVED_FIELDS = {
    "claim_wait_seconds",
    "estimated_wait_seconds",
    "heartbeat_age_seconds",
    "oldest_claim_wait_seconds",
    "oldest_heartbeat_age_seconds",
}
TIME_DERIVED_STATE_FIELDS = {
    "attention_count",
    "claim_wait_state",
    "fairness_wait_count",
    "health",
    "heartbeat_overdue",
    "heartbeat_state",
    "queue_reason",
    "queued_goals",
}
TIME_DERIVED_ID_FIELDS = {
    "agent_id",
    "goal_id",
    "job_id",
    "task_id",
}
TIME_DERIVED_CONTAINERS = {
    "cluster_goals",
    "cluster_summary",
    "items",
    "tasks",
}
_ETAG_TOKEN = re.compile(r'\*|(?:W/)?"(?:[^"\\]|\\.)*"')


def _query_flag(query: dict[str, list[str]], key: str) -> bool:
    return query.get(key, ["0"])[0].lower() in {"1", "true"}


def _query_int(
    query: dict[str, list[str]],
    key: str,
    *,
    default: int | None = None,
    minimum: int = 1,
    maximum: int = 500,
) -> int | None:
    raw = query.get(key, [""])[0].strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(
            f"{key} must be between {minimum} and {maximum}"
        )
    return value


def _etag_matches(header: str, etag: str) -> bool:
    def normalized(value: str) -> str:
        value = value.strip()
        return value[2:].strip() if value.startswith("W/") else value

    target = normalized(etag)
    for candidate in _ETAG_TOKEN.findall(str(header or "")):
        if candidate == "*" or normalized(candidate) == target:
            return True
    return False


def _time_bucket(value: object) -> int | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return int(parsed.timestamp() // CACHE_TIME_BUCKET_SECONDS)
    except (TypeError, ValueError, OverflowError):
        return None


def _has_time_derived_fields(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            key in TIME_DERIVED_FIELDS
            or _has_time_derived_fields(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_has_time_derived_fields(item) for item in value)
    return False


def _time_derived_context(value: object) -> object:
    if isinstance(value, list):
        return [_time_derived_context(item) for item in value]
    if not isinstance(value, dict):
        return None
    context: dict[str, object] = {}
    for key in TIME_DERIVED_ID_FIELDS:
        if value.get(key) is not None:
            context[key] = str(value[key])
    for key in TIME_DERIVED_STATE_FIELDS:
        if key in value:
            context[key] = value[key]
    for key in TIME_DERIVED_CONTAINERS:
        if key in value:
            context[key] = _time_derived_context(value[key])
    return context


def _validator_context(
    context: dict[str, object],
    value: object,
    *,
    time_aware: bool,
) -> dict[str, object]:
    result = dict(context)
    if time_aware:
        result["derived"] = _time_derived_context(value)
    return result


def _etag_for_revision(revision: str, context: object) -> str:
    return f'W/"{_contextual_revision(str(revision), context)}"'


def _etag_for_content(value: object, context: object) -> str:
    digest = hashlib.sha256(
        json.dumps(
            {"context": context, "value": value},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:16]
    return f'W/"v2:content-{digest}"'


def _contextual_revision(base: str, context: object) -> str:
    digest = hashlib.sha256(
        json.dumps(
            {"base": base, "context": context},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:16]
    return f"{base}:api-{digest}"


def _dashboard_context(
    project_id: str | None,
    include_archived: bool,
    runtime: dict[str, object],
) -> dict[str, object]:
    tick = runtime.get("tick")
    stable_tick: dict[str, object] = {}
    if isinstance(tick, dict):
        failed_phases = tick.get("failed_phases")
        if tick.get("degraded") or tick.get("error_code") or failed_phases:
            stable_tick = {
                "degraded": bool(tick.get("degraded")),
                "failed_phases": (
                    [str(item) for item in failed_phases]
                    if isinstance(failed_phases, (list, tuple))
                    else []
                ),
                "error_code": str(tick.get("error_code") or ""),
            }
    return {
        "scope": "dashboard",
        "project_id": project_id or "",
        "include_archived": bool(include_archived),
        "cluster_runtime": {
            "runtime": runtime.get("runtime"),
            "thread_reuse": bool(runtime.get("thread_reuse")),
            "running": bool(runtime.get("running")),
            "config_mismatch": bool(runtime.get("config_mismatch")),
            "version": runtime.get("version"),
            **({"tick": stable_tick} if stable_tick else {}),
        },
    }


def _authority_parts(value: str) -> tuple[str, int | None]:
    parsed = urlsplit(f"//{value}")
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("invalid Host header")
    return parsed.hostname.rstrip(".").lower(), parsed.port


def _is_loopback_host(hostname: str) -> bool:
    if hostname.lower().rstrip(".") == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _origin_matches_authority(origin: str, authority: str) -> bool:
    parsed = urlsplit(origin)
    if (
        parsed.scheme.lower() != "http"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return False
    host, port = _authority_parts(authority)
    return (
        parsed.hostname.rstrip(".").lower() == host
        and (parsed.port or 80) == (port or 80)
    )


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "CodexTeamBoard/" + __version__
    team_service: TeamService

    def _cluster_runtime(self) -> dict[str, object]:
        status = daemon_status(self.team_service.settings)
        state = status.get("state")
        state = state if isinstance(state, dict) else {}
        state_identity = state.get("identity") if isinstance(state, dict) else None
        expected = status.get("expected_identity")
        identity = (
            state_identity
            if isinstance(state_identity, dict)
            else expected
            if isinstance(expected, dict)
            else daemon_identity(self.team_service.settings)
        )
        runtime = str(identity.get("runtime") or "app_server")
        result: dict[str, object] = {
            "runtime": runtime,
            "thread_reuse": runtime == "app_server",
            "running": bool(status.get("running")),
            "config_mismatch": bool(status.get("config_mismatch")),
            "version": identity.get("version"),
        }
        if state:
            def nonnegative_int(value: object) -> int:
                try:
                    return max(0, int(value or 0))
                except (TypeError, ValueError):
                    return 0

            def nonnegative_float(value: object) -> float:
                try:
                    return max(0.0, float(value or 0))
                except (TypeError, ValueError):
                    return 0.0

            def string_list(value: object) -> list[str]:
                if not isinstance(value, (list, tuple)):
                    return []
                return [str(item) for item in value if str(item)]

            raw_phases = state.get("phases")
            if not isinstance(raw_phases, (list, tuple)):
                raw_phases = []
            phases = [
                {
                    "name": str(phase.get("name") or ""),
                    "ok": bool(phase.get("ok")),
                    "degraded": bool(phase.get("degraded")),
                    "attempts": nonnegative_int(phase.get("attempts")),
                    "duration_ms": nonnegative_float(
                        phase.get("duration_ms")
                    ),
                    "error_code": str(phase.get("error_code") or ""),
                    "redacted_error": str(
                        phase.get("redacted_error") or ""
                    ),
                    "retry_suppressed": bool(
                        phase.get("retry_suppressed")
                    ),
                }
                for phase in raw_phases
                if isinstance(phase, dict)
            ]
            failed_phases = string_list(state.get("failed_phases"))
            if not failed_phases and state.get("failed_phase"):
                failed_phases = [str(state["failed_phase"])]
            degraded_phases = (
                string_list(state.get("degraded_phases"))
                or failed_phases
            )
            skipped_phases = string_list(state.get("skipped_phases"))
            retry = state.get("retry")
            if not isinstance(retry, dict):
                retry = {
                    "limit": nonnegative_int(state.get("retry_budget")),
                    "used": nonnegative_int(state.get("retry_used")),
                    "exhausted": False,
                }
            else:
                retry = {
                    "limit": nonnegative_int(retry.get("limit")),
                    "used": nonnegative_int(retry.get("used")),
                    "exhausted": bool(retry.get("exhausted")),
                }
            health = state.get("health")
            if not isinstance(health, dict):
                health = {
                    "ok": bool(state.get("ok")),
                    "degraded": bool(degraded_phases),
                    "failed_phases": failed_phases,
                    "degraded_phases": degraded_phases,
                    "skipped_phases": skipped_phases,
                    "errors": state.get("errors") or [],
                }
            result["tick"] = {
                "tick_id": str(state.get("tick_id") or ""),
                "tick_at": str(state.get("tick_at") or ""),
                "ok": bool(state.get("ok")),
                "degraded": bool(
                    state.get("degraded")
                    or degraded_phases
                    or failed_phases
                ),
                "failed_phase": (
                    failed_phases[0] if failed_phases else None
                ),
                "failed_phases": failed_phases,
                "degraded_phases": degraded_phases,
                "skipped_phases": skipped_phases,
                "error_code": str(state.get("error_code") or ""),
                "redacted_error": str(state.get("error") or ""),
                "phases": phases,
                "retry": retry,
                "health": health,
            }
        return result

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        if parsed.path.startswith("/api/"):
            rejection = self._loopback_host_rejection()
            if rejection is not None:
                status, message = rejection
                self._send_json({"error": message}, status=status)
                return
        if parsed.path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            return
        if parsed.path in STATIC_FILES:
            self._serve_static(parsed.path)
            return
        if parsed.path == "/api/health":
            database = self.team_service.settings.db_path.resolve()
            cluster_runtime = self._cluster_runtime()
            with self.team_service.connection(write=False) as connection:
                schema_version = effective_schema_version(connection)
            self._send_json(
                {
                    "ok": True,
                    "service": "codex-team-board",
                    "version": __version__,
                    "team_home": str(
                        self.team_service.settings.home.resolve()
                    ),
                    "database": str(database),
                    "database_id": hashlib.sha256(
                        str(database).casefold().encode("utf-8")
                    ).hexdigest()[:16],
                    "schema_version": schema_version,
                    "cluster_runtime": cluster_runtime,
                    "generated_at": utc_now(),
                }
            )
            return
        if parsed.path == "/api/projects":
            query = parse_qs(parsed.query)
            include_archived = _query_flag(query, "include_archived")
            data = self.team_service.dashboard_summary(
                include_archived=include_archived,
            )
            revision = str(data["revision"])
            context = {
                "scope": "projects",
                "include_archived": include_archived,
            }
            data["revision"] = _contextual_revision(revision, context)
            self._send_revisable_json(
                {
                    "projects": data["projects"],
                    "revision": data["revision"],
                    "generated_at": data["generated_at"],
                },
                etag=_etag_for_revision(revision, context),
            )
            return
        if parsed.path == "/api/summary":
            query = parse_qs(parsed.query)
            project_id = query.get("project_id", [""])[0] or None
            include_archived = _query_flag(query, "include_archived")
            data = self.team_service.dashboard_summary(
                project_id,
                include_archived=include_archived,
            )
            runtime = self._cluster_runtime()
            data["cluster_runtime"] = runtime
            revision = str(data["revision"])
            context = _dashboard_context(
                project_id,
                include_archived,
                runtime,
            )
            time_aware = _has_time_derived_fields(data)
            if time_aware:
                context["time_bucket"] = _time_bucket(data["generated_at"])
            data["revision"] = _contextual_revision(revision, context)
            self._send_revisable_json(
                data,
                etag=_etag_for_revision(
                    revision,
                    _validator_context(
                        context,
                        data,
                        time_aware=time_aware,
                    ),
                ),
            )
            return
        if parsed.path == "/api/board":
            query = parse_qs(parsed.query)
            project_id = query.get("project_id", [""])[0] or None
            include_archived = _query_flag(query, "include_archived")
            data = self.team_service.dashboard_snapshot(
                project_id,
                include_archived=include_archived,
            )
            runtime = self._cluster_runtime()
            data["cluster_runtime"] = runtime
            revision = str(data["revision"])
            context = _dashboard_context(
                project_id,
                include_archived,
                runtime,
            )
            time_aware = _has_time_derived_fields(data)
            if time_aware:
                context["time_bucket"] = _time_bucket(data["generated_at"])
            data["revision"] = _contextual_revision(revision, context)
            self._send_revisable_json(
                data,
                etag=_etag_for_revision(
                    revision,
                    _validator_context(
                        context,
                        data,
                        time_aware=time_aware,
                    ),
                ),
            )
            return
        partitions = {
            "/api/tasks": "tasks",
            "/api/goals": "goals",
            "/api/agents": "agents",
            "/api/attempts": "attempts",
        }
        if parsed.path in partitions:
            try:
                query = parse_qs(parsed.query)
                project_id = query.get("project_id", [""])[0] or None
                include_archived = _query_flag(query, "include_archived")
                limit = _query_int(query, "limit", default=100)
                cursor = query.get("cursor", [""])[0].strip() or None
                data = self.team_service.dashboard_partition(
                    partitions[parsed.path],
                    project_id,
                    include_archived=include_archived,
                    limit=int(limit or 100),
                    cursor=cursor,
                )
                data["partition"] = partitions[parsed.path]
                revision = str(data["revision"])
                context: dict[str, object] = {
                    "scope": "partition",
                    "partition": partitions[parsed.path],
                    "project_id": project_id or "",
                    "include_archived": include_archived,
                    "limit": int(limit or 100),
                    "cursor": cursor or "",
                }
                time_aware = _has_time_derived_fields(data)
                if time_aware:
                    context["time_bucket"] = _time_bucket(
                        data["generated_at"]
                    )
                data["revision"] = _contextual_revision(revision, context)
                self._send_revisable_json(
                    data,
                    etag=_etag_for_revision(
                        revision,
                        _validator_context(
                            context,
                            data,
                            time_aware=time_aware,
                        ),
                    ),
                )
            except (TeamError, ValueError) as exc:
                self._send_json(
                    {"error": str(exc)},
                    status=HTTPStatus.BAD_REQUEST,
                )
            return
        if parsed.path == "/api/events":
            try:
                query = parse_qs(parsed.query)
                project_id = query.get("project_id", [""])[0] or None
                include_archived = _query_flag(query, "include_archived")
                limit = _query_int(query, "limit", default=100)
                before_event_id = _query_int(
                    query,
                    "cursor",
                    default=None,
                )
                data = self.team_service.dashboard_events(
                    project_id,
                    include_archived=include_archived,
                    limit=int(limit or 100),
                    before_event_id=before_event_id,
                )
                revision = str(data["revision"])
                context = {
                    "scope": "events",
                    "project_id": project_id or "",
                    "include_archived": include_archived,
                    "limit": int(limit or 100),
                    "cursor": before_event_id,
                }
                data["revision"] = _contextual_revision(revision, context)
                self._send_revisable_json(
                    data,
                    etag=_etag_for_revision(revision, context),
                )
            except ValueError as exc:
                self._send_json(
                    {"error": str(exc)},
                    status=HTTPStatus.BAD_REQUEST,
                )
            return
        task_events_prefix = "/api/tasks/"
        task_events_suffix = "/events"
        if (
            parsed.path.startswith(task_events_prefix)
            and parsed.path.endswith(task_events_suffix)
        ):
            try:
                query = parse_qs(parsed.query)
                limit = _query_int(query, "limit", default=100)
                before_event_id = _query_int(
                    query,
                    "cursor",
                    default=None,
                )
                encoded_task_id = parsed.path[
                    len(task_events_prefix) : -len(task_events_suffix)
                ]
                task_id = unquote(encoded_task_id).strip().upper()
                if not task_id:
                    raise ValueError("task_id is required")
                data = self.team_service.get_task_events(
                    task_id,
                    limit=int(limit or 100),
                    before_event_id=before_event_id,
                )
                context = {
                    "scope": "task-events",
                    "task_id": task_id,
                    "limit": int(limit or 100),
                    "cursor": before_event_id,
                }
                self._send_revisable_json(
                    data,
                    etag=_etag_for_content(data, context),
                )
            except TeamError as exc:
                status = (
                    HTTPStatus.NOT_FOUND
                    if str(exc).startswith("task not found:")
                    else HTTPStatus.BAD_REQUEST
                )
                self._send_json({"error": str(exc)}, status=status)
            except ValueError as exc:
                self._send_json(
                    {"error": str(exc)},
                    status=HTTPStatus.BAD_REQUEST,
                )
            return
        self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        rejection = self._write_request_rejection()
        if rejection is not None:
            status, message = rejection
            self._send_json({"error": message}, status=status)
            return
        parsed = urlsplit(self.path)
        if parsed.path == "/api/clusters":
            self._create_cluster_goal()
            return
        if parsed.path == "/api/clusters/control":
            self._control_cluster_goal()
            return
        if parsed.path == "/api/clusters/archive":
            self._archive_cluster_goal()
            return
        if parsed.path == "/api/agents/reset":
            self._reset_agent()
            return
        if parsed.path == "/api/agents/sync-native":
            self._sync_native_agents()
            return
        if parsed.path not in {"/api/projects/archive", "/api/tasks/archive"}:
            self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
            return
        try:
            payload = self._read_json()
            archived = payload.get("archived")
            if not isinstance(archived, bool):
                raise ValueError("archived must be a boolean")
            if parsed.path == "/api/projects/archive":
                project_id = str(payload.get("project_id") or "").strip()
                if not project_id:
                    raise ValueError("project_id is required")
                project = self.team_service.set_project_archived(
                    project_id,
                    archived,
                )
                self._send_json({"project": project})
            else:
                task_id = str(payload.get("task_id") or "").strip().upper()
                if not task_id:
                    raise ValueError("task_id is required")
                task = self.team_service.set_task_archived(
                    task_id,
                    archived,
                )
                self._send_json({"task": task})
        except TeamError as exc:
            self._send_json(
                {"error": str(exc)},
                status=HTTPStatus.CONFLICT,
            )
        except (OSError, ValueError) as exc:
            self._send_json(
                {"error": str(exc)},
                status=HTTPStatus.BAD_REQUEST,
            )

    def _loopback_host_rejection(
        self,
        *,
        request_kind: str = "API",
    ) -> tuple[HTTPStatus, str] | None:
        authority = str(self.headers.get("Host") or "").strip()
        try:
            hostname, _ = _authority_parts(authority)
        except ValueError:
            return (
                HTTPStatus.FORBIDDEN,
                f"{request_kind} requests require a valid loopback Host header",
            )
        if not _is_loopback_host(hostname):
            return (
                HTTPStatus.FORBIDDEN,
                f"{request_kind} requests require a loopback Host header",
            )
        return None

    def _write_request_rejection(
        self,
    ) -> tuple[HTTPStatus, str] | None:
        rejection = self._loopback_host_rejection(request_kind="write")
        if rejection is not None:
            return rejection
        authority = str(self.headers.get("Host") or "").strip()
        content_type = str(
            self.headers.get("Content-Type") or ""
        ).split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            return (
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "write requests require Content-Type application/json",
            )
        origin = str(self.headers.get("Origin") or "").strip()
        if origin and not _origin_matches_authority(origin, authority):
            return (
                HTTPStatus.FORBIDDEN,
                "write request Origin is not same-origin with Host",
            )
        return None

    def _reset_agent(self) -> None:
        try:
            payload = self._read_json()
            agent_id = self._required_text(payload, "agent_id")
            reason = payload.get(
                "reason",
                "manual reset requested through dashboard",
            )
            if not isinstance(reason, str):
                raise ValueError("reason must be a string")
            if len(reason.strip()) > 2000:
                raise ValueError("reason must not exceed 2000 characters")
            agent = self.team_service.reset_agent(
                agent_id,
                actor="human",
                reason=reason,
            )
            self._send_json({"agent": agent})
        except TeamError as exc:
            self._send_json(
                {"error": str(exc)},
                status=HTTPStatus.CONFLICT,
            )
        except (OSError, ValueError) as exc:
            self._send_json(
                {"error": str(exc)},
                status=HTTPStatus.BAD_REQUEST,
            )

    def _sync_native_agents(self) -> None:
        try:
            payload = self._read_json()
            project_id = payload.get("project_id")
            if project_id is not None and not isinstance(project_id, str):
                raise ValueError("project_id must be a string")
            limit = self._optional_integer(
                payload,
                "limit",
                default=200,
                minimum=1,
                maximum=1000,
            )
            cluster = ClusterManager(
                self.team_service.settings,
                team=self.team_service,
            )
            result = cluster.sync_native_agents(
                actor="dashboard",
                project_id=str(project_id).strip() if project_id else None,
                limit=limit,
            )
            self._send_json(result)
        except (ClusterError, TeamError) as exc:
            self._send_json(
                {"error": str(exc)},
                status=HTTPStatus.CONFLICT,
            )
        except (OSError, ValueError) as exc:
            self._send_json(
                {"error": str(exc)},
                status=HTTPStatus.BAD_REQUEST,
            )

    def _create_cluster_goal(self) -> None:
        try:
            payload = self._read_json()
            project_id = self._required_text(payload, "project_id")
            objective = self._required_text(payload, "objective")
            autonomy = payload.get("autonomy", "auto")
            if not isinstance(autonomy, str) or autonomy not in {
                "auto",
                "supervised",
            }:
                raise ValueError("autonomy must be auto or supervised")
            max_worker_attempts = self._optional_integer(
                payload,
                "max_worker_attempts",
                default=3,
                minimum=1,
                maximum=10,
            )
            max_lead_attempts = self._optional_integer(
                payload,
                "max_lead_attempts",
                default=5,
                minimum=1,
                maximum=20,
            )
            project = self.team_service.get_project(project_id)
            if project.get("archived_at"):
                raise TeamError("project is archived; restore it before making changes")
            cluster = ClusterManager(
                self.team_service.settings,
                team=self.team_service,
            )
            goal = cluster.create_goal(
                project["root_path"],
                objective=objective,
                autonomy=autonomy,
                max_worker_attempts=max_worker_attempts,
                max_lead_attempts=max_lead_attempts,
            )
            try:
                daemon = ensure_daemon(self.team_service.settings)
            except Exception as exc:
                try:
                    cluster.set_goal_status(
                        goal["goal_id"],
                        "failed",
                        reason=f"daemon start failed: {exc}",
                    )
                finally:
                    raise ClusterError(
                        f"goal created but cluster daemon failed to start: {exc}"
                    ) from exc
            self._send_json(
                {
                    "goal": goal,
                    "daemon": daemon,
                }
            )
        except TeamError as exc:
            self._send_json(
                {"error": str(exc)},
                status=HTTPStatus.CONFLICT,
            )
        except (OSError, ValueError) as exc:
            self._send_json(
                {"error": str(exc)},
                status=HTTPStatus.BAD_REQUEST,
            )

    def _control_cluster_goal(self) -> None:
        try:
            payload = self._read_json()
            goal_id = self._required_text(payload, "goal_id")
            action = payload.get("action")
            if not isinstance(action, str) or action not in {
                "pause",
                "resume",
                "cancel",
            }:
                raise ValueError("action must be pause, resume, or cancel")
            cluster = ClusterManager(
                self.team_service.settings,
                team=self.team_service,
            )
            if action == "cancel":
                goal = cluster.cancel_goal(
                    goal_id,
                    reason="cancel requested through dashboard",
                )
                daemon = daemon_status(self.team_service.settings)
            elif action == "resume":
                try:
                    daemon = ensure_daemon(self.team_service.settings)
                except Exception as exc:
                    raise ClusterError(
                        f"cluster daemon failed to start: {exc}"
                    ) from exc
                goal = cluster.set_goal_status(
                    goal_id,
                    "active",
                    reason="resume requested through dashboard",
                )
            else:
                goal = cluster.set_goal_status(
                    goal_id,
                    "paused",
                    reason="pause requested through dashboard",
                )
                daemon = daemon_status(self.team_service.settings)
            self._send_json({"goal": goal, "daemon": daemon})
        except TeamError as exc:
            self._send_json(
                {"error": str(exc)},
                status=HTTPStatus.CONFLICT,
            )
        except (OSError, ValueError) as exc:
            self._send_json(
                {"error": str(exc)},
                status=HTTPStatus.BAD_REQUEST,
            )

    def _archive_cluster_goal(self) -> None:
        try:
            payload = self._read_json()
            goal_id = self._required_text(payload, "goal_id")
            archived = payload.get("archived")
            if not isinstance(archived, bool):
                raise ValueError("archived must be a boolean")
            cluster = ClusterManager(
                self.team_service.settings,
                team=self.team_service,
            )
            goal = cluster.set_goal_archived(
                goal_id,
                archived,
                actor="human",
            )
            self._send_json({"goal": goal})
        except TeamError as exc:
            self._send_json(
                {"error": str(exc)},
                status=HTTPStatus.CONFLICT,
            )
        except (OSError, ValueError) as exc:
            self._send_json(
                {"error": str(exc)},
                status=HTTPStatus.BAD_REQUEST,
            )

    @staticmethod
    def _required_text(payload: dict[str, object], key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} is required")
        return value.strip()

    @staticmethod
    def _optional_integer(
        payload: dict[str, object],
        key: str,
        *,
        default: int,
        minimum: int,
        maximum: int,
    ) -> int:
        value = payload.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{key} must be an integer")
        if not minimum <= value <= maximum:
            raise ValueError(f"{key} must be between {minimum} and {maximum}")
        return value

    def _read_json(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 65536:
            raise ValueError("request body must be between 1 and 65536 bytes")
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def _serve_static(self, request_path: str) -> None:
        filename, content_type = STATIC_FILES[request_path]
        path = WEB_ROOT / filename
        try:
            content = path.read_bytes()
        except OSError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'")
        self.end_headers()
        self.wfile.write(content)

    def _send_revisable_json(
        self,
        value: dict[str, object],
        *,
        etag: str | None = None,
    ) -> None:
        etag = etag or f'W/"{value["revision"]}"'
        if _etag_matches(str(self.headers.get("If-None-Match") or ""), etag):
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            return
        self._send_json(value, headers={"ETag": etag})

    def _send_json(
        self,
        value: object,
        *,
        status: HTTPStatus = HTTPStatus.OK,
        headers: dict[str, str] | None = None,
    ) -> None:
        content = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, header_value in (headers or {}).items():
            self.send_header(name, header_value)
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args: object) -> None:
        print(f"[dashboard] {self.address_string()} {format % args}")


class TeamDashboardServer(ThreadingHTTPServer):
    daemon_threads = True


def build_server(
    settings: Settings,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> TeamDashboardServer:
    service = TeamService(settings)
    handler = type(
        "BoundDashboardHandler",
        (DashboardHandler,),
        {"team_service": service},
    )
    return TeamDashboardServer((host, port), handler)


def serve(
    settings: Settings,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = False,
) -> None:
    server = build_server(settings, host=host, port=port)
    url = f"http://{host}:{port}"
    print(f"Codex Team Board: {url}", flush=True)
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
