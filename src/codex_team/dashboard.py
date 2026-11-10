from __future__ import annotations

import json
import hashlib
import ipaddress
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

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
        return {
            "runtime": runtime,
            "thread_reuse": runtime == "app_server",
            "running": bool(status.get("running")),
            "config_mismatch": bool(status.get("config_mismatch")),
            "version": identity.get("version"),
        }

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
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
            include_archived = query.get("include_archived", ["0"])[0] in {"1", "true"}
            data = self.team_service.dashboard_snapshot(
                include_archived=include_archived
            )
            self._send_json(
                {
                    "projects": data["projects"],
                    "generated_at": data["generated_at"],
                }
            )
            return
        if parsed.path == "/api/board":
            query = parse_qs(parsed.query)
            project_id = query.get("project_id", [""])[0] or None
            include_archived = query.get("include_archived", ["0"])[0] in {"1", "true"}
            data = self.team_service.dashboard_snapshot(
                project_id,
                include_archived=include_archived,
            )
            data["cluster_runtime"] = self._cluster_runtime()
            self._send_json(data)
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

    def _write_request_rejection(
        self,
    ) -> tuple[HTTPStatus, str] | None:
        authority = str(self.headers.get("Host") or "").strip()
        try:
            hostname, _ = _authority_parts(authority)
        except ValueError:
            return (
                HTTPStatus.FORBIDDEN,
                "write requests require a valid loopback Host header",
            )
        if not _is_loopback_host(hostname):
            return (
                HTTPStatus.FORBIDDEN,
                "write requests require a loopback Host header",
            )
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

    def _send_json(
        self,
        value: object,
        *,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        content = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
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
