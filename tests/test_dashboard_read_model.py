from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

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
        self.assertTrue(etag.startswith('W/"v1:') and etag.endswith('"'))

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
