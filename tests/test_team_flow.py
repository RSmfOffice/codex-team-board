from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import unittest
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from codex_team.config import Settings
from codex_team.cli import build_parser
from codex_team.cluster import ClusterManager
from codex_team.dashboard import build_server
from codex_team.mcp import TeamMcpServer, _tool_definitions
from codex_team.service import TeamError, TeamService
from codex_team.worktrees import WorktreeManager


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


class TeamFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        git(self.repo, "init")
        git(self.repo, "config", "user.email", "team-test@example.invalid")
        git(self.repo, "config", "user.name", "Team Test")
        (self.repo / "README.md").write_text("# Test\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "initial")
        self.settings = Settings(
            project_root=Path(__file__).resolve().parents[1],
            home=self.base / "team-home",
            db_path=self.base / "team-home" / "team.sqlite",
            logs_dir=self.base / "team-home" / "logs",
        )
        self.team = TeamService(self.settings)
        self.worktrees = WorktreeManager(self.team)
        self.team.init_project(self.repo)
        self.assertTrue(
            (self.repo / ".codex" / "team-roles" / "team-worker.md").is_file()
        )
        self.assertEqual(
            self.team.role_prompt("team_worker", self.repo)["scope"],
            "project",
        )
        git(self.repo, "add", ".codex", "AGENTS.md")
        git(self.repo, "commit", "-m", "initialize team policy")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_cli_submit_accepts_commit_sha(self) -> None:
        args = build_parser().parse_args(
            [
                "task",
                "submit",
                "T-TEST",
                "--actor",
                "worker",
                "--token",
                "private-token",
                "--result",
                "result",
                "--tests",
                "tests",
                "--commit",
                "abc1234",
            ]
        )
        self.assertEqual(args.commit, "abc1234")

    def test_write_task_requires_worktree_and_full_review_flow(self) -> None:
        task = self.team.create_task(
            self.repo,
            title="Add greeting",
            description="Create a greeting file.",
            acceptance="greeting.txt contains hello and tests pass",
            kind="write",
            owner_role="team_worker",
        )
        with self.assertRaisesRegex(TeamError, "requires an existing worktree"):
            self.team.claim_task(task["task_id"], actor="worker-1")

        worktree = self.worktrees.create(
            task["task_id"],
            repo=self.repo,
            worktree_path=self.base / "isolated-worktree",
        )
        self.assertTrue(Path(worktree["worktree_path"]).is_dir())
        self.assertTrue(worktree["branch"].startswith("codex/team/"))
        self.assertTrue(worktree["target_branch"])

        claimed = self.team.claim_task(task["task_id"], actor="worker-1")
        token = claimed["claim_token"]
        isolated = Path(worktree["worktree_path"])
        (isolated / "greeting.txt").write_text("hello\n", encoding="utf-8")
        git(isolated, "add", "greeting.txt")
        git(isolated, "commit", "-m", "add greeting")
        worker_commit = git(isolated, "rev-parse", "HEAD")

        updated = self.team.update_task(
            task["task_id"],
            actor="worker-1",
            claim_token=token,
            note="implemented greeting",
            tests="manual content check passed",
        )
        self.assertEqual(updated["status"], "running")
        submitted = self.team.submit_task(
            task["task_id"],
            actor="worker-1",
            claim_token=token,
            result="greeting.txt created",
            tests="manual content check passed",
            commit_sha=worker_commit,
        )
        self.assertEqual(submitted["status"], "review")

        with self.assertRaisesRegex(TeamError, "dirty"):
            (isolated / "untracked.txt").write_text("dirty\n", encoding="utf-8")
            self.team.review_task(
                task["task_id"],
                actor="lead",
                approve=True,
                notes="looks good",
                merge_commit="placeholder",
            )
        (isolated / "untracked.txt").unlink()

        git(self.repo, "merge", "--no-ff", worktree["branch"], "-m", "merge greeting task")
        merge_commit = git(self.repo, "rev-parse", "HEAD")
        git(
            self.repo,
            "switch",
            "-c",
            "unrelated-check",
            worktree["base_commit"],
        )
        done = self.team.review_task(
            task["task_id"],
            actor="lead",
            approve=True,
            notes="accepted after tests",
            merge_commit=merge_commit,
        )
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["merge_commit"], merge_commit)
        dashboard = self.team.dashboard_snapshot()
        self.assertTrue(
            any(item["task_id"] == done["task_id"] for item in dashboard["tasks"])
        )
        self.assertGreaterEqual(dashboard["counts"].get("done", 0), 1)

        with self.team.connection(write=False) as connection:
            events = connection.execute(
                "SELECT event_type FROM task_events WHERE task_id = ? ORDER BY event_id",
                (task["task_id"],),
            ).fetchall()
            schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
        event_types = [row["event_type"] for row in events]
        self.assertIn("worktree_created", event_types)
        self.assertIn("claimed", event_types)
        self.assertIn("submitted", event_types)
        self.assertIn("reviewed", event_types)
        self.assertEqual(schema_version, 7)
        history = self.team.get_task_events(done["task_id"])
        self.assertIn("worktree_created", [item["event_type"] for item in history["events"]])

    def test_dependencies_block_claim(self) -> None:
        first = self.team.create_task(
            self.repo,
            title="Research",
            description="Read only",
            acceptance="summary",
            kind="read",
            owner_role="team_explorer",
        )
        second = self.team.create_task(
            self.repo,
            title="Implement",
            description="Write after research",
            acceptance="working code",
            kind="read",
            owner_role="team_worker",
            depends_on=[first["task_id"]],
        )
        with self.assertRaisesRegex(TeamError, "blocked by"):
            self.team.claim_task(second["task_id"], actor="worker")

        claimed = self.team.claim_task(first["task_id"], actor="explorer")
        self.team.submit_task(
            first["task_id"],
            actor="explorer",
            claim_token=claimed["claim_token"],
            result="research complete",
            tests="not applicable",
        )
        self.team.review_task(
            first["task_id"],
            actor="lead",
            approve=True,
            notes="accepted",
        )
        claimed_second = self.team.claim_task(second["task_id"], actor="worker")
        self.assertEqual(claimed_second["status"], "running")

    def test_project_initialization_is_idempotent_and_documents_handoff(self) -> None:
        agents_path = self.repo / "AGENTS.md"
        worker_prompt = self.repo / ".codex" / "team-roles" / "team-worker.md"
        before = agents_path.read_text(encoding="utf-8")
        configured = self.team.init_project(self.repo, max_threads=2)
        git(self.repo, "add", "AGENTS.md", ".codex/team-project.json")
        git(self.repo, "commit", "-m", "set project thread limit")
        worker_prompt.write_bytes(b"stale managed role\r\n")
        self.team.init_project(self.repo)
        after = agents_path.read_text(encoding="utf-8")
        marker = json.loads(
            (self.repo / ".codex" / "team-project.json").read_text(encoding="utf-8")
        )
        self.assertEqual(configured["max_threads"], 2)
        self.assertEqual(marker["max_threads"], 2)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertNotIn(b"\r\n", worker_prompt.read_bytes())
        self.assertNotIn(b"\r\n", agents_path.read_bytes())
        self.assertEqual(after.count("CODEX_TEAM_BOARD:START"), 1)
        self.assertEqual(after.count("CODEX_TEAM_BOARD:END"), 1)
        self.assertIn("推荐最大并发线程数：2", after)
        self.assertIn("team_get_task_events", after)
        self.assertIn("主 checkout 必须干净", after)
        self.assertNotEqual(before, "")
        self.assertIn("Review 重点", worker_prompt.read_text(encoding="utf-8"))

    def test_worktree_creation_rejects_dirty_main_checkout(self) -> None:
        task = self.team.create_task(
            self.repo,
            title="Dirty checkout guard",
            description="Do not create worktree from an uncommitted base.",
            acceptance="worktree creation rejected",
            kind="write",
            owner_role="team_worker",
        )
        (self.repo / "README.md").write_text("# Dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(TeamError, "uncommitted changes"):
            self.worktrees.create(
                task["task_id"],
                repo=self.repo,
                worktree_path=self.base / "dirty-worktree",
            )

    def test_project_archive_hides_data_and_blocks_writes(self) -> None:
        project = self.team.register_project(self.repo)
        task = self.team.create_task(
            self.repo,
            title="Archived task",
            description="Retained after archiving.",
            acceptance="can be restored",
            kind="read",
            owner_role="team_explorer",
        )
        archived = self.team.set_project_archived(project["project_id"], True)
        self.assertTrue(archived["archived_at"])

        active = self.team.dashboard_snapshot()
        visible = self.team.dashboard_snapshot(include_archived=True)
        self.assertFalse(
            any(item["task_id"] == task["task_id"] for item in active["tasks"])
        )
        self.assertTrue(
            any(item["task_id"] == task["task_id"] for item in visible["tasks"])
        )
        self.assertNotIn(
            project["project_id"],
            {item["project_id"] for item in active["projects"]},
        )
        self.assertIn(
            project["project_id"],
            {item["project_id"] for item in visible["projects"]},
        )
        with self.assertRaisesRegex(TeamError, "project is archived"):
            self.team.create_task(
                self.repo,
                title="Blocked after archive",
                description="Must be rejected.",
                acceptance="rejected",
                kind="read",
                owner_role="team_explorer",
            )

        restored = self.team.set_project_archived(project["project_id"], False)
        self.assertFalse(restored["archived_at"])

    def test_archive_rejects_running_task(self) -> None:
        project = self.team.register_project(self.repo)
        task = self.team.create_task(
            self.repo,
            title="Running task",
            description="Archive must wait.",
            acceptance="not archived",
            kind="read",
            owner_role="team_explorer",
        )
        self.team.claim_task(task["task_id"], actor="explorer")
        with self.assertRaisesRegex(TeamError, "running task"):
            self.team.set_project_archived(project["project_id"], True)

    def test_cancel_stale_task_preserves_audit_and_rejects_running(self) -> None:
        stale = self.team.create_task(
            self.repo,
            title="Stale reviewer",
            description="Superseded task.",
            acceptance="cancelled with reason",
            kind="review",
            owner_role="team_reviewer",
        )
        cancelled = self.team.cancel_task(
            stale["task_id"],
            actor="lead",
            reason="superseded by a fresh reviewer task",
        )
        self.assertEqual(cancelled["status"], "canceled")
        self.assertIn("superseded", cancelled["blocker"])
        history = self.team.get_task_events(stale["task_id"])
        self.assertIn("canceled", [event["event_type"] for event in history["events"]])

        running = self.team.create_task(
            self.repo,
            title="Running cancellation",
            description="Must block before cancellation.",
            acceptance="cancel rejected",
            kind="read",
            owner_role="team_explorer",
        )
        self.team.claim_task(running["task_id"], actor="explorer")
        with self.assertRaisesRegex(TeamError, "must be submitted or blocked"):
            self.team.cancel_task(
                running["task_id"],
                actor="lead",
                reason="stale",
            )

    def test_task_archive_hides_restores_and_blocks_claim(self) -> None:
        task = self.team.create_task(
            self.repo,
            title="Task archive",
            description="Hide this task without deleting it.",
            acceptance="task can be restored",
            kind="read",
            owner_role="team_explorer",
        )
        archived = self.team.set_task_archived(task["task_id"], True)
        self.assertTrue(archived["archived_at"])

        active = self.team.dashboard_snapshot()
        visible = self.team.dashboard_snapshot(include_archived=True)
        self.assertFalse(any(item["task_id"] == task["task_id"] for item in active["tasks"]))
        self.assertTrue(any(item["task_id"] == task["task_id"] for item in visible["tasks"]))
        with self.assertRaisesRegex(TeamError, "task is archived"):
            self.team.claim_task(task["task_id"], actor="explorer")

        restored = self.team.set_task_archived(task["task_id"], False)
        self.assertFalse(restored["archived_at"])
        self.assertIn(
            "archived",
            [
                event["event_type"]
                for event in self.team.get_task_events(task["task_id"])["events"]
            ],
        )
        self.assertIn(
            "restored",
            [
                event["event_type"]
                for event in self.team.get_task_events(task["task_id"])["events"]
            ],
        )

    def test_task_archive_rejects_active_goal(self) -> None:
        manager = ClusterManager(
            self.settings,
            team=self.team,
            worktrees=self.worktrees,
        )
        goal = manager.create_goal(
            self.repo,
            objective="Active goal guard",
            autonomy="supervised",
        )
        task = self.team.create_task(
            self.repo,
            title="Goal task",
            description="Must stay visible while goal is active.",
            acceptance="archive rejected",
            kind="read",
            owner_role="team_explorer",
            goal_id=goal["goal_id"],
        )
        with self.assertRaisesRegex(TeamError, "unfinished goal"):
            self.team.set_task_archived(task["task_id"], True)

    def test_dashboard_api(self) -> None:
        task = self.team.create_task(
            self.repo,
            title="Dashboard task",
            description="Visible task",
            acceptance="appears",
            kind="review",
            owner_role="team_reviewer",
        )
        server = build_server(self.settings, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/favicon.ico",
                timeout=5,
            ) as response:
                self.assertEqual(response.status, 204)
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/board",
                timeout=5,
            ) as response:
                data = json.loads(response.read().decode("utf-8"))
            self.assertEqual(len(data["tasks"]), 1)
            self.assertEqual(data["tasks"][0]["title"], "Dashboard task")
            self.assertIsNone(data["tasks"][0]["active_job"])
            self.assertIsInstance(data["tasks"][0]["claim_wait_seconds"], int)
            self.assertIn(
                data["tasks"][0]["claim_wait_state"],
                {"normal", "warning", "critical"},
            )
            self.assertEqual(len(data["events"]), 1)
            self.assertEqual(data["events"][0]["event_type"], "created")

            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/tasks/archive",
                data=json.dumps(
                    {"task_id": task["task_id"], "archived": True}
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                archived_task = json.loads(response.read().decode("utf-8"))
            self.assertTrue(archived_task["task"]["archived_at"])

            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/board",
                timeout=5,
            ) as response:
                hidden_task = json.loads(response.read().decode("utf-8"))
            self.assertEqual(hidden_task["tasks"], [])

            restore_request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/tasks/archive",
                data=json.dumps(
                    {"task_id": task["task_id"], "archived": False}
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(restore_request, timeout=5) as response:
                restored_task = json.loads(response.read().decode("utf-8"))
            self.assertFalse(restored_task["task"]["archived_at"])

            project = self.team.register_project(self.repo)
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/projects/archive",
                data=json.dumps(
                    {"project_id": project["project_id"], "archived": True}
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                archived = json.loads(response.read().decode("utf-8"))
            self.assertTrue(archived["project"]["archived_at"])

            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/board",
                timeout=5,
            ) as response:
                hidden = json.loads(response.read().decode("utf-8"))
            self.assertEqual(hidden["tasks"], [])

            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/board?include_archived=1",
                timeout=5,
            ) as response:
                visible = json.loads(response.read().decode("utf-8"))
            self.assertEqual(len(visible["tasks"]), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_dashboard_cluster_goal_includes_project_name(self) -> None:
        manager = ClusterManager(
            self.settings,
            team=self.team,
            worktrees=self.worktrees,
        )
        goal = manager.create_goal(
            self.repo,
            objective="Dashboard cluster goal",
            autonomy="supervised",
        )
        dashboard = self.team.dashboard_snapshot()
        saved = next(
            item
            for item in dashboard["cluster_goals"]
            if item["goal_id"] == goal["goal_id"]
        )
        self.assertEqual(saved["project_name"], self.repo.name)

    def test_dashboard_flags_old_ready_task_for_claim_followup(self) -> None:
        task = self.team.create_task(
            self.repo,
            title="Old ready task",
            description="Shows claim delay.",
            acceptance="dashboard flags delay",
            kind="review",
            owner_role="team_reviewer",
        )
        with self.team.connection() as connection:
            connection.execute(
                "UPDATE tasks SET created_at = ? WHERE task_id = ?",
                ("2000-01-01T00:00:00Z", task["task_id"]),
            )
        saved = next(
            item
            for item in self.team.dashboard_snapshot()["tasks"]
            if item["task_id"] == task["task_id"]
        )
        self.assertEqual(saved["claim_wait_state"], "critical")
        self.assertGreaterEqual(saved["claim_wait_seconds"], 300)

    def test_dependency_blocked_task_does_not_trigger_claim_warning(self) -> None:
        dependency = self.team.create_task(
            self.repo,
            title="Dependency",
            description="Must finish first.",
            acceptance="dependency completes",
            kind="read",
            owner_role="team_explorer",
        )
        blocked = self.team.create_task(
            self.repo,
            title="Dependency blocked",
            description="Must not be treated as unclaimed.",
            acceptance="wait state remains normal",
            kind="review",
            owner_role="team_reviewer",
            depends_on=[dependency["task_id"]],
        )
        with self.team.connection() as connection:
            connection.execute(
                "UPDATE tasks SET created_at = ? WHERE task_id = ?",
                ("2000-01-01T00:00:00Z", blocked["task_id"]),
            )
        saved = next(
            item
            for item in self.team.dashboard_snapshot()["tasks"]
            if item["task_id"] == blocked["task_id"]
        )
        self.assertEqual(len(saved["blocked_by"]), 1)
        self.assertEqual(saved["claim_wait_seconds"], 0)
        self.assertEqual(saved["claim_wait_state"], "normal")

    def test_dashboard_cluster_summary_and_attention_thresholds(self) -> None:
        manager = ClusterManager(
            self.settings,
            team=self.team,
            worktrees=self.worktrees,
        )
        active_goal = manager.create_goal(
            self.repo,
            objective="Summarize active goal health",
            autonomy="auto",
        )
        manager.set_goal_status(active_goal["goal_id"], "active")
        project = self.team.register_project(self.repo)
        now = datetime.now(UTC)
        with self.team.connection() as connection:
            for goal_id, status in (
                ("G-PAUSED-TEST", "paused"),
                ("G-FAILED-TEST", "failed"),
            ):
                connection.execute(
                    """
                    INSERT INTO cluster_goals(
                        goal_id, project_id, objective, status, autonomy,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'auto', ?, ?)
                    """,
                    (
                        goal_id,
                        project["project_id"],
                        status,
                        status,
                        now.isoformat().replace("+00:00", "Z"),
                        now.isoformat().replace("+00:00", "Z"),
                    ),
                )

        critical_task = self.team.create_task(
            self.repo,
            title="Critical ready task",
            description="Waits longer than the critical threshold.",
            acceptance="dashboard marks it critical",
            kind="read",
            owner_role="team_explorer",
            goal_id=active_goal["goal_id"],
        )
        warning_task = self.team.create_task(
            self.repo,
            title="Warning ready task",
            description="Waits longer than the warning threshold.",
            acceptance="dashboard marks it warning",
            kind="read",
            owner_role="team_explorer",
            goal_id=active_goal["goal_id"],
        )
        running_task = self.team.create_task(
            self.repo,
            title="Running task",
            description="Provides a running task count.",
            acceptance="dashboard counts it",
            kind="read",
            owner_role="team_explorer",
            goal_id=active_goal["goal_id"],
        )
        review_task = self.team.create_task(
            self.repo,
            title="Review task",
            description="Provides a review task count.",
            acceptance="dashboard counts it",
            kind="read",
            owner_role="team_explorer",
            goal_id=active_goal["goal_id"],
        )
        blocked_task = self.team.create_task(
            self.repo,
            title="Blocked task",
            description="Provides a blocked task count.",
            acceptance="dashboard counts it",
            kind="read",
            owner_role="team_explorer",
            goal_id=active_goal["goal_id"],
        )
        with self.team.connection() as connection:
            connection.execute(
                "UPDATE tasks SET created_at = ? WHERE task_id = ?",
                (
                    (now - timedelta(seconds=360))
                    .isoformat()
                    .replace("+00:00", "Z"),
                    critical_task["task_id"],
                ),
            )
            connection.execute(
                "UPDATE tasks SET created_at = ? WHERE task_id = ?",
                (
                    (now - timedelta(seconds=90))
                    .isoformat()
                    .replace("+00:00", "Z"),
                    warning_task["task_id"],
                ),
            )
            connection.execute(
                """
                INSERT INTO cluster_jobs(
                    job_id, goal_id, task_id, kind, role, status,
                    log_path, output_path, started_at
                ) VALUES (?, ?, ?, 'worker', 'team_explorer', 'running', ?, ?, ?)
                """,
                (
                    "J-ACTIVE-TEST",
                    active_goal["goal_id"],
                    running_task["task_id"],
                    str(self.base / "active.log"),
                    str(self.base / "active.out"),
                    now.isoformat().replace("+00:00", "Z"),
                ),
            )
        self.team.claim_task(running_task["task_id"], actor="runner")
        claimed_review = self.team.claim_task(
            review_task["task_id"],
            actor="reviewer",
        )
        self.team.submit_task(
            review_task["task_id"],
            actor="reviewer",
            claim_token=claimed_review["claim_token"],
            result="ready for review",
            tests="manual dashboard fixture",
        )
        self.team.block_task(
            blocked_task["task_id"],
            actor="worker",
            reason="waiting for input",
        )

        dashboard = self.team.dashboard_snapshot()
        summary = dashboard["cluster_summary"]
        self.assertEqual(summary["goals_total"], 3)
        self.assertEqual(summary["goals_active"], 1)
        self.assertEqual(summary["goals_paused"], 1)
        self.assertEqual(summary["goals_failed"], 1)
        self.assertEqual(summary["ready_tasks"], 2)
        self.assertEqual(summary["running_tasks"], 1)
        self.assertEqual(summary["review_tasks"], 1)
        self.assertEqual(summary["blocked_tasks"], 1)
        self.assertEqual(summary["active_jobs"], 1)
        self.assertEqual(summary["attention_count"], 5)
        self.assertGreaterEqual(summary["oldest_claim_wait_seconds"], 360)

        tasks_by_id = {
            task["task_id"]: task for task in dashboard["tasks"]
        }
        self.assertEqual(
            tasks_by_id[critical_task["task_id"]]["claim_wait_state"],
            "critical",
        )
        self.assertEqual(
            tasks_by_id[warning_task["task_id"]]["claim_wait_state"],
            "warning",
        )
        goals_by_id = {
            goal["goal_id"]: goal for goal in dashboard["cluster_goals"]
        }
        active = goals_by_id[active_goal["goal_id"]]
        self.assertEqual(active["health"], "critical")
        self.assertEqual(active["next_action"], "resolve_blocked_tasks")
        self.assertEqual(active["attention_task_count"], 3)
        self.assertEqual(goals_by_id["G-PAUSED-TEST"]["health"], "warning")
        self.assertEqual(
            goals_by_id["G-PAUSED-TEST"]["next_action"],
            "resume_or_cancel_goal",
        )
        self.assertEqual(goals_by_id["G-PAUSED-TEST"]["attention_task_count"], 0)
        self.assertEqual(goals_by_id["G-FAILED-TEST"]["health"], "critical")
        self.assertEqual(
            goals_by_id["G-FAILED-TEST"]["next_action"],
            "inspect_failed_goal",
        )

    def test_mcp_handoff_flow(self) -> None:
        create_tool = next(
            item
            for item in _tool_definitions()
            if item["name"] == "team_create_task"
        )
        priority_schema = create_tool["inputSchema"]["properties"]["priority"]
        self.assertEqual(priority_schema["minimum"], 0)
        self.assertEqual(priority_schema["maximum"], 3)
        self.assertIn("0 is highest", priority_schema["description"])
        submit_tool = next(
            item
            for item in _tool_definitions()
            if item["name"] == "team_submit_task"
        )
        self.assertIn("commit_sha", submit_tool["inputSchema"]["properties"])
        mcp = TeamMcpServer(self.settings)
        task = mcp.call_tool(
            "team_create_task",
            {
                "repo_root": str(self.repo),
                "title": "MCP handoff",
                "description": "Verify the MCP tool sequence.",
                "acceptance": "task reaches done",
                "kind": "read",
                "owner_role": "team_explorer",
            },
        )
        claimed = mcp.call_tool(
            "team_claim_task",
            {"task_id": task["task_id"], "actor": "explorer"},
        )
        token = claimed["claim_token"]
        mcp.call_tool(
            "team_update_task",
            {
                "task_id": task["task_id"],
                "actor": "explorer",
                "claim_token": token,
                "note": "evidence collected",
            },
        )
        submitted = mcp.call_tool(
            "team_submit_task",
            {
                "task_id": task["task_id"],
                "actor": "explorer",
                "claim_token": token,
                "result": "files: a.py; behavior: mapped; risk: none; review: verify",
                "tests": "manual evidence review",
            },
        )
        self.assertEqual(submitted["status"], "review")
        done = mcp.call_tool(
            "team_review_task",
            {
                "task_id": task["task_id"],
                "actor": "lead",
                "approve": True,
                "notes": "accepted",
            },
        )
        self.assertEqual(done["status"], "done")
        history = mcp.call_tool(
            "team_get_task_events",
            {"task_id": task["task_id"]},
        )
        self.assertIn("claimed", [event["event_type"] for event in history["events"]])

        stale = mcp.call_tool(
            "team_create_task",
            {
                "repo_root": str(self.repo),
                "title": "MCP stale task",
                "description": "Cancelled through MCP.",
                "acceptance": "cancelled",
                "kind": "review",
                "owner_role": "team_reviewer",
            },
        )
        canceled = mcp.call_tool(
            "team_cancel_task",
            {
                "task_id": stale["task_id"],
                "actor": "lead",
                "reason": "superseded",
            },
        )
        self.assertEqual(canceled["status"], "canceled")

    def test_concurrent_claim_has_one_winner_and_expired_lease_can_move(self) -> None:
        task = self.team.create_task(
            self.repo,
            title="Claim race",
            description="Only one worker may own this task.",
            acceptance="single owner",
            kind="read",
            owner_role="team_explorer",
        )
        barrier = threading.Barrier(2)

        def claim(actor: str) -> tuple[str, str]:
            barrier.wait()
            try:
                self.team.claim_task(task["task_id"], actor=actor)
                return actor, "ok"
            except Exception as exc:
                return actor, str(exc)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim, ["explorer-a", "explorer-b"]))
        self.assertEqual(sum(1 for _, result in results if result == "ok"), 1)

        with self.team.connection() as connection:
            connection.execute(
                "UPDATE tasks SET lease_until = ? WHERE task_id = ?",
                ("2000-01-01T00:00:00Z", task["task_id"]),
            )
        reclaimed = self.team.claim_task(task["task_id"], actor="explorer-c")
        self.assertEqual(reclaimed["claimed_by"], "explorer-c")
        self.assertEqual(reclaimed["status"], "running")


if __name__ == "__main__":
    unittest.main()
