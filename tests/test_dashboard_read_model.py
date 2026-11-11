from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from codex_team.config import Settings
from codex_team.dashboard import build_server
from codex_team.db import (
    DASHBOARD_REVISION_TABLES,
    read_dashboard_revision,
)
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


class DashboardReadModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        git(self.repo, "init")
        git(self.repo, "config", "user.email", "read-model@example.invalid")
        git(self.repo, "config", "user.name", "Read Model Test")
        (self.repo / "README.md").write_text("# Read model\n", encoding="utf-8")
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
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()
        self.base_url = (
            f"http://127.0.0.1:{self.server.server_address[1]}"
        )

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def request(
        self,
        path: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes, dict[str, str]]:
        request = urllib.request.Request(
            self.base_url + path,
            headers=headers or {},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return (
                    response.status,
                    response.read(),
                    dict(response.headers.items()),
                )
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, exc.read(), dict(exc.headers.items())
            finally:
                exc.close()

    def json_request(self, path: str) -> tuple[int, dict[str, object]]:
        status, body, _ = self.request(path)
        return status, json.loads(body.decode("utf-8"))

    def create_read_task(self, index: int, *, title: str | None = None):
        return self.team.create_task(
            self.repo,
            title=title or f"Read task {index}",
            description="Read-model fixture",
            acceptance="bounded response",
            kind="read",
            owner_role="team_explorer",
        )

    def ledger_value(self) -> int:
        revision = self.team.dashboard_revision()
        self.assertTrue(revision.startswith("v2:"))
        return int(revision.split(":", 1)[1])

    def assert_revision_advances(self, before: int) -> int:
        after = self.ledger_value()
        self.assertGreater(after, before)
        return after

    def test_cacheable_get_endpoints_validate_scope_and_errors(self) -> None:
        task = self.create_read_task(1, title="Validator fixture")
        claimed = self.team.claim_task(
            task["task_id"],
            actor="validator-fixture",
        )
        self.team.heartbeat(
            task["task_id"],
            actor="validator-fixture",
            claim_token=claimed["claim_token"],
        )
        project_id = str(self.project["project_id"])
        endpoints = [
            "/api/projects",
            "/api/summary",
            "/api/board",
            "/api/tasks",
            "/api/goals",
            "/api/agents",
            "/api/attempts",
            "/api/events",
            f"/api/tasks/{task['task_id']}/events",
        ]
        etags: dict[str, str] = {}
        for path in endpoints:
            with self.subTest(path=path):
                status, _, headers = self.request(path)
                self.assertEqual(status, 200)
                etag = headers.get("ETag", "")
                self.assertTrue(etag.startswith('W/"'))
                self.assertEqual(headers.get("Cache-Control"), "no-store")
                etags[path] = etag

                strong_etag = etag[2:] if etag.startswith("W/") else etag
                status, body, conditional_headers = self.request(
                    path,
                    headers={
                        "If-None-Match": f'"unrelated", {strong_etag}',
                    },
                )
                self.assertEqual(status, 304)
                self.assertEqual(body, b"")
                self.assertEqual(conditional_headers.get("ETag"), etag)
                self.assertEqual(
                    conditional_headers.get("Cache-Control"),
                    "no-store",
                )
                self.assertEqual(
                    self.request(
                        path,
                        headers={"If-None-Match": "*"},
                    )[0],
                    304,
                )

        _, _, health_headers = self.request("/api/health")
        self.assertNotIn("ETag", health_headers)
        self.assertEqual(health_headers.get("Cache-Control"), "no-store")

        status, _, error_headers = self.request(
            "/api/tasks?limit=501",
            headers={"If-None-Match": "*"},
        )
        self.assertEqual(status, 400)
        self.assertNotIn("ETag", error_headers)
        self.assertEqual(error_headers.get("Cache-Control"), "no-store")

        scoped_paths = [
            "/api/summary",
            "/api/board",
            "/api/tasks",
            "/api/goals",
            "/api/agents",
            "/api/attempts",
            "/api/events",
            f"/api/tasks/{task['task_id']}/events",
        ]
        for path in scoped_paths:
            with self.subTest(scope=path):
                separator = "&" if "?" in path else "?"
                if path.startswith("/api/tasks/") and path.endswith("/events"):
                    alternate = f"{path}?limit=1"
                else:
                    alternate = (
                        f"{path}{separator}project_id={project_id}"
                    )
                _, _, headers = self.request(alternate)
                self.assertNotEqual(headers.get("ETag"), etags[path])

        _, _, archived_headers = self.request(
            f"/api/tasks?project_id={project_id}&include_archived=1"
        )
        self.assertNotEqual(
            archived_headers.get("ETag"),
            etags["/api/tasks"],
        )
        _, _, archived_projects = self.request(
            "/api/projects?include_archived=1"
        )
        self.assertNotEqual(
            archived_projects.get("ETag"),
            etags["/api/projects"],
        )
        for left, right in (
            ("/api/tasks?limit=1", "/api/tasks?limit=2"),
            ("/api/events?limit=1", "/api/events?limit=2"),
            (
                f"/api/tasks/{task['task_id']}/events?limit=1",
                f"/api/tasks/{task['task_id']}/events?limit=2",
            ),
        ):
            with self.subTest(limit=(left, right)):
                self.assertNotEqual(
                    self.request(left)[2].get("ETag"),
                    self.request(right)[2].get("ETag"),
                )

    def test_generated_clock_bucket_and_threshold_transitions(self) -> None:
        task = self.create_read_task(1, title="Clock fixture")
        anchor = datetime(2030, 1, 1, 0, 1, 0, tzinfo=UTC)
        created_at = (anchor - timedelta(seconds=39)).isoformat().replace(
            "+00:00",
            "Z",
        )
        with self.team.connection() as connection:
            connection.execute(
                "UPDATE tasks SET created_at = ? WHERE task_id = ?",
                (created_at, task["task_id"]),
            )

        class FrozenDateTime(datetime):
            current = anchor

            @classmethod
            def now(cls, tz=None):
                if tz is None:
                    return cls.current.replace(tzinfo=None)
                return cls.current.astimezone(tz)

        def at_offset(
            seconds: int,
        ) -> tuple[object, object]:
            FrozenDateTime.current = anchor + timedelta(seconds=seconds)
            return (
                patch(
                    "codex_team.service.datetime",
                    FrozenDateTime,
                ),
                patch(
                    "codex_team.service.utc_now",
                    side_effect=lambda: FrozenDateTime.current.isoformat(
                        timespec="seconds"
                    ).replace("+00:00", "Z"),
                ),
            )

        def request_at(
            path: str,
            seconds: int,
            *,
            etag: str = "",
        ) -> tuple[int, bytes, dict[str, str]]:
            datetime_patch, now_patch = at_offset(seconds)
            with datetime_patch, now_patch:
                return self.request(
                    path,
                    headers={"If-None-Match": etag} if etag else None,
                )

        status, _, headers = request_at("/api/projects", 10)
        projects_etag = headers["ETag"]
        self.assertEqual(status, 200)
        self.assertEqual(
            request_at(
                "/api/projects",
                29,
                etag=projects_etag,
            )[0],
            304,
        )

        status, body, headers = request_at("/api/board", 20)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["tasks"][0]["claim_wait_seconds"], 59)
        self.assertEqual(
            json.loads(body)["tasks"][0]["claim_wait_state"],
            "normal",
        )
        board_etag = headers["ETag"]
        self.assertEqual(
            request_at("/api/board", 20, etag=board_etag)[0],
            304,
        )

        status, body, changed_headers = request_at(
            "/api/board",
            21,
            etag=board_etag,
        )
        self.assertEqual(status, 200)
        task_payload = json.loads(body)["tasks"][0]
        self.assertEqual(task_payload["claim_wait_seconds"], 60)
        self.assertEqual(task_payload["claim_wait_state"], "warning")
        self.assertNotEqual(changed_headers["ETag"], board_etag)
        self.assertEqual(
            request_at(
                "/api/board",
                21,
                etag=changed_headers["ETag"],
            )[0],
            304,
        )
        self.assertEqual(
            request_at(
                "/api/board",
                50,
                etag=changed_headers["ETag"],
            )[0],
            200,
        )

        status, _, headers = request_at("/api/summary", 20)
        summary_etag = headers["ETag"]
        self.assertEqual(status, 200)
        self.assertEqual(
            request_at(
                "/api/summary",
                29,
                etag=summary_etag,
            )[0],
            304,
        )
        self.assertEqual(
            request_at(
                "/api/summary",
                50,
                etag=summary_etag,
            )[0],
            200,
        )

    def test_revision_summary_and_conditional_board(self) -> None:
        task = self.create_read_task(1)
        summary = self.team.dashboard_summary()
        board = self.team.dashboard_snapshot()
        self.assertEqual(summary["revision"], board["revision"])
        self.assertEqual(summary["revision"], self.team.dashboard_revision())

        status, board_body, headers = self.request("/api/board")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(board_body)["tasks"])
        etag = headers["ETag"]
        self.assertTrue(etag.startswith('W/"v2:') and etag.endswith('"'))

        status, not_modified_body, _ = self.request(
            "/api/board",
            headers={"If-None-Match": etag},
        )
        self.assertEqual(status, 304)
        self.assertEqual(not_modified_body, b"")

        status, summary_body, summary_headers = self.request("/api/summary")
        self.assertEqual(status, 200)
        summary_payload = json.loads(summary_body)
        self.assertEqual(summary_payload["counts"]["ready"], 1)
        self.assertIn("cluster_runtime", summary_payload)
        self.assertEqual(
            self.request(
                "/api/summary",
                headers={"If-None-Match": summary_headers["ETag"]},
            )[0],
            304,
        )

        before = self.team.dashboard_summary()["revision"]
        self.team.set_task_archived(task["task_id"], True)
        after = self.team.dashboard_summary()["revision"]
        self.assertNotEqual(before, after)

    def test_revision_trigger_contract_covers_semantic_tables(self) -> None:
        with self.team.connection(write=False) as connection:
            ledger = connection.execute(
                """
                SELECT revision
                FROM dashboard_revision
                WHERE singleton = 1
                """
            ).fetchone()
            self.assertIsNotNone(ledger)
            self.assertGreaterEqual(int(ledger["revision"]), 0)
            for table in DASHBOARD_REVISION_TABLES:
                triggers = {
                    str(row["name"])
                    for row in connection.execute(
                        """
                        SELECT name
                        FROM sqlite_master
                        WHERE type = 'trigger' AND tbl_name = ?
                        """,
                        (table,),
                    ).fetchall()
                }
                self.assertTrue(
                    {
                        f"trg_dashboard_revision_{table}_insert",
                        f"trg_dashboard_revision_{table}_update",
                        f"trg_dashboard_revision_{table}_delete",
                    }.issubset(triggers)
                )

    def test_revision_is_monotonic_without_timestamp_changes(self) -> None:
        task = self.create_read_task(
            1,
            title="Original same-second title",
        )
        original_title = str(task["title"])
        with self.team.connection() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET title = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (
                    original_title,
                    "2030-01-01T00:00:00Z",
                    task["task_id"],
                ),
            )

        before = self.ledger_value()
        TeamService(self.settings)
        self.team.register_project(self.repo)
        self.assertEqual(self.ledger_value(), before)
        self.assertEqual(self.team.dashboard_summary()["revision"], f"v2:{before}")
        self.assertEqual(self.team.dashboard_snapshot()["revision"], f"v2:{before}")

        with self.team.connection() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET title = ?, updated_at = updated_at
                WHERE task_id = ?
                """,
                ("Same-second edit one", task["task_id"]),
            )
        after_first = self.assert_revision_advances(before)

        with self.team.connection() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET title = ?, updated_at = updated_at
                WHERE task_id = ?
                """,
                ("Same-second edit two", task["task_id"]),
            )
        after_second = self.assert_revision_advances(after_first)

        with self.team.connection() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET title = ?, updated_at = updated_at
                WHERE task_id = ?
                """,
                (original_title, task["task_id"]),
            )
        after_revert = self.assert_revision_advances(after_second)

        with self.team.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE tasks SET title = ? WHERE task_id = ?",
                ("Rolled back title", task["task_id"]),
            )
            self.assertGreater(
                read_dashboard_revision(connection),
                after_revert,
            )
            connection.rollback()
        self.assertEqual(self.ledger_value(), after_revert)

        victim = self.create_read_task(2, title="Delete trigger fixture")
        before_delete = self.ledger_value()
        with self.team.connection() as connection:
            connection.execute(
                "DELETE FROM tasks WHERE task_id = ?",
                (victim["task_id"],),
            )
        self.assert_revision_advances(before_delete)
        self.assertFalse(
            any(
                item["task_id"] == victim["task_id"]
                for item in self.team.dashboard_snapshot()["tasks"]
            )
        )

    def test_every_semantic_update_advances_revision(self) -> None:
        project_id = str(self.project["project_id"])
        now = "2030-01-01T00:00:00Z"
        with self.team.connection() as connection:
            for task_id in (
                "T-REV-BASE-1",
                "T-REV-BASE-2",
                "T-REV-DEP-A",
                "T-REV-DEP-B",
            ):
                connection.execute(
                    """
                    INSERT INTO tasks(
                        task_id, project_id, title, description,
                        acceptance, kind, status, priority, owner_role,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, 'Revision fixture', 'Advance ledger',
                              'read', 'ready', 2, 'team_explorer', ?, ?)
                    """,
                    (task_id, project_id, task_id, now, now),
                )
            connection.execute(
                """
                INSERT INTO task_dependencies(task_id, depends_on)
                VALUES ('T-REV-DEP-A', 'T-REV-BASE-1')
                """
            )
            connection.execute(
                """
                INSERT INTO task_events(
                    task_id, event_type, actor, data_json, created_at
                ) VALUES ('T-REV-BASE-1', 'fixture', 'test', '{}', ?)
                """,
                (now,),
            )
            connection.execute(
                """
                INSERT INTO messages(
                    project_id, sender, body, created_at
                ) VALUES (?, 'test', 'revision message', ?)
                """,
                (project_id, now),
            )
            connection.execute(
                """
                INSERT INTO cluster_goals(
                    goal_id, project_id, objective, status, autonomy,
                    created_at, updated_at
                ) VALUES ('G-REV', ?, 'Revision goal', 'paused',
                          'supervised', ?, ?)
                """,
                (project_id, now, now),
            )
            connection.execute(
                """
                INSERT INTO cluster_events(
                    goal_id, event_type, actor, data_json, created_at
                ) VALUES ('G-REV', 'fixture', 'test', '{}', ?)
                """,
                (now,),
            )
            connection.execute(
                """
                INSERT INTO cluster_agents(
                    agent_id, project_id, role, workstream_key,
                    display_name, thread_id, status, created_at, updated_at
                ) VALUES ('A-REV', ?, 'team_explorer', 'revision',
                          'Revision Agent', 'thread-revision', 'idle', ?, ?)
                """,
                (project_id, now, now),
            )
            connection.execute(
                """
                INSERT INTO cluster_agent_events(
                    agent_id, event_type, actor, data_json, created_at
                ) VALUES ('A-REV', 'fixture', 'test', '{}', ?)
                """,
                (now,),
            )
            connection.execute(
                """
                INSERT INTO cluster_jobs(
                    job_id, goal_id, kind, role, status,
                    log_path, output_path, started_at
                ) VALUES ('J-REV', 'G-REV', 'worker', 'team_worker',
                          'completed', 'revision.log', 'revision.out', ?)
                """,
                (now,),
            )

        before = self.ledger_value()
        updates = (
            (
                "projects",
                "UPDATE projects SET name = ? WHERE project_id = ?",
                ("Revision Project", project_id),
            ),
            (
                "tasks",
                """
                UPDATE tasks
                SET title = ?, updated_at = updated_at
                WHERE task_id = 'T-REV-BASE-2'
                """,
                ("Revision Task",),
            ),
            (
                "task_dependencies",
                """
                UPDATE task_dependencies
                SET task_id = 'T-REV-DEP-B'
                WHERE task_id = 'T-REV-DEP-A'
                """,
                (),
            ),
            (
                "task_events",
                """
                UPDATE task_events SET data_json = '{"updated":true}'
                WHERE task_id = 'T-REV-BASE-1'
                """,
                (),
            ),
            (
                "messages",
                "UPDATE messages SET body = 'revision message updated'",
                (),
            ),
            (
                "cluster_goals",
                "UPDATE cluster_goals SET last_error = 'ledger update' "
                "WHERE goal_id = 'G-REV'",
                (),
            ),
            (
                "cluster_jobs",
                "UPDATE cluster_jobs SET error = 'ledger update' "
                "WHERE job_id = 'J-REV'",
                (),
            ),
            (
                "cluster_agents",
                "UPDATE cluster_agents SET last_error = 'ledger update' "
                "WHERE agent_id = 'A-REV'",
                (),
            ),
            (
                "cluster_agent_events",
                "UPDATE cluster_agent_events "
                "SET data_json = '{\"updated\":true}'",
                (),
            ),
            (
                "cluster_events",
                "UPDATE cluster_events SET data_json = '{\"updated\":true}'",
                (),
            ),
        )
        for table, sql, parameters in updates:
            with self.subTest(table=table):
                revision_before = self.ledger_value()
                with self.team.connection() as connection:
                    connection.execute(sql, parameters)
                before = self.assert_revision_advances(revision_before)

        self.assertGreater(before, 0)

    def test_snapshot_rows_and_revision_come_from_one_read_txn(self) -> None:
        task = self.create_read_task(1, title="Before concurrent write")
        initial_revision = self.team.dashboard_revision()

        class ConcurrentWriterService(TeamService):
            def __init__(self, settings: Settings) -> None:
                self.write_once = True
                super().__init__(settings)

            def _dashboard_revision(
                self,
                connection,
                project_id,
                include_archived,
            ) -> str:
                if self.write_once:
                    self.write_once = False
                    with self.connection() as writer:
                        writer.execute(
                            """
                            UPDATE tasks
                            SET title = 'After concurrent write'
                            WHERE task_id = ?
                            """,
                            (task["task_id"],),
                        )
                return super()._dashboard_revision(
                    connection,
                    project_id,
                    include_archived,
                )

        snapshot = ConcurrentWriterService(self.settings).dashboard_snapshot()
        snapshot_task = next(
            item
            for item in snapshot["tasks"]
            if item["task_id"] == task["task_id"]
        )
        self.assertEqual(snapshot_task["title"], "Before concurrent write")
        self.assertEqual(snapshot["revision"], initial_revision)
        self.assertNotEqual(self.team.dashboard_revision(), initial_revision)

        next_snapshot = self.team.dashboard_snapshot()
        next_task = next(
            item
            for item in next_snapshot["tasks"]
            if item["task_id"] == task["task_id"]
        )
        self.assertEqual(next_task["title"], "After concurrent write")
        self.assertEqual(
            next_snapshot["revision"],
            self.team.dashboard_revision(),
        )

    def test_summary_and_board_share_revision_but_validate_derived_data(
        self,
    ) -> None:
        task = self.create_read_task(1, title="Contextual ETag fixture")
        project_id = str(self.project["project_id"])
        path = f"?project_id={project_id}"
        summary_status, summary_body, summary_headers = self.request(
            f"/api/summary{path}"
        )
        board_status, board_body, board_headers = self.request(
            f"/api/board{path}"
        )
        self.assertEqual(summary_status, 200)
        self.assertEqual(board_status, 200)
        summary = json.loads(summary_body)
        board = json.loads(board_body)
        self.assertEqual(summary["revision"], board["revision"])
        self.assertNotEqual(summary_headers["ETag"], board_headers["ETag"])
        self.assertEqual(
            self.request(
                f"/api/summary{path}",
                headers={"If-None-Match": summary_headers["ETag"]},
            )[0],
            304,
        )
        self.assertEqual(
            self.request(
                f"/api/board{path}",
                headers={"If-None-Match": board_headers["ETag"]},
            )[0],
            304,
        )

        _, global_body, global_headers = self.request("/api/summary")
        self.assertNotEqual(
            json.loads(global_body)["revision"],
            summary["revision"],
        )
        self.assertNotEqual(global_headers["ETag"], summary_headers["ETag"])
        _, archived_body, archived_headers = self.request(
            f"/api/summary{path}&include_archived=1"
        )
        self.assertNotEqual(
            json.loads(archived_body)["revision"],
            summary["revision"],
        )
        self.assertNotEqual(archived_headers["ETag"], summary_headers["ETag"])

        with self.team.connection() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET title = ?, updated_at = updated_at
                WHERE task_id = ?
                """,
                ("Same-second HTTP revision", task["task_id"]),
            )
        changed_status, changed_body, changed_headers = self.request(
            f"/api/summary{path}",
            headers={"If-None-Match": summary_headers["ETag"]},
        )
        self.assertEqual(changed_status, 200)
        self.assertNotEqual(changed_headers["ETag"], summary_headers["ETag"])
        self.assertNotEqual(
            json.loads(changed_body)["revision"],
            summary["revision"],
        )
        self.assertEqual(
            self.request(
                f"/api/summary{path}",
                headers={"If-None-Match": changed_headers["ETag"]},
            )[0],
            304,
        )
        changed_board_status, changed_board_body, changed_board_headers = (
            self.request(
                f"/api/board{path}",
                headers={"If-None-Match": board_headers["ETag"]},
            )
        )
        self.assertEqual(changed_board_status, 200)
        self.assertNotEqual(changed_board_headers["ETag"], board_headers["ETag"])
        self.assertNotEqual(changed_board_headers["ETag"], summary_headers["ETag"])
        self.assertEqual(
            json.loads(changed_board_body)["revision"],
            json.loads(changed_body)["revision"],
        )

    def test_partition_limits_and_event_cursor_without_duplicates(
        self,
    ) -> None:
        tasks = [self.create_read_task(index) for index in range(130)]
        event_task = tasks[0]
        claimed = self.team.claim_task(
            event_task["task_id"],
            actor="cursor-test-reader",
        )
        claim_token = claimed["claim_token"]
        for _ in range(40):
            self.team.heartbeat(
                event_task["task_id"],
                actor="cursor-test-reader",
                claim_token=claim_token,
            )

        status, first_tasks = self.json_request("/api/tasks?limit=25")
        self.assertEqual(status, 200)
        self.assertEqual(len(first_tasks["items"]), 25)
        self.assertTrue(first_tasks["has_more"])
        self.assertTrue(first_tasks["next_cursor"])
        status, second_tasks = self.json_request(
            f"/api/tasks?limit=25&cursor={first_tasks['next_cursor']}"
        )
        self.assertEqual(status, 200)
        first_ids = {item["task_id"] for item in first_tasks["items"]}
        second_ids = {item["task_id"] for item in second_tasks["items"]}
        self.assertFalse(first_ids & second_ids)
        self.assertEqual(
            self.json_request("/api/tasks?limit=501")[0],
            400,
        )

        status, first_events = self.json_request(
            f"/api/tasks/{event_task['task_id']}/events?limit=20"
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(first_events["events"]), 20)
        self.assertTrue(first_events["has_more"])
        status, second_events = self.json_request(
            f"/api/tasks/{event_task['task_id']}/events"
            f"?limit=20&cursor={first_events['next_cursor']}"
        )
        self.assertEqual(status, 200)
        first_event_ids = {
            event["event_id"] for event in first_events["events"]
        }
        second_event_ids = {
            event["event_id"] for event in second_events["events"]
        }
        self.assertEqual(len(first_event_ids), 20)
        self.assertFalse(first_event_ids & second_event_ids)

        status, global_first = self.json_request("/api/events?limit=25")
        self.assertEqual(status, 200)
        self.assertEqual(len(global_first["events"]), 25)
        status, global_second = self.json_request(
            f"/api/events?limit=25&cursor={global_first['next_cursor']}"
        )
        self.assertEqual(status, 200)
        self.assertFalse(
            {
                event["event_id"] for event in global_first["events"]
            }
            & {
                event["event_id"] for event in global_second["events"]
            }
        )

        board_started = time.perf_counter()
        _, board_body, _ = self.request("/api/board")
        board_elapsed = time.perf_counter() - board_started
        summary_started = time.perf_counter()
        _, summary_body, _ = self.request("/api/summary")
        summary_elapsed = time.perf_counter() - summary_started
        self.assertGreater(len(board_body), len(summary_body) * 4)
        self.assertLess(summary_elapsed, 5.0)
        self.assertLess(board_elapsed, 20.0)


if __name__ == "__main__":
    unittest.main()
