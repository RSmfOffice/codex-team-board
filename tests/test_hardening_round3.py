"""Regression tests for hardening round 3.

Covers:
- connect fast path preserves schema/backfill correctness
- dashboard_events applies consistent visibility rules across scopes
- MCP Content-Length framing rejects oversized/invalid payloads
"""

from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from codex_team.config import Settings
from codex_team.db import SCHEMA_VERSION, connect
from codex_team.mcp import MAX_MCP_MESSAGE_BYTES, _read_message
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


class ConnectCorrectnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.db_path = self.base / "team.sqlite"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_repeated_connect_keeps_schema_and_markers(self) -> None:
        first = connect(self.db_path)
        first.close()
        second = connect(self.db_path)
        try:
            version = int(
                second.execute("PRAGMA user_version").fetchone()[0]
            )
            self.assertEqual(version, SCHEMA_VERSION)
            markers = {
                str(row["key"])
                for row in second.execute(
                    "SELECT key FROM metadata WHERE key LIKE '%backfill%'"
                ).fetchall()
            }
            self.assertIn("workflow_epoch_backfill_v20", markers)
            self.assertIn("goal_archive_done_backfill_v11", markers)
            self.assertIn("goal_archive_all_tasks_backfill_v16", markers)
            triggers = second.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'"
            ).fetchone()[0]
            self.assertGreater(triggers, 0)
        finally:
            second.close()

    def test_null_profile_self_heals_on_connect(self) -> None:
        connection = connect(self.db_path)
        try:
            connection.execute(
                """
                INSERT INTO projects(
                    project_id, name, root_path, created_at, updated_at
                ) VALUES ('P1', 'Heal', 'D:/heal', '2026-01-01T00:00:00Z',
                          '2026-01-01T00:00:00Z')
                """
            )
            connection.execute(
                "ALTER TABLE cluster_agents DROP COLUMN profile"
            )
            connection.execute(
                "ALTER TABLE cluster_agents ADD COLUMN profile TEXT"
            )
            connection.execute(
                """
                INSERT INTO cluster_agents(
                    agent_id, project_id, role, workstream_key, display_name,
                    thread_id, profile, status, created_at, updated_at
                ) VALUES ('A1', 'P1', 'team_explorer', 'main', 'Heal',
                          'thread-heal', NULL, 'idle',
                          '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
                """
            )
            connection.commit()
        finally:
            connection.close()
        reopened = connect(self.db_path)
        try:
            row = reopened.execute(
                "SELECT profile FROM cluster_agents WHERE agent_id = 'A1'"
            ).fetchone()
            self.assertEqual(row["profile"], "legacy")
        finally:
            reopened.close()


class EventVisibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.email", "round3@example.invalid")
        git(self.repo, "config", "user.name", "Round Three")
        (self.repo / "README.md").write_text("# fixture\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "initial")
        self.settings = Settings(
            project_root=Path(__file__).resolve().parents[1],
            home=self.base / "team-home",
            db_path=self.base / "team-home" / "team.sqlite",
            logs_dir=self.base / "team-home" / "logs",
        )
        self.team = TeamService(self.settings)
        self.project = self.team.register_project(self.repo)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_canceled_task_events_hidden_in_both_scopes(self) -> None:
        task = self.team.create_task(
            self.repo,
            title="Canceled task",
            description="fixture",
            acceptance="fixture",
            kind="read",
            owner_role="team_explorer",
        )
        self.team.cancel_task(
            str(task["task_id"]),
            actor="lead",
            reason="fixture cancel",
        )
        project_id = str(self.project["project_id"])
        global_events = self.team.dashboard_events()["events"]
        scoped_events = self.team.dashboard_events(project_id)["events"]
        global_task_ids = {event["task_id"] for event in global_events}
        scoped_task_ids = {event["task_id"] for event in scoped_events}
        self.assertNotIn(task["task_id"], global_task_ids)
        self.assertNotIn(task["task_id"], scoped_task_ids)
        # include_archived only relaxes archive visibility; canceled tasks stay
        # hidden from the event stream in every scope (matches board snapshot).
        included = self.team.dashboard_events(
            project_id, include_archived=True
        )["events"]
        self.assertNotIn(
            task["task_id"],
            {event["task_id"] for event in included},
        )


class McpFramingTests(unittest.TestCase):
    def test_valid_content_length_frame(self) -> None:
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        raw = (
            f"Content-Length: {len(payload.encode('utf-8'))}\r\n\r\n{payload}"
        ).encode("utf-8")
        message = _read_message(io.BytesIO(raw))
        self.assertEqual(message["method"], "ping")

    def test_oversized_content_length_rejected(self) -> None:
        raw = f"Content-Length: {MAX_MCP_MESSAGE_BYTES + 1}\r\n\r\n".encode(
            "utf-8"
        )
        with self.assertRaisesRegex(ValueError, "Content-Length"):
            _read_message(io.BytesIO(raw))

    def test_invalid_content_length_rejected(self) -> None:
        raw = b"Content-Length: not-a-number\r\n\r\n"
        with self.assertRaisesRegex(ValueError, "Content-Length"):
            _read_message(io.BytesIO(raw))

    def test_truncated_payload_rejected(self) -> None:
        raw = b"Content-Length: 100\r\n\r\n{}"
        with self.assertRaisesRegex(ValueError, "truncated"):
            _read_message(io.BytesIO(raw))


if __name__ == "__main__":
    unittest.main()