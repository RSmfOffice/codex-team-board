from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from codex_team.cluster import ClusterError, ClusterManager
from codex_team.config import Settings
from codex_team.dashboard import build_server
from codex_team.db import utc_now
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


class DashboardClusterControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        git(self.repo, "init")
        git(self.repo, "config", "user.email", "dashboard-test@example.invalid")
        git(self.repo, "config", "user.name", "Dashboard Test")
        (self.repo / "README.md").write_text("# Dashboard test\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "initial")
        self.settings = Settings(
            project_root=Path(__file__).resolve().parents[1],
            home=self.base / "team-home",
            db_path=self.base / "team-home" / "team.sqlite",
            logs_dir=self.base / "team-home" / "logs",
        )
        self.team = TeamService(self.settings)
        self.project = self.team.init_project(self.repo)
        git(self.repo, "add", ".codex", "AGENTS.md")
        git(self.repo, "commit", "-m", "initialize team policy")
        self.server = build_server(self.settings, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def post(self, path: str, payload: object) -> tuple[int, dict[str, object]]:
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode("utf-8"))
            finally:
                exc.close()

    def get(self, path: str) -> dict[str, object]:
        with urllib.request.urlopen(self.base_url + path, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_create_cluster_goal_and_start_daemon(self) -> None:
        daemon = {"running": True, "pid": 1234}
        with patch(
            "codex_team.dashboard.ensure_daemon",
            return_value=daemon,
        ) as start_daemon:
            status, body = self.post(
                "/api/clusters",
                {
                    "project_id": self.project["project_id"],
                    "objective": "Build the release",
                    "autonomy": "supervised",
                    "max_worker_attempts": 4,
                    "max_lead_attempts": 6,
                },
            )

        self.assertEqual(status, 200)
        self.assertEqual(body["daemon"], daemon)
        goal = body["goal"]
        self.assertEqual(goal["project_id"], self.project["project_id"])
        self.assertEqual(goal["objective"], "Build the release")
        self.assertEqual(goal["autonomy"], "supervised")
        self.assertEqual(goal["max_worker_attempts"], 4)
        self.assertEqual(goal["max_lead_attempts"], 6)
        start_daemon.assert_called_once_with(self.settings)

    def test_dashboard_exposes_default_thread_reuse_runtime(self) -> None:
        connection = sqlite3.connect(self.settings.db_path)
        try:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0],
                17,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0],
                17,
            )
            connection.execute("PRAGMA user_version = 7")
            connection.execute(
                "UPDATE metadata SET value = '7' WHERE key = 'schema_version'"
            )
            connection.commit()
        finally:
            connection.close()
        with patch.dict(os.environ, {"CODEX_TEAM_AGENT_RUNTIME": ""}):
            health = self.get("/api/health")
            board = self.get("/api/board")

        self.assertEqual(health["schema_version"], 17)
        self.assertEqual(
            health["cluster_runtime"],
            {
                "runtime": "app_server",
                "thread_reuse": True,
                "running": False,
                "config_mismatch": False,
                "version": "0.5.0",
            },
        )
        self.assertEqual(board["cluster_runtime"], health["cluster_runtime"])

    def test_control_cluster_goal_pause_resume_and_cancel(self) -> None:
        cluster = ClusterManager(self.settings, team=self.team)
        goal = cluster.create_goal(
            self.repo,
            objective="Control the release",
            autonomy="auto",
        )
        daemon = {"running": True, "pid": 4321}
        with (
            patch(
                "codex_team.dashboard.daemon_status",
                return_value=daemon,
            ) as current_daemon,
            patch(
                "codex_team.dashboard.ensure_daemon",
                return_value=daemon,
            ) as start_daemon,
        ):
            status, paused = self.post(
                "/api/clusters/control",
                {"goal_id": goal["goal_id"], "action": "pause"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(paused["goal"]["status"], "paused")
            self.assertEqual(paused["daemon"], daemon)
            start_daemon.assert_not_called()

            status, resumed = self.post(
                "/api/clusters/control",
                {"goal_id": goal["goal_id"], "action": "resume"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(resumed["goal"]["status"], "active")
            self.assertEqual(resumed["daemon"], daemon)
            start_daemon.assert_called_once_with(self.settings)
            board = self.get("/api/board")
            board_goal = next(
                item
                for item in board["cluster_goals"]
                if item["goal_id"] == goal["goal_id"]
            )
            self.assertEqual(board_goal["status"], "active")

            task = self.team.create_task(
                self.repo,
                title="Canceled with goal",
                description="Must be blocked when the goal is canceled.",
                acceptance="blocked with goal cancellation reason",
                kind="read",
                owner_role="team_explorer",
                goal_id=goal["goal_id"],
            )
            self.team.claim_task(task["task_id"], actor="worker")
            with self.team.connection() as connection:
                connection.execute(
                    """
                    INSERT INTO cluster_jobs(
                        job_id, goal_id, task_id, kind, role, status,
                        log_path, output_path, started_at
                    ) VALUES (
                        'J-CANCEL-TEST', ?, ?, 'worker', 'team_explorer', 'running',
                        ?, ?, ?
                    )
                    """,
                    (
                        goal["goal_id"],
                        task["task_id"],
                        str(self.base / "cancel.log"),
                        str(self.base / "cancel.out"),
                        "2026-11-07T10:00:00Z",
                    ),
                )
            status, canceled = self.post(
                "/api/clusters/control",
                {"goal_id": goal["goal_id"], "action": "cancel"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(canceled["goal"]["status"], "failed")
            self.assertIn("cancel requested", canceled["goal"]["last_error"])
            current_daemon.assert_called()
            canceled_task = self.team.get_task(task["task_id"])
            self.assertEqual(canceled_task["status"], "blocked")
            self.assertIn("goal canceled", canceled_task["blocker"])
            with self.team.connection(write=False) as connection:
                job = connection.execute(
                    "SELECT status, error FROM cluster_jobs WHERE job_id = 'J-CANCEL-TEST'"
                ).fetchone()
            self.assertEqual(job["status"], "interrupted")
            self.assertIn("cancel requested", job["error"])
            status, invalid_resume = self.post(
                "/api/clusters/control",
                {"goal_id": goal["goal_id"], "action": "resume"},
            )
            self.assertEqual(status, 409)
            self.assertIn("invalid goal transition", invalid_resume["error"])
            done_task = self.team.create_task(
                self.repo,
                title="Goal cascade archive",
                description="Done task archived with its goal.",
                acceptance="cascade through dashboard API",
                kind="read",
                owner_role="team_explorer",
                goal_id=goal["goal_id"],
            )
            with self.team.connection() as connection:
                connection.execute(
                    """
                    UPDATE tasks
                    SET status = 'done', completed_at = ?, updated_at = ?,
                        version = version + 1
                    WHERE task_id = ?
                    """,
                    (utc_now(), utc_now(), done_task["task_id"]),
                )
            status, archived = self.post(
                "/api/clusters/archive",
                {"goal_id": goal["goal_id"], "archived": True},
            )
            self.assertEqual(status, 200)
            self.assertTrue(archived["goal"]["archived_at"])
            archived_task = self.team.get_task(done_task["task_id"])
            self.assertIsNotNone(archived_task["archived_at"])
            self.assertEqual(archived_task["archived_by_goal"], 1)
            active_board = self.get("/api/board")
            visible_board = self.get("/api/board?include_archived=1")
            self.assertNotIn(
                goal["goal_id"],
                {item["goal_id"] for item in active_board["cluster_goals"]},
            )
            self.assertIn(
                goal["goal_id"],
                {item["goal_id"] for item in visible_board["cluster_goals"]},
            )
            status, restored = self.post(
                "/api/clusters/archive",
                {"goal_id": goal["goal_id"], "archived": False},
            )
            self.assertEqual(status, 200)
            self.assertIsNone(restored["goal"]["archived_at"])
            restored_task = self.team.get_task(done_task["task_id"])
            self.assertIsNone(restored_task["archived_at"])
            self.assertEqual(restored_task["archived_by_goal"], 0)

    def test_daemon_start_failure_marks_goal_failed(self) -> None:
        with patch(
            "codex_team.dashboard.ensure_daemon",
            side_effect=ClusterError("daemon unavailable"),
        ):
            status, body = self.post(
                "/api/clusters",
                {
                    "project_id": self.project["project_id"],
                    "objective": "Daemon failure rollback",
                },
            )
        self.assertEqual(status, 409)
        self.assertIn("failed to start", body["error"])
        board = self.get("/api/board")
        failed = next(
            goal
            for goal in board["cluster_goals"]
            if goal["objective"] == "Daemon failure rollback"
        )
        self.assertEqual(failed["status"], "failed")
        self.assertIn("daemon start failed", failed["last_error"])

    def test_archived_project_is_rejected_before_starting_daemon(self) -> None:
        self.team.set_project_archived(self.project["project_id"], True)
        with patch("codex_team.dashboard.ensure_daemon") as start_daemon:
            status, body = self.post(
                "/api/clusters",
                {
                    "project_id": self.project["project_id"],
                    "objective": "Must not start",
                },
            )

        self.assertEqual(status, 409)
        self.assertIn("archived", body["error"])
        start_daemon.assert_not_called()

    def test_invalid_cluster_requests_return_json_errors(self) -> None:
        requests = [
            (
                "/api/clusters",
                {"project_id": self.project["project_id"]},
            ),
            (
                "/api/clusters",
                {
                    "project_id": self.project["project_id"],
                    "objective": "Invalid autonomy",
                    "autonomy": "manual",
                },
            ),
            (
                "/api/clusters",
                {
                    "project_id": self.project["project_id"],
                    "objective": "Invalid autonomy type",
                    "autonomy": [],
                },
            ),
            (
                "/api/clusters",
                {
                    "project_id": self.project["project_id"],
                    "objective": "Invalid attempts",
                    "max_worker_attempts": 0,
                },
            ),
            (
                "/api/clusters/control",
                {"goal_id": "G-MISSING", "action": "restart"},
            ),
        ]
        with patch("codex_team.dashboard.ensure_daemon") as start_daemon:
            for path, payload in requests:
                with self.subTest(path=path, payload=payload):
                    status, body = self.post(path, payload)
                    self.assertGreaterEqual(status, 400)
                    self.assertLess(status, 500)
                    self.assertIn("error", body)
            start_daemon.assert_not_called()

    def test_reset_agent_rejects_busy_and_records_idle_request(self) -> None:
        now = "2026-11-07T11:30:00Z"
        with self.team.connection() as connection:
            for agent_id, status, archived_at in (
                ("A-BUSY", "busy", None),
                ("A-IDLE", "idle", None),
                ("A-SUPERSEDED", "superseded", None),
                ("A-ARCHIVED", "stored", now),
            ):
                connection.execute(
                    """
                    INSERT INTO cluster_agents(
                        agent_id, project_id, role, workstream_key,
                        display_name, thread_id, status, archived_at,
                        created_at, updated_at
                    ) VALUES (?, ?, 'team_worker', 'reset-test', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        agent_id,
                        self.project["project_id"],
                        f"Agent {agent_id}",
                        f"thread-{agent_id.lower()}",
                        status,
                        archived_at,
                        now,
                        now,
                    ),
                )

        with (
            patch(
                "codex_team.dashboard.ensure_daemon",
                side_effect=AssertionError("reset must not start a daemon"),
            ) as start_daemon,
            patch(
                "codex_team.dashboard.daemon_status",
                side_effect=AssertionError("reset must not inspect a daemon"),
            ) as read_daemon,
        ):
            status, busy = self.post(
                "/api/agents/reset",
                {"agent_id": "A-BUSY", "reason": "must be rejected"},
            )
            status_ok, reset = self.post(
                "/api/agents/reset",
                {"agent_id": "A-IDLE", "reason": "manual diagnostics reset"},
            )
            status_superseded, superseded = self.post(
                "/api/agents/reset",
                {"agent_id": "A-SUPERSEDED", "reason": "must be rejected"},
            )
            status_archived, archived = self.post(
                "/api/agents/reset",
                {"agent_id": "A-ARCHIVED", "reason": "must be rejected"},
            )

        self.assertEqual(status, 409)
        self.assertIn("busy", busy["error"])
        self.assertEqual(status_superseded, 409)
        self.assertIn("superseded", superseded["error"])
        self.assertEqual(status_archived, 409)
        self.assertIn("archived", archived["error"])
        self.assertEqual(status_ok, 200)
        self.assertEqual(reset["agent"]["agent_id"], "A-IDLE")
        self.assertEqual(reset["agent"]["status"], "idle")
        self.assertIsNotNone(reset["agent"]["reset_requested_at"])
        self.assertEqual(
            reset["agent"]["reset_reason"],
            "manual diagnostics reset",
        )
        start_daemon.assert_not_called()
        read_daemon.assert_not_called()

        with self.team.connection(write=False) as connection:
            agents = {
                row["agent_id"]: dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM cluster_agents
                    WHERE agent_id IN (
                        'A-BUSY', 'A-IDLE', 'A-SUPERSEDED', 'A-ARCHIVED'
                    )
                    """
                )
            }
            events = connection.execute(
                """
                SELECT agent_id, event_type, actor, data_json
                FROM cluster_agent_events
                ORDER BY event_id
                """
            ).fetchall()
        self.assertIsNone(agents["A-BUSY"]["reset_requested_at"])
        self.assertEqual(agents["A-BUSY"]["reset_reason"], "")
        self.assertIsNone(agents["A-SUPERSEDED"]["reset_requested_at"])
        self.assertIsNone(agents["A-ARCHIVED"]["reset_requested_at"])
        self.assertIsNotNone(agents["A-IDLE"]["reset_requested_at"])
        self.assertEqual(
            agents["A-IDLE"]["reset_reason"],
            "manual diagnostics reset",
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["agent_id"], "A-IDLE")
        self.assertEqual(events[0]["event_type"], "reset_requested")
        self.assertEqual(events[0]["actor"], "human")
        self.assertEqual(
            json.loads(events[0]["data_json"])["reason"],
            "manual diagnostics reset",
        )

    def test_malformed_json_returns_json_error(self) -> None:
        request = urllib.request.Request(
            self.base_url + "/api/clusters",
            data=b"{",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        error = caught.exception
        self.assertEqual(error.code, 400)
        body = json.loads(error.read().decode("utf-8"))
        error.close()
        self.assertIn("error", body)


if __name__ == "__main__":
    unittest.main()
