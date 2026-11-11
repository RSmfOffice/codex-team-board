from __future__ import annotations

import socket
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from codex_team.config import Settings
from codex_team.dashboard import build_server
from codex_team.service import TeamService


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


class DashboardGetSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        git(self.repo, "init")
        git(self.repo, "config", "user.email", "dashboard-test@example.invalid")
        git(self.repo, "config", "user.name", "Dashboard Test")
        (self.repo / "README.md").write_text("# Dashboard\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "initial")
        self.settings = Settings(
            project_root=Path(__file__).resolve().parents[1],
            home=self.base / "team-home",
            db_path=self.base / "team-home" / "team.sqlite",
            logs_dir=self.base / "team-home" / "logs",
        )
        self.team = TeamService(self.settings)
        self.task = self.team.create_task(
            self.repo,
            title="GET security fixture",
            description="Sensitive task read through the API.",
            acceptance="Only loopback callers may read it.",
            kind="read",
            owner_role="team_explorer",
        )
        self.server = build_server(self.settings, port=0)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()
        self.port = int(self.server.server_address[1])
        self.base_url = f"http://127.0.0.1:{self.port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def request(
        self,
        path: str,
        *,
        host: str,
    ) -> tuple[int, bytes, dict[str, str]]:
        request = urllib.request.Request(
            self.base_url + path,
            headers={"Host": host},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, exc.read(), dict(exc.headers)
            finally:
                exc.close()

    def raw_status(self, payload: bytes) -> bytes:
        with socket.create_connection(
            ("127.0.0.1", self.port),
            timeout=5,
        ) as connection:
            connection.sendall(payload)
            connection.shutdown(socket.SHUT_WR)
            return connection.recv(4096)

    def test_all_api_gets_reject_non_loopback_or_invalid_host(self) -> None:
        paths = [
            "/api/health",
            "/api/projects",
            "/api/summary",
            "/api/board",
            "/api/tasks?limit=1",
            "/api/goals?limit=1",
            "/api/agents?limit=1",
            "/api/attempts?limit=1",
            "/api/events?limit=1",
            f"/api/tasks/{self.task['task_id']}/events?limit=1",
        ]
        for path in paths:
            with self.subTest(path=path):
                status, body, headers = self.request(
                    path,
                    host="evil.example.com",
                )
                self.assertEqual(status, 403)
                self.assertIn(b"loopback Host", body)
                self.assertNotIn("ETag", headers)

        status, _, _ = self.request(
            "/api/summary",
            host="bad_host:not-a-port",
        )
        self.assertEqual(status, 403)

        response = self.raw_status(
            b"GET /api/health HTTP/1.1\r\n"
            b"Host: bad host\r\n"
            b"Connection: close\r\n\r\n"
        )
        self.assertIn(b" 403 ", response)

    def test_rejected_get_does_not_invoke_business_services(self) -> None:
        with (
            patch(
                "codex_team.dashboard.daemon_status",
                side_effect=AssertionError("daemon status must not be read"),
            ),
            patch.object(
                self.team,
                "dashboard_summary",
                side_effect=AssertionError("summary must not be read"),
            ),
            patch.object(
                self.team,
                "dashboard_snapshot",
                side_effect=AssertionError("board must not be read"),
            ),
        ):
            for path in (
                "/api/health",
                "/api/projects",
                "/api/summary",
                "/api/board",
                "/api/tasks?limit=1",
                "/api/events?limit=1",
            ):
                with self.subTest(path=path):
                    status, _, _ = self.request(
                        path,
                        host="evil.example.com",
                    )
                    self.assertEqual(status, 403)

    def test_loopback_host_variants_remain_supported(self) -> None:
        for host in (
            f"127.0.0.1:{self.port}",
            f"localhost:{self.port}",
            f"[::1]:{self.port}",
        ):
            with self.subTest(host=host):
                status, body, _ = self.request(
                    "/api/health",
                    host=host,
                )
                self.assertEqual(status, 200)
                self.assertIn(b'"ok": true', body)


if __name__ == "__main__":
    unittest.main()
