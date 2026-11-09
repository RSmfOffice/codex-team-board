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
from unittest.mock import patch

from codex_team.config import Settings
from codex_team.cli import build_parser
from codex_team.cluster import ClusterManager
from codex_team.dashboard import build_server
from codex_team.db import SCHEMA_VERSION, utc_now
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
        ensure_args = build_parser().parse_args(
            [
                "cluster",
                "ensure",
                "--repo",
                ".",
                "--objective",
                "Automatic cluster gate",
                "--no-daemon",
            ]
        )
        self.assertEqual(ensure_args.cluster_command, "ensure")
        self.assertTrue(ensure_args.no_daemon)
        archive_args = build_parser().parse_args(
            ["cluster", "archive", "G-EXAMPLE"]
        )
        self.assertEqual(archive_args.cluster_command, "archive")

    def test_create_task_persists_validated_continuity_mode(self) -> None:
        default_task = self.team.create_task(
            self.repo,
            title="Default continuity",
            description="Use the default continuity policy.",
            acceptance="auto is persisted",
            kind="read",
            owner_role="team_explorer",
        )
        fresh_task = self.team.create_task(
            self.repo,
            title="Fresh continuity",
            description="Force a fresh Agent thread.",
            acceptance="fresh is persisted",
            kind="write",
            owner_role="team_worker",
            continuity_mode=" FRESH ",
        )
        reuse_task = self.team.create_task(
            self.repo,
            title="Reuse continuity",
            description="Reuse the current Agent thread.",
            acceptance="reuse is persisted",
            kind="read",
            owner_role="team_explorer",
            continuity_mode="reuse",
        )

        self.assertEqual(default_task["continuity_mode"], "auto")
        self.assertEqual(fresh_task["continuity_mode"], "fresh")
        self.assertEqual(reuse_task["continuity_mode"], "reuse")
        events = self.team.get_task_events(fresh_task["task_id"])["events"]
        self.assertEqual(events[0]["data"]["continuity_mode"], "fresh")
        with self.assertRaisesRegex(
            TeamError,
            "continuity_mode must be auto, reuse, or fresh",
        ):
            self.team.create_task(
                self.repo,
                title="Invalid continuity",
                description="Reject unsupported modes.",
                acceptance="validation error",
                kind="read",
                owner_role="team_explorer",
                continuity_mode="isolated",
            )
        with self.team.connection(write=False) as connection:
            modes = {
                row["task_id"]: row["continuity_mode"]
                for row in connection.execute(
                    """
                    SELECT task_id, continuity_mode
                    FROM tasks
                    WHERE task_id IN (?, ?, ?)
                    """,
                    (
                        default_task["task_id"],
                        fresh_task["task_id"],
                        reuse_task["task_id"],
                    ),
                )
            }
        self.assertEqual(
            modes,
            {
                default_task["task_id"]: "auto",
                fresh_task["task_id"]: "fresh",
                reuse_task["task_id"]: "reuse",
            },
        )

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
        self.assertEqual(schema_version, SCHEMA_VERSION)
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
        self.assertIn("team_goal_set_checkpoint", after)
        self.assertIn("Explorer 的 read 任务和 Reviewer", after)
        self.assertIn("Worker 的 write 任务默认 fresh", after)
        self.assertIn('team_read(operation="team_list_tasks")', after)
        self.assertIn("team_get_task_events", after)
        self.assertIn("主 checkout 必须干净", after)
        self.assertNotEqual(before, "")
        self.assertIn("Review 重点", worker_prompt.read_text(encoding="utf-8"))
        reviewer_prompt = (
            self.repo / ".codex" / "team-roles" / "team-reviewer.md"
        ).read_text(encoding="utf-8")
        explorer_prompt = (
            self.repo / ".codex" / "team-roles" / "team-explorer.md"
        ).read_text(encoding="utf-8")
        self.assertIn("team_heartbeat", reviewer_prompt)
        self.assertIn("team_heartbeat", explorer_prompt)
        self.assertIn(
            'team_write(operation="team_claim_task"',
            reviewer_prompt,
        )
        self.assertIn(
            'team_read(operation="team_get_task_events"',
            reviewer_prompt,
        )
        self.assertIn(
            'team_write(operation="team_submit_task"',
            explorer_prompt,
        )

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
                f"http://127.0.0.1:{port}/api/health",
                timeout=5,
            ) as response:
                health = json.loads(response.read().decode("utf-8"))
            self.assertEqual(
                Path(health["team_home"]).resolve(),
                self.settings.home.resolve(),
            )
            self.assertEqual(
                Path(health["database"]).resolve(),
                self.settings.db_path.resolve(),
            )
            self.assertTrue(health["database_id"])
            self.assertEqual(health["schema_version"], SCHEMA_VERSION)
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

    def test_dashboard_snapshot_exposes_agent_and_job_token_diagnostics(
        self,
    ) -> None:
        manager = ClusterManager(
            self.settings,
            team=self.team,
            worktrees=self.worktrees,
        )
        goal = manager.create_goal(
            self.repo,
            objective="Inspect Agent continuity",
            autonomy="auto",
        )
        task = self.team.create_task(
            self.repo,
            title="Inspect token usage",
            description="Expose Agent diagnostics through the board.",
            acceptance="snapshot fields are available",
            kind="read",
            owner_role="team_explorer",
            goal_id=goal["goal_id"],
        )
        self.team.claim_task(task["task_id"], actor="agent-diagnostics")
        now = "2026-11-07T11:00:00Z"
        with self.team.connection() as connection:
            connection.execute(
                """
                INSERT INTO cluster_jobs(
                    job_id, goal_id, task_id, kind, role, status, pid,
                    thread_id, attempt, log_path, output_path, started_at,
                    turn_id, runtime, input_tokens, cached_input_tokens,
                    uncached_input_tokens, output_tokens, context_tokens,
                    model_context_window
                ) VALUES (
                    'J-AGENT-DIAG', ?, ?, 'worker', 'team_explorer', 'running',
                    4321, 'thread-diag', 1, ?, ?, ?, 'turn-diag', 'exec',
                    800, 600, 200, 120, 4096, 128000
                )
                """,
                (
                    goal["goal_id"],
                    task["task_id"],
                    str(self.base / "diagnostic.log"),
                    str(self.base / "diagnostic.out"),
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO cluster_agents(
                    agent_id, project_id, role, workstream_key, display_name,
                    thread_id, status, model, model_provider, current_goal_id,
                    current_task_id, current_job_id, last_turn_id,
                    model_context_window, last_input_tokens,
                    last_cached_input_tokens, last_uncached_input_tokens,
                    total_input_tokens, total_cached_input_tokens,
                    total_uncached_input_tokens, context_tokens, turn_count,
                    last_used_at, created_at, updated_at
                ) VALUES (
                    'A-AGENT-DIAG', ?, 'team_explorer', 'diagnostics',
                    'Diagnostic Explorer', 'thread-diag', 'loaded',
                    'test-model', 'test-provider', ?, ?, 'J-AGENT-DIAG',
                    'turn-diag', 128000, 400, 300, 100, 2000, 1500, 500,
                    4096, 7, ?, ?, ?
                )
                """,
                (
                    task["project_id"],
                    goal["goal_id"],
                    task["task_id"],
                    now,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO cluster_agent_events(
                    agent_id, event_type, actor, data_json, created_at
                ) VALUES (
                    'A-AGENT-DIAG', 'tokens_updated', 'app_server',
                    '{"turn_id": "turn-diag"}', ?
                )
                """,
                (now,),
            )

        dashboard = self.team.dashboard_snapshot(task["project_id"])
        agent = next(
            item
            for item in dashboard["cluster_agents"]
            if item["agent_id"] == "A-AGENT-DIAG"
        )
        self.assertEqual(agent["status"], "loaded")
        self.assertEqual(agent["current_goal_id"], goal["goal_id"])
        self.assertEqual(agent["current_task_id"], task["task_id"])
        self.assertEqual(agent["current_task_title"], "Inspect token usage")
        self.assertEqual(agent["current_goal_objective"], "Inspect Agent continuity")
        self.assertEqual(agent["thread_id"], "thread-diag")
        self.assertEqual(agent["model"], "test-model")
        self.assertEqual(agent["model_provider"], "test-provider")
        self.assertEqual(agent["model_context_window"], 128000)
        self.assertEqual(agent["context_tokens"], 4096)
        self.assertEqual(agent["last_cached_input_tokens"], 300)
        self.assertEqual(agent["last_uncached_input_tokens"], 100)
        self.assertEqual(agent["total_cached_input_tokens"], 1500)
        self.assertEqual(agent["total_uncached_input_tokens"], 500)
        self.assertEqual(agent["cache_hit_rate"], 0.75)
        self.assertEqual(agent["turn_count"], 7)
        self.assertEqual(agent["last_used_at"], now)
        self.assertIsNone(agent["reset_requested_at"])

        event = dashboard["cluster_agent_events"][-1]
        self.assertEqual(event["agent_id"], "A-AGENT-DIAG")
        self.assertEqual(event["event_type"], "tokens_updated")
        self.assertEqual(event["data"], {"turn_id": "turn-diag"})

        job = next(
            item
            for item in dashboard["cluster_jobs"]
            if item["job_id"] == "J-AGENT-DIAG"
        )
        self.assertEqual(job["thread_id"], "thread-diag")
        self.assertEqual(job["turn_id"], "turn-diag")
        self.assertEqual(job["input_tokens"], 800)
        self.assertEqual(job["cached_input_tokens"], 600)
        self.assertEqual(job["uncached_input_tokens"], 200)
        self.assertEqual(job["output_tokens"], 120)
        self.assertEqual(job["context_tokens"], 4096)
        self.assertEqual(job["model_context_window"], 128000)
        self.assertEqual(job["cache_hit_rate"], 0.75)

        snapshot_task = next(
            item
            for item in dashboard["tasks"]
            if item["task_id"] == task["task_id"]
        )
        active_job = snapshot_task["active_job"]
        self.assertEqual(active_job["thread_id"], "thread-diag")
        self.assertEqual(active_job["turn_id"], "turn-diag")
        self.assertEqual(active_job["cached_input_tokens"], 600)
        self.assertEqual(active_job["uncached_input_tokens"], 200)
        self.assertEqual(active_job["cache_hit_rate"], 0.75)

    def test_dashboard_projects_missing_usage_fields_to_null(self) -> None:
        manager = ClusterManager(
            self.settings,
            team=self.team,
            worktrees=self.worktrees,
        )
        goal = manager.create_goal(
            self.repo,
            objective="Project partial usage field unknowns",
        )
        task = self.team.create_task(
            self.repo,
            title="Partial usage diagnostics",
            description="Only input and context were observed.",
            acceptance="Unknown token fields render as null.",
            kind="read",
            owner_role="team_explorer",
            goal_id=goal["goal_id"],
        )
        now = utc_now()
        missing_fields = [
            "cached_input_tokens",
            "uncached_input_tokens",
            "output_tokens",
            "first_cached_input_tokens",
            "first_uncached_input_tokens",
            "cache_write_input_tokens",
            "reasoning_output_tokens",
            "model_context_window",
        ]
        with self.team.connection() as connection:
            connection.execute(
                """
                INSERT INTO cluster_jobs(
                    job_id, goal_id, task_id, kind, role, status, pid,
                    thread_id, attempt, log_path, output_path, started_at,
                    turn_id, runtime, input_tokens, cached_input_tokens,
                    uncached_input_tokens, output_tokens, first_input_tokens,
                    first_cached_input_tokens, first_uncached_input_tokens,
                    context_tokens, cache_write_input_tokens,
                    reasoning_output_tokens, model_context_window,
                    usage_source, telemetry_complete, usage_missing_fields
                ) VALUES (
                    'J-PARTIAL-USAGE', ?, ?, 'worker', 'team_explorer',
                    'running', 4322, 'thread-partial', 1, ?, ?, ?,
                    'turn-partial', 'app_server', 37, 0, 0, 0, 37, 0, 0,
                    0, 0, 0, NULL, 'app_server.token_usage', 0, ?
                )
                """,
                (
                    goal["goal_id"],
                    task["task_id"],
                    str(self.base / "partial.log"),
                    str(self.base / "partial.out"),
                    now,
                    json.dumps(missing_fields),
                ),
            )
            connection.execute(
                """
                INSERT INTO cluster_jobs(
                    job_id, goal_id, kind, role, status,
                    log_path, output_path, started_at
                ) VALUES (
                    'J-LEGACY-UNKNOWN-USAGE', ?, 'worker', 'team_explorer',
                    'completed', ?, ?, ?
                )
                """,
                (
                    goal["goal_id"],
                    str(self.base / "legacy.log"),
                    str(self.base / "legacy.out"),
                    now,
                ),
            )

        dashboard = self.team.dashboard_snapshot(task["project_id"])
        job = next(
            item
            for item in dashboard["cluster_jobs"]
            if item["job_id"] == "J-PARTIAL-USAGE"
        )
        self.assertEqual(job["input_tokens"], 37)
        self.assertEqual(job["first_input_tokens"], 37)
        self.assertEqual(job["context_tokens"], 0)
        for field in missing_fields:
            self.assertIsNone(job[field], field)
        self.assertIsNone(job["cache_hit_rate"])

        snapshot_task = next(
            item
            for item in dashboard["tasks"]
            if item["task_id"] == task["task_id"]
        )
        active_job = snapshot_task["active_job"]
        self.assertEqual(active_job["input_tokens"], 37)
        self.assertIsNone(active_job["cached_input_tokens"])
        self.assertIsNone(active_job["uncached_input_tokens"])
        self.assertIsNone(active_job["output_tokens"])
        self.assertIsNone(active_job["cache_hit_rate"])

        legacy_job = next(
            item
            for item in dashboard["cluster_jobs"]
            if item["job_id"] == "J-LEGACY-UNKNOWN-USAGE"
        )
        self.assertEqual(legacy_job["usage_missing_fields"], "[]")
        self.assertIsNone(legacy_job["input_tokens"])
        self.assertIsNone(legacy_job["cached_input_tokens"])
        self.assertIsNone(legacy_job["output_tokens"])

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
        self.assertEqual(saved["active_job_count"], 0)
        self.assertEqual(saved["heartbeat_state"], "idle")
        self.assertEqual(saved["heartbeat_age_seconds"], 0)
        self.assertEqual(saved["queue_reason"], "waiting_for_lead")
        self.assertGreaterEqual(saved["estimated_wait_seconds"], 0)
        summary = dashboard["cluster_summary"]
        self.assertEqual(summary["global_jobs"], 0)
        self.assertGreaterEqual(summary["global_capacity"], 1)
        self.assertEqual(summary["heartbeat_overdue"], 0)
        self.assertEqual(summary["oldest_heartbeat_age_seconds"], 0)
        self.assertEqual(summary["fairness_wait_count"], 0)

    def test_cluster_goal_archive_hides_restores_and_rejects_unfinished(self) -> None:
        manager = ClusterManager(
            self.settings,
            team=self.team,
            worktrees=self.worktrees,
        )
        goal = manager.create_goal(
            self.repo,
            objective="Archive a completed cluster goal",
            autonomy="auto",
        )
        manager.set_goal_status(goal["goal_id"], "active")
        with self.assertRaisesRegex(TeamError, "only completed or failed"):
            manager.set_goal_archived(goal["goal_id"], True)
        manager.set_goal_status(goal["goal_id"], "completed")

        cascaded = self.team.create_task(
            self.repo,
            title="Cascade completed task",
            description="Archived with its goal.",
            acceptance="archived by goal",
            kind="read",
            owner_role="team_explorer",
            goal_id=goal["goal_id"],
        )
        manually_archived = self.team.create_task(
            self.repo,
            title="Individually archived task",
            description="Keeps its independent archive state.",
            acceptance="not restored by goal",
            kind="read",
            owner_role="team_explorer",
            goal_id=goal["goal_id"],
        )
        canceled = self.team.create_task(
            self.repo,
            title="Canceled task",
            description="Canceled tasks cascade with their goal.",
            acceptance="archived and restored by goal",
            kind="read",
            owner_role="team_explorer",
            goal_id=goal["goal_id"],
        )
        blocked = self.team.create_task(
            self.repo,
            title="Blocked task",
            description="Blocked tasks cascade with their goal.",
            acceptance="hidden from active board",
            kind="read",
            owner_role="team_explorer",
            goal_id=goal["goal_id"],
        )
        now = utc_now()
        with self.team.connection() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET status = 'done', completed_at = ?, updated_at = ?,
                    version = version + 1
                WHERE task_id IN (?, ?)
                """,
                (now, now, cascaded["task_id"], manually_archived["task_id"]),
            )
            connection.execute(
                """
                UPDATE tasks
                SET status = 'canceled', completed_at = ?, updated_at = ?,
                    version = version + 1
                WHERE task_id = ?
                """,
                (now, now, canceled["task_id"]),
            )
            connection.execute(
                """
                UPDATE tasks
                SET status = 'blocked', blocker = 'fixture blocker',
                    updated_at = ?, version = version + 1
                WHERE task_id = ?
                """,
                (now, blocked["task_id"]),
            )
        self.team.set_task_archived(manually_archived["task_id"], True)
        persistent_agent = manager.agent_pool.register_thread(
            goal["project_id"],
            "team_explorer",
            goal["workstream_key"],
            "goal-archive-persistent-thread",
            manager.model,
            manager.model_provider,
            "Goal archive persistent explorer",
        )

        archived = manager.set_goal_archived(goal["goal_id"], True)
        self.assertTrue(archived["archived_at"])
        cascaded_archived = self.team.get_task(cascaded["task_id"])
        self.assertIsNotNone(cascaded_archived["archived_at"])
        self.assertEqual(cascaded_archived["archived_by_goal"], 1)
        self.assertEqual(
            self.team.get_task(manually_archived["task_id"])["archived_by_goal"],
            0,
        )
        for task_id in (canceled["task_id"], blocked["task_id"]):
            task_after_archive = self.team.get_task(task_id)
            self.assertIsNotNone(task_after_archive["archived_at"])
            self.assertEqual(task_after_archive["archived_by_goal"], 1)
        goal_detail = manager.get_goal(goal["goal_id"])
        self.assertIn(
            cascaded["task_id"],
            {task["task_id"] for task in goal_detail["tasks"]},
        )
        with self.team.connection() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET archived_at = NULL, updated_at = ?, version = version + 1
                WHERE task_id = ?
                """,
                (utc_now(), blocked["task_id"]),
            )
        active = self.team.dashboard_snapshot()
        visible = self.team.dashboard_snapshot(include_archived=True)
        self.assertNotIn(
            goal["goal_id"],
            {item["goal_id"] for item in active["cluster_goals"]},
        )
        self.assertIn(
            goal["goal_id"],
            {item["goal_id"] for item in visible["cluster_goals"]},
        )
        self.assertEqual(active["cluster_summary"]["blocked_tasks"], 0)
        active_task_ids = {
            task["task_id"] for task in active["tasks"]
        }
        visible_task_ids = {
            task["task_id"] for task in visible["tasks"]
        }
        self.assertNotIn(blocked["task_id"], active_task_ids)
        self.assertIn(blocked["task_id"], visible_task_ids)
        mcp = TeamMcpServer(self.settings)
        visible_tasks = mcp.call_tool(
            "team_list_tasks",
            {
                "repo_root": str(self.repo),
                "goal_id": goal["goal_id"],
                "include_archived": False,
            },
        )["tasks"]
        archived_tasks = mcp.call_tool(
            "team_list_tasks",
            {
                "repo_root": str(self.repo),
                "goal_id": goal["goal_id"],
                "include_archived": True,
            },
        )["tasks"]
        self.assertNotIn(
            cascaded["task_id"],
            {task["task_id"] for task in visible_tasks},
        )
        self.assertNotIn(
            blocked["task_id"],
            {task["task_id"] for task in visible_tasks},
        )
        self.assertIn(
            cascaded["task_id"],
            {task["task_id"] for task in archived_tasks},
        )
        self.assertIn(
            blocked["task_id"],
            {task["task_id"] for task in archived_tasks},
        )
        agent_after_archive = next(
            agent
            for agent in manager.list_agents(goal["project_id"])
            if agent["agent_id"] == persistent_agent["agent_id"]
        )
        self.assertIsNone(agent_after_archive["archived_at"])
        self.assertEqual(agent_after_archive["status"], "idle")
        with self.assertRaisesRegex(TeamError, "restore it first"):
            manager.set_goal_status(goal["goal_id"], "paused")

        restored = manager.set_goal_archived(goal["goal_id"], False)
        self.assertFalse(restored["archived_at"])
        cascaded_restored = self.team.get_task(cascaded["task_id"])
        self.assertIsNone(cascaded_restored["archived_at"])
        self.assertEqual(cascaded_restored["archived_by_goal"], 0)
        manual_after_restore = self.team.get_task(manually_archived["task_id"])
        self.assertIsNotNone(manual_after_restore["archived_at"])
        self.assertEqual(manual_after_restore["archived_by_goal"], 0)
        for task_id in (canceled["task_id"], blocked["task_id"]):
            restored_task = self.team.get_task(task_id)
            self.assertIsNone(restored_task["archived_at"])
            self.assertEqual(restored_task["archived_by_goal"], 0)
        cascade_events = [
            event["event_type"]
            for event in self.team.get_task_events(cascaded["task_id"])["events"]
        ]
        self.assertIn("archived_by_goal", cascade_events)
        self.assertIn("restored_by_goal", cascade_events)
        event_types = [
            event["event_type"]
            for event in manager.get_goal(goal["goal_id"])["events"]
        ]
        self.assertIn("goal_archived", event_types)
        self.assertIn("goal_restored", event_types)

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

    def test_archived_failed_goal_is_historical_not_actionable(self) -> None:
        manager = ClusterManager(
            self.settings,
            team=self.team,
            worktrees=self.worktrees,
        )
        goal = manager.create_goal(
            self.repo,
            objective="Keep an archived failure for audit only",
            autonomy="auto",
        )
        manager.set_goal_status(goal["goal_id"], "failed", reason="historical failure")
        task = self.team.create_task(
            self.repo,
            title="Blocked historical task",
            description="Must not remain actionable after its goal is archived.",
            acceptance="visible only as history",
            kind="read",
            owner_role="team_explorer",
            goal_id=goal["goal_id"],
        )
        self.team.block_task(
            task["task_id"],
            actor="worker",
            reason="preserved for audit",
        )

        manager.set_goal_archived(goal["goal_id"], True)
        active = self.team.dashboard_snapshot()
        archived = self.team.dashboard_snapshot(include_archived=True)

        self.assertNotIn(
            goal["goal_id"],
            {item["goal_id"] for item in active["cluster_goals"]},
        )
        archived_goal = next(
            item
            for item in archived["cluster_goals"]
            if item["goal_id"] == goal["goal_id"]
        )
        self.assertEqual(archived_goal["status"], "failed")
        self.assertEqual(archived_goal["health"], "archived")
        self.assertEqual(archived_goal["next_action"], "none")
        self.assertEqual(archived_goal["attention_task_count"], 0)
        self.assertEqual(archived_goal["queue_reason"], "none")
        self.assertEqual(archived["cluster_summary"]["goals_failed"], 1)
        self.assertEqual(archived["cluster_summary"]["attention_count"], 0)

    def test_archived_project_tasks_are_historical_without_task_archive(self) -> None:
        manager = ClusterManager(
            self.settings,
            team=self.team,
            worktrees=self.worktrees,
        )
        project = self.team.register_project(self.repo)
        goal = manager.create_goal(
            self.repo,
            objective="Archive a failed project as history",
            autonomy="auto",
        )
        manager.set_goal_status(goal["goal_id"], "failed", reason="historical failure")
        task = self.team.create_task(
            self.repo,
            title="Legacy blocked task",
            description="Its task archive flag stays null when the project is archived.",
            acceptance="historical task is not actionable",
            kind="read",
            owner_role="team_explorer",
            goal_id=goal["goal_id"],
        )
        self.team.block_task(
            task["task_id"],
            actor="worker",
            reason="preserved for audit",
        )

        self.team.set_project_archived(project["project_id"], True)
        self.assertIsNone(self.team.get_task(task["task_id"])["archived_at"])
        self.assertIsNone(manager.get_goal(goal["goal_id"])["archived_at"])
        active = self.team.dashboard_snapshot()
        archived = self.team.dashboard_snapshot(include_archived=True)

        self.assertNotIn(
            goal["goal_id"],
            {item["goal_id"] for item in active["cluster_goals"]},
        )
        archived_goal = next(
            item
            for item in archived["cluster_goals"]
            if item["goal_id"] == goal["goal_id"]
        )
        self.assertEqual(archived_goal["status"], "failed")
        self.assertEqual(archived_goal["health"], "archived")
        self.assertEqual(archived_goal["next_action"], "none")
        self.assertEqual(archived_goal["attention_task_count"], 0)
        self.assertEqual(archived["cluster_summary"]["attention_count"], 0)

    def test_dashboard_cluster_capacity_queue_and_heartbeat_diagnostics(self) -> None:
        manager = ClusterManager(
            self.settings,
            team=self.team,
            worktrees=self.worktrees,
        )
        first_goal = manager.create_goal(
            self.repo,
            objective="First concurrent goal",
            autonomy="auto",
        )
        manager.set_goal_status(first_goal["goal_id"], "active")
        second_repo = self.base / "second-repo"
        second_repo.mkdir()
        git(second_repo, "init")
        git(second_repo, "config", "user.email", "team-test@example.invalid")
        git(second_repo, "config", "user.name", "Team Test")
        (second_repo / "README.md").write_text("# Second\n", encoding="utf-8")
        git(second_repo, "add", "README.md")
        git(second_repo, "commit", "-m", "initial")
        second_goal = manager.create_goal(
            second_repo,
            objective="Second concurrent goal",
            autonomy="auto",
        )
        manager.set_goal_status(second_goal["goal_id"], "active")

        critical_task = self.team.create_task(
            self.repo,
            title="Critical worker",
            description="Uses an old claimed heartbeat.",
            acceptance="reported critical",
            kind="read",
            owner_role="team_explorer",
            goal_id=first_goal["goal_id"],
        )
        warning_task = self.team.create_task(
            second_repo,
            title="Warning worker",
            description="Uses an old progress heartbeat.",
            acceptance="reported warning",
            kind="read",
            owner_role="team_explorer",
            goal_id=second_goal["goal_id"],
        )
        claimed_critical = self.team.claim_task(
            critical_task["task_id"],
            actor="critical-worker",
        )
        claimed_warning = self.team.claim_task(
            warning_task["task_id"],
            actor="warning-worker",
        )
        self.assertTrue(claimed_critical["claim_token"])
        self.assertTrue(claimed_warning["claim_token"])

        now = datetime.now(UTC)
        critical_activity = (now - timedelta(seconds=1200)).isoformat().replace(
            "+00:00",
            "Z",
        )
        warning_activity = (now - timedelta(seconds=420)).isoformat().replace(
            "+00:00",
            "Z",
        )
        queue_activity = (now - timedelta(seconds=600)).isoformat().replace(
            "+00:00",
            "Z",
        )
        with self.team.connection() as connection:
            connection.execute(
                """
                UPDATE task_events SET created_at = ?
                WHERE task_id = ? AND event_type = 'claimed'
                """,
                (critical_activity, critical_task["task_id"]),
            )
            connection.execute(
                """
                UPDATE task_events SET created_at = ?
                WHERE task_id = ? AND event_type = 'claimed'
                """,
                (critical_activity, warning_task["task_id"]),
            )
            connection.execute(
                """
                INSERT INTO task_events(
                    task_id, event_type, actor, data_json, created_at
                ) VALUES (?, 'progress', 'warning-worker', '{}', ?)
                """,
                (warning_task["task_id"], warning_activity),
            )
            job_rows = (
                (
                    "J-CRITICAL-WORKER",
                    first_goal["goal_id"],
                    critical_task["task_id"],
                    "worker",
                    "team_explorer",
                    (now - timedelta(seconds=1300))
                    .isoformat()
                    .replace("+00:00", "Z"),
                ),
                (
                    "J-WARNING-WORKER",
                    second_goal["goal_id"],
                    warning_task["task_id"],
                    "worker",
                    "team_explorer",
                    (now - timedelta(seconds=500))
                    .isoformat()
                    .replace("+00:00", "Z"),
                ),
                (
                    "J-LEAD-NORMAL",
                    second_goal["goal_id"],
                    None,
                    "lead",
                    "lead",
                    (now - timedelta(seconds=180))
                    .isoformat()
                    .replace("+00:00", "Z"),
                ),
            )
            for job_id, goal_id, task_id, kind, role, started_at in job_rows:
                connection.execute(
                    """
                    INSERT INTO cluster_jobs(
                        job_id, goal_id, task_id, kind, role, status,
                        log_path, output_path, started_at
                    ) VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?)
                    """,
                    (
                        job_id,
                        goal_id,
                        task_id,
                        kind,
                        role,
                        str(self.base / f"{job_id}.log"),
                        str(self.base / f"{job_id}.out"),
                        started_at,
                    ),
                )

        queue_task = self.team.create_task(
            second_repo,
            title="Queued task",
            description="Changes waiting reason.",
            acceptance="reason is reported",
            kind="read",
            owner_role="team_explorer",
            goal_id=second_goal["goal_id"],
        )
        with self.team.connection() as connection:
            connection.execute(
                """
                UPDATE tasks SET created_at = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (queue_activity, queue_activity, queue_task["task_id"]),
            )

        heartbeat_environment = {
            "CODEX_TEAM_CLUSTER_MAX_JOBS": "4",
            "CODEX_TEAM_CLUSTER_MAX_WORKERS": "3",
            "CODEX_TEAM_HEARTBEAT_WARNING_SECONDS": "300",
            "CODEX_TEAM_HEARTBEAT_CRITICAL_SECONDS": "900",
        }
        allowed_heartbeat_states = {"idle", "normal", "warning", "critical"}
        allowed_queue_reasons = {
            "running",
            "review_pending",
            "blocked",
            "ready",
            "dependency_wait",
            "global_capacity",
            "goal_capacity",
            "waiting_for_lead",
            "none",
        }
        with patch.dict("os.environ", heartbeat_environment):
            for task_status, expected_reason in (
                ("review", "review_pending"),
                ("blocked", "blocked"),
                ("ready", "ready"),
            ):
                with self.team.connection() as connection:
                    connection.execute(
                        """
                        UPDATE tasks SET status = ?, updated_at = ?
                        WHERE task_id = ?
                        """,
                        (task_status, queue_activity, queue_task["task_id"]),
                    )
                dashboard = self.team.dashboard_snapshot()
                summary = dashboard["cluster_summary"]
                self.assertEqual(summary["active_jobs"], 3)
                self.assertEqual(summary["global_jobs"], 2)
                self.assertEqual(summary["global_capacity"], 6)
                self.assertEqual(summary["worker_capacity_per_goal"], 3)
                self.assertEqual(summary["system_jobs"], 3)
                self.assertEqual(summary["system_capacity"], 4)
                self.assertEqual(summary["heartbeat_overdue"], 2)
                self.assertGreaterEqual(
                    summary["oldest_heartbeat_age_seconds"],
                    1200,
                )
                self.assertEqual(summary["fairness_wait_count"], 0)
                self.assertEqual(summary["queued_goals"], 1)
                for field, minimum in (
                    ("global_jobs", 0),
                    ("global_capacity", 1),
                    ("worker_capacity_per_goal", 1),
                    ("system_capacity", 1),
                    ("queued_goals", 0),
                    ("heartbeat_overdue", 0),
                    ("oldest_heartbeat_age_seconds", 0),
                    ("fairness_wait_count", 0),
                ):
                    self.assertIsInstance(summary[field], int)
                    self.assertGreaterEqual(summary[field], minimum)
                goals = {
                    goal["goal_id"]: goal for goal in dashboard["cluster_goals"]
                }
                first = goals[first_goal["goal_id"]]
                second = goals[second_goal["goal_id"]]
                self.assertEqual(first["active_job_count"], 1)
                self.assertEqual(first["heartbeat_state"], "critical")
                self.assertGreaterEqual(first["heartbeat_age_seconds"], 1200)
                self.assertEqual(first["queue_reason"], "running")
                self.assertEqual(second["active_job_count"], 2)
                self.assertEqual(second["heartbeat_state"], "warning")
                self.assertGreaterEqual(second["heartbeat_age_seconds"], 420)
                self.assertEqual(second["queue_reason"], expected_reason)
                self.assertGreaterEqual(second["estimated_wait_seconds"], 590)
                for goal in dashboard["cluster_goals"]:
                    self.assertIn(
                        goal["heartbeat_state"],
                        allowed_heartbeat_states,
                    )
                    self.assertIn(goal["queue_reason"], allowed_queue_reasons)
                    for field in (
                        "active_job_count",
                        "heartbeat_age_seconds",
                        "estimated_wait_seconds",
                    ):
                        self.assertIsInstance(goal[field], int)
                        self.assertGreaterEqual(goal[field], 0)

            with patch.dict(
                "os.environ",
                {
                    "CODEX_TEAM_CLUSTER_MAX_JOBS": "3",
                    "CODEX_TEAM_CLUSTER_MAX_WORKERS": "1",
                },
            ):
                dashboard = self.team.dashboard_snapshot()
                capacity_goal = next(
                    goal
                    for goal in dashboard["cluster_goals"]
                    if goal["goal_id"] == second_goal["goal_id"]
                )
                self.assertEqual(capacity_goal["queue_reason"], "global_capacity")
                self.assertEqual(
                    dashboard["cluster_summary"]["fairness_wait_count"],
                    1,
                )
                filtered = self.team.dashboard_snapshot(
                    second_goal["project_id"]
                )
                filtered_goal = next(
                    goal
                    for goal in filtered["cluster_goals"]
                    if goal["goal_id"] == second_goal["goal_id"]
                )
                self.assertEqual(
                    filtered_goal["queue_reason"],
                    "global_capacity",
                )
                self.assertEqual(
                    filtered["cluster_summary"]["system_jobs"],
                    3,
                )

            with patch.dict(
                "os.environ",
                {
                    "CODEX_TEAM_CLUSTER_MAX_JOBS": "4",
                    "CODEX_TEAM_CLUSTER_MAX_WORKERS": "1",
                },
            ):
                dashboard = self.team.dashboard_snapshot()
                capacity_goal = next(
                    goal
                    for goal in dashboard["cluster_goals"]
                    if goal["goal_id"] == second_goal["goal_id"]
                )
                self.assertEqual(capacity_goal["queue_reason"], "goal_capacity")
                self.assertEqual(
                    dashboard["cluster_summary"]["fairness_wait_count"],
                    0,
                )

            with patch.dict(
                "os.environ",
                {
                    "CODEX_TEAM_CLUSTER_MAX_JOBS": "999",
                    "CODEX_TEAM_CLUSTER_MAX_WORKERS": "999",
                },
            ):
                clamped = self.team.dashboard_snapshot()
                self.assertEqual(
                    clamped["cluster_summary"]["system_capacity"],
                    32,
                )
                self.assertEqual(
                    clamped["cluster_summary"]["worker_capacity_per_goal"],
                    8,
                )
                self.assertEqual(
                    clamped["cluster_summary"]["global_capacity"],
                    16,
                )

    def test_heartbeat_ignores_events_from_a_previous_attempt(self) -> None:
        manager = ClusterManager(
            self.settings,
            team=self.team,
            worktrees=self.worktrees,
        )
        goal = manager.create_goal(
            self.repo,
            objective="Current attempt heartbeat",
            autonomy="auto",
        )
        manager.set_goal_status(goal["goal_id"], "active")
        task = self.team.create_task(
            self.repo,
            title="Fresh worker attempt",
            description="Old progress must not affect this attempt.",
            acceptance="heartbeat is normal",
            kind="read",
            owner_role="team_explorer",
            goal_id=goal["goal_id"],
        )
        self.team.claim_task(task["task_id"], actor="worker")
        now = datetime.now(UTC)
        with self.team.connection() as connection:
            connection.execute(
                """
                UPDATE task_events SET created_at = ?
                WHERE task_id = ? AND event_type = 'claimed'
                """,
                (
                    (now - timedelta(seconds=1200))
                    .isoformat()
                    .replace("+00:00", "Z"),
                    task["task_id"],
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
                    "J-CURRENT-ATTEMPT",
                    goal["goal_id"],
                    task["task_id"],
                    str(self.base / "current.log"),
                    str(self.base / "current.out"),
                    now.isoformat().replace("+00:00", "Z"),
                ),
            )

        dashboard = self.team.dashboard_snapshot()
        saved = next(
            item
            for item in dashboard["cluster_goals"]
            if item["goal_id"] == goal["goal_id"]
        )
        self.assertEqual(saved["heartbeat_state"], "normal")
        self.assertLess(saved["heartbeat_age_seconds"], 60)
        self.assertEqual(dashboard["cluster_summary"]["heartbeat_overdue"], 0)

    def test_dependency_wait_and_lead_heartbeat_are_not_false_alarms(self) -> None:
        manager = ClusterManager(
            self.settings,
            team=self.team,
            worktrees=self.worktrees,
        )
        goal = manager.create_goal(
            self.repo,
            objective="Dependency and lead diagnostics",
            autonomy="auto",
        )
        manager.set_goal_status(goal["goal_id"], "active")
        blocker = self.team.create_task(
            self.repo,
            title="Backlog blocker",
            description="Keeps the dependent task blocked without an active job.",
            acceptance="dependency remains pending",
            kind="read",
            owner_role="team_explorer",
            goal_id=goal["goal_id"],
        )
        dependent = self.team.create_task(
            self.repo,
            title="Dependency waiter",
            description="Must not be reported as ready for scheduling.",
            acceptance="queue reason is dependency_wait",
            kind="read",
            owner_role="team_explorer",
            goal_id=goal["goal_id"],
            depends_on=[blocker["task_id"]],
        )
        now = datetime.now(UTC)
        with self.team.connection() as connection:
            connection.execute(
                "UPDATE tasks SET status = 'backlog' WHERE task_id = ?",
                (blocker["task_id"],),
            )
            connection.execute(
                """
                UPDATE tasks SET created_at = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (
                    (now - timedelta(seconds=600))
                    .isoformat()
                    .replace("+00:00", "Z"),
                    (now - timedelta(seconds=600))
                    .isoformat()
                    .replace("+00:00", "Z"),
                    dependent["task_id"],
                ),
            )
            connection.execute(
                """
                INSERT INTO cluster_jobs(
                    job_id, goal_id, task_id, kind, role, status,
                    log_path, output_path, started_at
                ) VALUES (?, ?, NULL, 'lead', 'lead', 'running', ?, ?, ?)
                """,
                (
                    "J-OLD-LEAD-NO-HEARTBEAT",
                    goal["goal_id"],
                    str(self.base / "old-lead.log"),
                    str(self.base / "old-lead.out"),
                    (now - timedelta(seconds=3600))
                    .isoformat()
                    .replace("+00:00", "Z"),
                ),
            )

        dashboard = self.team.dashboard_snapshot()
        saved = next(
            item
            for item in dashboard["cluster_goals"]
            if item["goal_id"] == goal["goal_id"]
        )
        self.assertEqual(saved["queue_reason"], "dependency_wait")
        self.assertGreaterEqual(saved["estimated_wait_seconds"], 590)
        self.assertEqual(saved["heartbeat_state"], "normal")
        self.assertEqual(saved["heartbeat_age_seconds"], 0)
        self.assertEqual(dashboard["cluster_summary"]["heartbeat_overdue"], 0)
        self.assertEqual(
            dashboard["cluster_summary"]["oldest_heartbeat_age_seconds"],
            0,
        )
        self.assertEqual(
            dashboard["cluster_summary"]["fairness_wait_count"],
            0,
        )

    def test_mcp_handoff_flow(self) -> None:
        create_tool = next(
            item
            for item in _tool_definitions()
            if item["name"] == "team_create_task"
        )
        ensure_tool = next(
            item
            for item in _tool_definitions()
            if item["name"] == "team_cluster_ensure"
        )
        self.assertIn("repo_root", ensure_tool["inputSchema"]["required"])
        self.assertIn("objective", ensure_tool["inputSchema"]["required"])
        control_tool = next(
            item
            for item in _tool_definitions()
            if item["name"] == "team_cluster_control"
        )
        self.assertIn(
            "archive",
            control_tool["inputSchema"]["properties"]["action"]["enum"],
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
        update_tool = next(
            item
            for item in _tool_definitions()
            if item["name"] == "team_update_task"
        )
        self.assertIn("commit_sha", update_tool["inputSchema"]["properties"])
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

        with patch(
            "codex_team.mcp.ensure_daemon",
            return_value={"running": True, "pid": 1234},
        ):
            ensured = mcp.call_tool(
                "team_cluster_ensure",
                {
                    "repo_root": str(self.repo),
                    "objective": "Automatic team task",
                },
            )
            reused = mcp.call_tool(
                "team_cluster_ensure",
                {
                    "repo_root": str(self.repo),
                    "objective": "Continue automatic team task",
                },
            )
        self.assertTrue(ensured["created"])
        self.assertTrue(reused["reused"])
        self.assertEqual(
            ensured["goal"]["goal_id"],
            reused["goal"]["goal_id"],
        )

    def test_mcp_update_task_persists_commit_sha(self) -> None:
        mcp = TeamMcpServer(self.settings)
        task = mcp.call_tool(
            "team_create_task",
            {
                "repo_root": str(self.repo),
                "title": "MCP update commit",
                "description": "Record a worker commit through MCP progress updates.",
                "acceptance": "commit_sha persists and can be read back",
                "kind": "write",
                "owner_role": "team_worker",
            },
        )
        worktree = self.worktrees.create(
            task["task_id"],
            repo=self.repo,
            worktree_path=self.base / "mcp-update-worktree",
        )
        commit_sha = git(Path(worktree["worktree_path"]), "rev-parse", "HEAD")
        claimed = mcp.call_tool(
            "team_claim_task",
            {"task_id": task["task_id"], "actor": "worker"},
        )
        updated = mcp.call_tool(
            "team_update_task",
            {
                "task_id": task["task_id"],
                "actor": "worker",
                "claim_token": claimed["claim_token"],
                "note": "implementation committed",
                "commit_sha": commit_sha,
            },
        )
        self.assertEqual(updated["status"], "running")
        self.assertEqual(updated["commit_sha"], commit_sha)
        fetched = mcp.call_tool(
            "team_get_task",
            {"task_id": task["task_id"]},
        )
        self.assertEqual(fetched["commit_sha"], commit_sha)

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
