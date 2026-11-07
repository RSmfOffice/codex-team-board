from __future__ import annotations

import json
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .config import Settings
from .service import TeamError, TeamService
from .db import utc_now

WEB_ROOT = Path(__file__).resolve().parent / "web"
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "CodexTeamBoard/" + __version__
    team_service: TeamService

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        if parsed.path in STATIC_FILES:
            self._serve_static(parsed.path)
            return
        if parsed.path == "/api/health":
            self._send_json(
                {
                    "ok": True,
                    "service": "codex-team-board",
                    "version": __version__,
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
            self._send_json(
                self.team_service.dashboard_snapshot(
                    project_id,
                    include_archived=include_archived,
                )
            )
            return
        self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
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
