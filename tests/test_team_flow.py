from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import types
import unittest
import urllib.request
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from codex_team.cache_contract import canonical_json, sha256_text
from codex_team.config import Settings
from codex_team.cli import build_parser
from codex_team.cluster import ClusterError, ClusterManager
from codex_team.dashboard import build_server
from codex_team.db import SCHEMA_VERSION, utc_now
from codex_team.mcp import TeamMcpServer, _process, _tool_definitions
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

    def _create_write_task_in_review(
        self,
        *,
        name: str,
        worktree_name: str,
    ) -> tuple[dict[str, object], dict[str, object], Path, str]:
        task = self.team.create_task(
            self.repo,
            title=name,
            description="Exercise review integrity.",
            acceptance="Only the submitted commit can be merged.",
            kind="write",
            owner_role="team_worker",
        )
        worktree = self.worktrees.create(
            str(task["task_id"]),
            repo=self.repo,
            worktree_path=self.base / worktree_name,
        )
        claimed = self.team.claim_task(
            str(task["task_id"]),
            actor=f"worker-{name}",
        )
        isolated = Path(str(worktree["worktree_path"]))
        filename = f"{worktree_name}.txt"
        (isolated / filename).write_text(name + "\n", encoding="utf-8")
        git(isolated, "add", filename)
        git(isolated, "commit", "-m", f"work {name}")
        worker_commit = git(isolated, "rev-parse", "HEAD")
        self.team.submit_task(
            str(task["task_id"]),
            actor=f"worker-{name}",
            claim_token=str(claimed["claim_token"]),
            result=f"{filename} created",
            tests="review integrity fixture",
            commit_sha=worker_commit,
        )
        return task, worktree, isolated, worker_commit

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
        update_args = build_parser().parse_args(
            [
                "task",
                "update",
                "T-TEST",
                "--actor",
                "worker",
                "--token",
                "private-token",
                "--note",
                "progress",
                "--commit",
                "def5678",
            ]
        )
        self.assertEqual(update_args.commit, "def5678")
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

    def test_create_task_rejects_terminal_and_archived_goals(self) -> None:
        cluster = ClusterManager(
            self.settings,
            team=self.team,
            runtime_mode="exec",
            agent_command=["codex"],
        )
        completed = cluster.create_goal(
            self.repo,
            objective="Completed goal",
        )
        cluster.set_goal_status(completed["goal_id"], "active")
        cluster.set_goal_status(completed["goal_id"], "completed")
        with self.assertRaisesRegex(TeamError, "terminal"):
            self.team.create_task(
                self.repo,
                title="Must not be orphaned",
                description="Terminal goals cannot accept tasks.",
                acceptance="Creation is rejected.",
                kind="read",
                owner_role="team_explorer",
                goal_id=completed["goal_id"],
            )

        archived = cluster.create_goal(
            self.repo,
            objective="Archived goal",
        )
        cluster.set_goal_status(archived["goal_id"], "active")
        cluster.set_goal_status(archived["goal_id"], "completed")
        cluster.set_goal_archived(archived["goal_id"], True)
        with self.assertRaisesRegex(TeamError, "archived"):
            self.team.create_task(
                self.repo,
                title="Must not target archive",
                description="Archived goals cannot accept tasks.",
                acceptance="Creation is rejected.",
                kind="read",
                owner_role="team_explorer",
                goal_id=archived["goal_id"],
            )

    def test_workflow_epoch_tracks_policy_role_and_environment(self) -> None:
        initial = self.team.current_workflow_epoch(self.repo)
        task = self.team.create_task(
            self.repo,
            title="Epoch-bound task",
            description="Bind to the current model-visible contract.",
            acceptance="The stored epoch matches the current cohort.",
            kind="read",
            owner_role="team_explorer",
        )
        self.assertEqual(task["workflow_epoch"], initial)

        agents_path = self.repo / "AGENTS.md"
        agents_path.write_text(
            agents_path.read_text(encoding="utf-8") + "\n<!-- epoch v2 -->\n",
            encoding="utf-8",
        )
        policy_changed = self.team.current_workflow_epoch(self.repo)
        self.assertNotEqual(policy_changed, initial)

        role_path = self.repo / ".codex" / "team-roles" / "team-worker.md"
        role_path.write_text(
            role_path.read_text(encoding="utf-8") + "\nRole epoch v2.\n",
            encoding="utf-8",
        )
        role_changed = self.team.current_workflow_epoch(self.repo)
        self.assertNotEqual(role_changed, policy_changed)

        with patch.dict(
            os.environ,
            {"CODEX_TEAM_MODEL": "model-epoch-v2"},
            clear=False,
        ):
            model_changed = self.team.current_workflow_epoch(self.repo)
        self.assertNotEqual(model_changed, role_changed)

    def test_old_workflow_epoch_tasks_and_goals_fail_closed(self) -> None:
        manager = ClusterManager(
            self.settings,
            team=self.team,
            worktrees=self.worktrees,
            runtime_mode="exec",
            agent_command=["codex"],
        )
        self.addCleanup(manager.close)
        goal = manager.create_goal(
            self.repo,
            objective="Reject an old workflow cohort",
            autonomy="auto",
        )
        task = self.team.create_task(
            self.repo,
            title="Old cohort task",
            description="Must not dispatch after an epoch change.",
            acceptance="The task is blocked before claim.",
            kind="read",
            owner_role="team_explorer",
            goal_id=goal["goal_id"],
        )
        with self.team.connection() as connection:
            connection.execute(
                "UPDATE cluster_goals SET workflow_epoch = 'legacy-unbound' WHERE goal_id = ?",
                (goal["goal_id"],),
            )
            connection.execute(
                "UPDATE tasks SET workflow_epoch = 'legacy-unbound' WHERE task_id = ?",
                (task["task_id"],),
            )

        with self.assertRaisesRegex(TeamError, "workflow epoch"):
            manager.ensure_goal(
                self.repo,
                objective="Do not reuse an old cohort",
            )
        manager.tick()
        current_task = self.team.get_task(task["task_id"])
        current_goal = manager.get_goal(goal["goal_id"])
        self.assertEqual(current_task["status"], "ready")
        self.assertEqual(current_goal["status"], "paused")
        self.assertIn(
            "workflow_epoch_mismatch",
            [event["event_type"] for event in current_goal["events"]],
        )
        with self.assertRaisesRegex(TeamError, "workflow epoch"):
            manager.set_goal_status(
                goal["goal_id"],
                "active",
                reason="must migrate first",
            )
        with self.assertRaisesRegex(TeamError, "workflow epoch"):
            self.team.create_task(
                self.repo,
                title="Cannot join old goal",
                description="Old goals cannot accept current tasks.",
                acceptance="Creation is rejected.",
                kind="read",
                owner_role="team_explorer",
                goal_id=goal["goal_id"],
            )
        with self.team.connection() as connection:
            connection.execute(
                "UPDATE cluster_goals SET workflow_epoch = ? WHERE goal_id = ?",
                (
                    self.team.current_workflow_epoch(self.repo),
                    goal["goal_id"],
                ),
            )
        manager.set_goal_status(
            goal["goal_id"],
            "active",
            reason="exercise task-level epoch gate",
        )
        manager._dispatch_next_job(
            manager.get_goal(goal["goal_id"]),
            [self.team.get_task(task["task_id"])],
        )
        blocked_task = self.team.get_task(task["task_id"])
        self.assertEqual(blocked_task["status"], "blocked")
        self.assertIn("workflow epoch", blocked_task["blocker"])

    def test_explicit_goal_workflow_epoch_migration(self) -> None:
        manager = ClusterManager(
            self.settings,
            team=self.team,
            worktrees=self.worktrees,
            runtime_mode="exec",
            agent_command=["codex"],
        )
        goal = manager.create_goal(
            self.repo,
            objective="Migrate an explicit workflow cohort",
            autonomy="auto",
        )
        task = self.team.create_task(
            self.repo,
            title="Migrate me explicitly",
            description="Only explicit migration may change the epoch.",
            acceptance="Goal and ready task move together.",
            kind="read",
            owner_role="team_explorer",
            goal_id=goal["goal_id"],
        )
        with self.team.connection() as connection:
            connection.execute(
                "UPDATE cluster_goals SET workflow_epoch = 'legacy-unbound' WHERE goal_id = ?",
                (goal["goal_id"],),
            )
            connection.execute(
                "UPDATE tasks SET workflow_epoch = 'legacy-unbound' WHERE task_id = ?",
                (task["task_id"],),
            )

        with self.assertRaisesRegex(TeamError, "durable checkpoint"):
            manager.migrate_goal_workflow_epoch(
                goal["goal_id"],
                actor="human",
                reason="missing checkpoint",
            )
        manager.set_goal_checkpoint(
            goal["goal_id"],
            summary="Reviewed current prefix and task intent.",
            actor="human",
        )
        migrated = manager.migrate_goal_workflow_epoch(
            goal["goal_id"],
            actor="human",
            reason="accept current stable prefix",
        )
        current = self.team.current_workflow_epoch(self.repo)
        self.assertEqual(migrated["workflow_epoch"], current)
        self.assertEqual(migrated["goal"]["workflow_epoch"], current)
        self.assertEqual(
            self.team.get_task(task["task_id"])["workflow_epoch"],
            current,
        )
        event_types = [
            event["event_type"]
            for event in manager.get_goal(goal["goal_id"])["events"]
        ]
        self.assertIn("workflow_epoch_migrated", event_types)
        with self.team.connection() as connection:
            connection.execute(
                """
                INSERT INTO cluster_jobs(
                    job_id, goal_id, task_id, workflow_epoch, kind, role,
                    actor, status, attempt, log_path, output_path, started_at
                ) VALUES (
                    'J-MIGRATION-ACTIVE', ?, ?, ?, 'worker', 'team_explorer',
                    'migration-test', 'running', 1, 'active.log', 'active.out', ?
                )
                """,
                (
                    goal["goal_id"],
                    task["task_id"],
                    current,
                    utc_now(),
                ),
            )
        with self.assertRaisesRegex(TeamError, "active jobs"):
            manager.migrate_goal_workflow_epoch(
                goal["goal_id"],
                actor="human",
                reason="must wait for active jobs",
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
        self.assertIn(
            "worktree_created",
            [item["event_type"] for item in history["events"]],
        )

    def test_write_claim_requires_owned_branch_and_clean_worktree(self) -> None:
        task = self.team.create_task(
            self.repo,
            title="Assignment boundary branch",
            description="A task must claim only its assigned branch.",
            acceptance="Wrong branch and dirty worktrees are rejected.",
            kind="write",
            owner_role="team_worker",
        )
        worktree = self.worktrees.create(
            task["task_id"],
            repo=self.repo,
            worktree_path=self.base / "assignment-boundary-branch",
        )
        isolated = Path(worktree["worktree_path"])

        git(isolated, "switch", "-c", "codex/team/manual-unassigned")
        with self.assertRaisesRegex(TeamError, "does not match its assignment"):
            self.team.claim_task(task["task_id"], actor="worker-1")
        git(isolated, "switch", worktree["branch"])

        (isolated / "uncommitted.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(TeamError, "must be clean"):
            self.team.claim_task(task["task_id"], actor="worker-1")

    def test_write_claim_rejects_cross_task_worktree_and_main_checkout(self) -> None:
        first = self.team.create_task(
            self.repo,
            title="First worktree owner",
            description="Own the first isolated worktree.",
            acceptance="Ownership is unique.",
            kind="write",
            owner_role="team_worker",
        )
        second = self.team.create_task(
            self.repo,
            title="Second worktree owner",
            description="Must not claim another task's worktree.",
            acceptance="Cross-task assignment is rejected.",
            kind="write",
            owner_role="team_worker",
        )
        first_worktree = self.worktrees.create(
            first["task_id"],
            repo=self.repo,
            worktree_path=self.base / "assignment-owner-a",
        )
        second_worktree = self.worktrees.create(
            second["task_id"],
            repo=self.repo,
            worktree_path=self.base / "assignment-owner-b",
        )

        with self.team.connection() as connection:
            connection.execute(
                "UPDATE tasks SET branch = ? WHERE task_id = ?",
                (first_worktree["branch"], second["task_id"]),
            )
        with self.assertRaisesRegex(TeamError, "branch is already assigned"):
            self.team.claim_task(second["task_id"], actor="worker-2")

        with self.team.connection() as connection:
            connection.execute(
                "UPDATE tasks SET branch = ? WHERE task_id = ?",
                (second_worktree["branch"], second["task_id"]),
            )
            connection.execute(
                "UPDATE tasks SET worktree_path = ? WHERE task_id = ?",
                (first_worktree["worktree_path"], second["task_id"]),
            )
        with self.assertRaisesRegex(TeamError, "worktree is already assigned"):
            self.team.claim_task(second["task_id"], actor="worker-2")

        with self.team.connection() as connection:
            connection.execute(
                "UPDATE tasks SET worktree_path = ?, branch = ? WHERE task_id = ?",
                (
                    str(self.repo.resolve()),
                    "codex/team/manual-main-copy",
                    second["task_id"],
                ),
            )
        with self.assertRaisesRegex(TeamError, "isolated from the main checkout"):
            self.team.claim_task(second["task_id"], actor="worker-2")

    def test_review_rejects_merge_without_submitted_commit(self) -> None:
        task, worktree, _, worker_commit = self._create_write_task_in_review(
            name="unrelated merge",
            worktree_name="unrelated-merge-worktree",
        )
        with self.assertRaisesRegex(TeamError, "not contained"):
            self.team.review_task(
                str(task["task_id"]),
                actor="lead",
                approve=True,
                notes="must reject unrelated target history",
                merge_commit=str(worktree["base_commit"]),
            )
        current = self.team.get_task(str(task["task_id"]))
        self.assertEqual(current["status"], "review")
        self.assertEqual(current["commit_sha"], worker_commit)

    def test_review_persists_target_snapshot_when_ref_moves_after_checks(self) -> None:
        task, worktree, _, _ = self._create_write_task_in_review(
            name="target snapshot",
            worktree_name="target-snapshot-worktree",
        )
        git(
            self.repo,
            "merge",
            "--no-ff",
            str(worktree["branch"]),
            "-m",
            "merge target snapshot task",
        )
        merge_commit = git(self.repo, "rev-parse", "HEAD")
        base_commit = str(worktree["base_commit"])

        checks_finished = threading.Event()
        continue_review = threading.Event()
        original_connection = self.team.connection
        delayed_write = {"entered": False}

        @contextmanager
        def pause_before_review_commit(
            service: TeamService,
            *,
            write: bool = True,
        ):
            if write and not delayed_write["entered"]:
                delayed_write["entered"] = True
                checks_finished.set()
                if not continue_review.wait(timeout=5):
                    raise RuntimeError("timed out waiting to continue review")
            with original_connection(write=write) as connection:
                yield connection

        self.team.connection = types.MethodType(
            pause_before_review_commit,
            self.team,
        )
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                review = executor.submit(
                    self.team.review_task,
                    str(task["task_id"]),
                    actor="lead",
                    approve=True,
                    notes="verified against the target snapshot",
                    merge_commit=merge_commit,
                )
                self.assertTrue(checks_finished.wait(timeout=5))

                git(self.repo, "update-ref", "refs/heads/main", base_commit)
                continue_review.set()
                done = review.result(timeout=5)
        finally:
            continue_review.set()
            self.team.connection = original_connection

        self.assertEqual(done["status"], "done")
        self.assertEqual(done["merge_commit"], merge_commit)
        self.assertEqual(done["review_target_commit"], merge_commit)
        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/main"), base_commit)
        merge_still_on_target = subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "merge-base",
                "--is-ancestor",
                merge_commit,
                "refs/heads/main",
            ],
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(merge_still_on_target.returncode, 0)

        reviewed = next(
            event
            for event in self.team.get_task_events(str(task["task_id"]))["events"]
            if event["event_type"] == "reviewed"
        )
        self.assertEqual(reviewed["data"]["target_branch"], "main")
        self.assertEqual(reviewed["data"]["target_commit"], merge_commit)
        self.assertEqual(reviewed["data"]["merge_commit"], merge_commit)
        dashboard_task = next(
            item
            for item in self.team.dashboard_snapshot()["tasks"]
            if item["task_id"] == task["task_id"]
        )
        self.assertEqual(dashboard_task["review_target_commit"], merge_commit)

    def test_review_rejects_archived_task_and_project(self) -> None:
        task = self.team.create_task(
            self.repo,
            title="Archived review",
            description="Archived records are immutable.",
            acceptance="Review is rejected.",
            kind="read",
            owner_role="team_explorer",
        )
        claimed = self.team.claim_task(
            str(task["task_id"]),
            actor="archive-worker",
        )
        self.team.submit_task(
            str(task["task_id"]),
            actor="archive-worker",
            claim_token=str(claimed["claim_token"]),
            result="ready for review",
            tests="fixture",
        )
        self.team.set_task_archived(str(task["task_id"]), True)
        with self.assertRaisesRegex(TeamError, "archived"):
            self.team.review_task(
                str(task["task_id"]),
                actor="lead",
                approve=True,
                notes="must reject archived task",
            )

        second = self.team.create_task(
            self.repo,
            title="Archived project review",
            description="Project archive blocks review.",
            acceptance="Review is rejected.",
            kind="read",
            owner_role="team_explorer",
        )
        claimed = self.team.claim_task(
            str(second["task_id"]),
            actor="archive-project-worker",
        )
        self.team.submit_task(
            str(second["task_id"]),
            actor="archive-project-worker",
            claim_token=str(claimed["claim_token"]),
            result="ready for project archive",
            tests="fixture",
        )
        project = self.team.get_project(str(second["project_id"]))
        self.team.set_project_archived(str(project["project_id"]), True)
        with self.assertRaisesRegex(TeamError, "project is archived"):
            self.team.review_task(
                str(second["task_id"]),
                actor="lead",
                approve=True,
                notes="must reject archived project",
            )

    def test_review_requires_trusted_managed_authority(self) -> None:
        def submit_read_task(title: str) -> dict[str, object]:
            task = self.team.create_task(
                self.repo,
                title=title,
                description="Authority is bound by the local MCP process.",
                acceptance="Only trusted Lead or human callers may review.",
                kind="read",
                owner_role="team_explorer",
            )
            claimed = self.team.claim_task(
                str(task["task_id"]),
                actor=f"{title}-worker",
            )
            self.team.submit_task(
                str(task["task_id"]),
                actor=f"{title}-worker",
                claim_token=str(claimed["claim_token"]),
                result="ready for review",
                tests="authority fixture",
            )
            return task

        worker_task = submit_read_task("Forged worker review")
        with patch.dict(
            os.environ,
            {
                "CODEX_TEAM_ROLE": "team_worker",
                "CODEX_TEAM_ACTOR": "managed-worker",
            },
            clear=False,
        ):
            with self.assertRaisesRegex(TeamError, "managed role team_worker"):
                self.team.review_task(
                    str(worker_task["task_id"]),
                    actor="lead",
                    approve=True,
                    notes="forged actor must not elevate",
                )
        self.assertEqual(
            self.team.get_task(str(worker_task["task_id"]))["status"],
            "review",
        )

        lead_task = submit_read_task("Managed lead review")
        with patch.dict(
            os.environ,
            {
                "CODEX_TEAM_ROLE": "lead",
                "CODEX_TEAM_ACTOR": "",
            },
            clear=False,
        ):
            reviewed = self.team.review_task(
                str(lead_task["task_id"]),
                actor="lead",
                approve=True,
                notes="trusted managed lead",
            )
        self.assertEqual(reviewed["status"], "done")

        human_task = submit_read_task("Direct human review")
        human_reviewed = self.team.review_task(
            str(human_task["task_id"]),
            actor="human",
            approve=True,
            notes="direct local human path",
        )
        self.assertEqual(human_reviewed["status"], "done")

    def test_mcp_privileged_operations_require_authority_token(self) -> None:
        server = TeamMcpServer(self.settings)

        def reviewable_task(title: str) -> dict[str, object]:
            task = self.team.create_task(
                self.repo,
                title=title,
                description="The public MCP boundary needs authority.",
                acceptance="Actor spoofing is rejected without a token.",
                kind="read",
                owner_role="team_explorer",
            )
            claimed = self.team.claim_task(
                str(task["task_id"]),
                actor=f"{title}-worker",
            )
            self.team.submit_task(
                str(task["task_id"]),
                actor=f"{title}-worker",
                claim_token=str(claimed["claim_token"]),
                result="ready for review",
                tests="authority fixture",
            )
            return task

        review_task = reviewable_task("MCP forged review")
        with patch.dict(
            os.environ,
            {
                "CODEX_TEAM_ROLE": "",
                "CODEX_TEAM_ACTOR": "",
            },
            clear=False,
        ):
            with self.assertRaisesRegex(
                TeamError,
                "local authority token",
            ):
                server.call_tool(
                    "team_review_task",
                    {
                        "task_id": review_task["task_id"],
                        "actor": "lead",
                        "approve": True,
                        "notes": "forged review",
                    },
                )
            reviewed = server.call_tool(
                "team_review_task",
                {
                    "task_id": review_task["task_id"],
                    "actor": "lead",
                    "approve": True,
                    "notes": "authorized review",
                    "authority_token": self.team.operator_authority(),
                },
            )
        self.assertEqual(reviewed["status"], "done")

        block_task = self.team.create_task(
            self.repo,
            title="MCP forged block",
            description="Running claims need authority without a token.",
            acceptance="A forged lead cannot clear another claim.",
            kind="read",
            owner_role="team_explorer",
        )
        claimed = self.team.claim_task(
            str(block_task["task_id"]),
            actor="block-victim",
        )
        with patch.dict(
            os.environ,
            {
                "CODEX_TEAM_ROLE": "",
                "CODEX_TEAM_ACTOR": "",
            },
            clear=False,
        ):
            with self.assertRaisesRegex(
                TeamError,
                "local authority token",
            ):
                server.call_tool(
                    "team_block_task",
                    {
                        "task_id": block_task["task_id"],
                        "actor": "lead",
                        "reason": "forged block",
                    },
                )
            blocked = server.call_tool(
                "team_block_task",
                {
                    "task_id": block_task["task_id"],
                    "actor": "lead",
                    "reason": "authorized block",
                    "authority_token": self.team.operator_authority(),
                },
            )
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(
            self.team.get_task(str(block_task["task_id"]))["claimed_by"],
            None,
        )
        self.assertIsNotNone(claimed["claim_token"])

    def test_concurrent_review_has_one_terminal_winner(self) -> None:
        task = self.team.create_task(
            self.repo,
            title="Concurrent review",
            description="Only one reviewer may transition the task.",
            acceptance="Exactly one review succeeds.",
            kind="read",
            owner_role="team_explorer",
        )
        claimed = self.team.claim_task(
            str(task["task_id"]),
            actor="review-race-worker",
        )
        self.team.submit_task(
            str(task["task_id"]),
            actor="review-race-worker",
            claim_token=str(claimed["claim_token"]),
            result="ready for concurrent review",
            tests="fixture",
        )

        barrier = threading.Barrier(2)
        state_lock = threading.Lock()
        state = {"active": True, "reads": 0}
        original_connection = TeamService.connection

        @contextmanager
        def synchronized_connection(
            service: TeamService,
            *,
            write: bool = True,
        ):
            with original_connection(service, write=write) as connection:
                yield connection
                if not write and state["active"]:
                    with state_lock:
                        state["reads"] += 1
                        read_number = state["reads"]
                    if read_number <= 2:
                        barrier.wait(timeout=5)
                        if read_number == 2:
                            state["active"] = False

        self.team.connection = types.MethodType(
            synchronized_connection,
            self.team,
        )

        def review() -> object:
            try:
                return self.team.review_task(
                    str(task["task_id"]),
                    actor="lead",
                    approve=True,
                    notes="concurrent review",
                )
            except TeamError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(lambda _: review(), range(2)))

        successes = [item for item in outcomes if isinstance(item, dict)]
        failures = [item for item in outcomes if isinstance(item, TeamError)]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual(self.team.get_task(str(task["task_id"]))["status"], "done")

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
        self.assertIn(
            'team_read(operation="team_list_tasks", arguments={...})',
            after,
        )
        self.assertIn(
            'team_read(operation="team_get_task_events"',
            after,
        )
        self.assertIn(
            'team_write(operation="team_goal_set_checkpoint"',
            after,
        )
        self.assertIn(
            'team_write(operation="team_review_task"',
            after,
        )
        self.assertIn(
            'team_agent_register_native',
            after,
        )
        self.assertIn("same-task-only", after)
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
        worker_agent_toml = (
            self.repo / ".codex" / "agents" / "team-worker.toml"
        ).read_text(encoding="utf-8")
        reviewer_agent_toml = (
            self.repo / ".codex" / "agents" / "team-reviewer.toml"
        ).read_text(encoding="utf-8")
        explorer_agent_toml = (
            self.repo / ".codex" / "agents" / "team-explorer.toml"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'team_write(operation="team_claim_task"',
            worker_agent_toml,
        )
        self.assertIn(
            'team_read(operation="team_get_task_events"',
            reviewer_agent_toml,
        )
        self.assertIn(
            'team_write(operation="team_submit_task"',
            explorer_agent_toml,
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

    def test_worktree_intent_recovers_after_database_finalize_failure(self) -> None:
        task = self.team.create_task(
            self.repo,
            title="Recover worktree intent",
            description="The Git worktree exists before DB finalization.",
            acceptance="Recovery attaches the existing worktree to the task.",
            kind="write",
            owner_role="team_worker",
        )
        with patch.object(
            self.worktrees,
            "_finalize_intent",
            side_effect=RuntimeError("simulated database interruption"),
        ):
            with self.assertRaisesRegex(TeamError, "recovery is required"):
                self.worktrees.create(task["task_id"], repo=self.repo)

        self.assertIsNone(self.team.get_task(task["task_id"])["worktree_path"])
        recovered = self.worktrees.recover_pending()
        self.assertEqual(recovered[0]["status"], "completed")
        self.assertEqual(self.worktrees.recover_pending(), [])
        current = self.team.get_task(task["task_id"])
        self.assertTrue(Path(current["worktree_path"]).is_dir())
        with self.team.connection(write=False) as connection:
            operation = connection.execute(
                """
                SELECT status FROM external_operations
                WHERE task_id = ? AND operation_type = 'worktree_create'
                """,
                (task["task_id"],),
            ).fetchone()
            created_events = connection.execute(
                """
                SELECT COUNT(*) FROM task_events
                WHERE task_id = ? AND event_type = 'worktree_created'
                """,
                (task["task_id"],),
            ).fetchone()[0]
        self.assertEqual(operation["status"], "completed")
        self.assertEqual(created_events, 1)

    def test_concurrent_worktree_creation_has_one_intent_winner(self) -> None:
        task = self.team.create_task(
            self.repo,
            title="Concurrent worktree intent",
            description="Only one durable intent may own task provisioning.",
            acceptance="One worktree and one completed operation exist.",
            kind="write",
            owner_role="team_worker",
        )

        def create() -> object:
            try:
                return self.worktrees.create(
                    str(task["task_id"]),
                    repo=self.repo,
                )
            except TeamError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(lambda _: create(), range(2)))
        self.assertEqual(sum(isinstance(item, dict) for item in outcomes), 1)
        self.assertEqual(sum(isinstance(item, TeamError) for item in outcomes), 1)
        current = self.team.get_task(str(task["task_id"]))
        self.assertTrue(Path(str(current["worktree_path"])).is_dir())
        with self.team.connection(write=False) as connection:
            operations = connection.execute(
                """
                SELECT status FROM external_operations
                WHERE task_id = ? AND operation_type = 'worktree_create'
                """,
                (task["task_id"],),
            ).fetchall()
        statuses = [row["status"] for row in operations]
        self.assertEqual(statuses.count("completed"), 1)
        self.assertNotIn("pending", statuses)
        self.assertNotIn("running", statuses)

    def test_concurrent_tasks_serialize_real_git_worktree_creation(self) -> None:
        first_task = self.team.create_task(
            self.repo,
            title="First serialized worktree",
            description="Create a real isolated worktree.",
            acceptance="The first task owns its branch and path.",
            kind="write",
            owner_role="team_worker",
        )
        second_task = self.team.create_task(
            self.repo,
            title="Second serialized worktree",
            description="Wait for repository-level provisioning.",
            acceptance="The second task owns its own branch and path.",
            kind="write",
            owner_role="team_worker",
        )
        first_at_add = threading.Event()
        second_started = threading.Event()
        second_git_seen = threading.Event()
        release_first = threading.Event()
        state_lock = threading.Lock()
        first_thread_id: dict[str, int] = {}
        original_git = WorktreeManager._git

        def observed_git(
            cwd: Path,
            *args: str,
        ) -> subprocess.CompletedProcess[str]:
            is_first_add = False
            if args[:2] == ("worktree", "add"):
                with state_lock:
                    if "id" not in first_thread_id:
                        first_thread_id["id"] = threading.get_ident()
                is_first_add = threading.get_ident() == first_thread_id["id"]
            if is_first_add:
                first_at_add.set()
                if not release_first.wait(timeout=5):
                    raise RuntimeError("timed out waiting to release first worktree")
            elif first_at_add.is_set():
                with state_lock:
                    is_other_worker = (
                        threading.get_ident()
                        != first_thread_id.get("id")
                    )
                if is_other_worker:
                    second_git_seen.set()
            return original_git(cwd, *args)

        def create_task_worktree(task: dict[str, object], suffix: str) -> object:
            try:
                return self.worktrees.create(
                    str(task["task_id"]),
                    repo=self.repo,
                    worktree_path=self.base / f"serialized-{suffix}",
                )
            except TeamError as exc:
                return exc

        with patch.object(
            WorktreeManager,
            "_git",
            staticmethod(observed_git),
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(
                    create_task_worktree,
                    first_task,
                    "first",
                )
                self.assertTrue(first_at_add.wait(timeout=5))

                def create_second() -> object:
                    second_started.set()
                    return create_task_worktree(second_task, "second")

                second = executor.submit(create_second)
                self.assertTrue(second_started.wait(timeout=5))
                try:
                    self.assertFalse(second_git_seen.wait(timeout=0.3))
                finally:
                    release_first.set()
                first_result = first.result(timeout=5)
                second_result = second.result(timeout=5)

        self.assertIsInstance(first_result, dict)
        self.assertIsInstance(second_result, dict)
        first_saved = self.team.get_task(str(first_task["task_id"]))
        second_saved = self.team.get_task(str(second_task["task_id"]))
        self.assertIn(str(first_task["task_id"]).lower(), first_saved["branch"])
        self.assertIn(str(second_task["task_id"]).lower(), second_saved["branch"])
        self.assertNotEqual(first_saved["branch"], second_saved["branch"])
        self.assertNotEqual(
            first_saved["worktree_path"],
            second_saved["worktree_path"],
        )
        self.assertEqual(
            Path(
                git(
                    Path(first_saved["worktree_path"]),
                    "rev-parse",
                    "--show-toplevel",
                )
            ).resolve(),
            Path(first_saved["worktree_path"]).resolve(),
        )
        self.assertEqual(
            Path(
                git(
                    Path(second_saved["worktree_path"]),
                    "rev-parse",
                    "--show-toplevel",
                )
            ).resolve(),
            Path(second_saved["worktree_path"]).resolve(),
        )

    def test_worktree_intent_without_git_result_is_marked_failed(self) -> None:
        task = self.team.create_task(
            self.repo,
            title="Missing worktree result",
            description="A durable intent exists without a Git worktree.",
            acceptance="Recovery records a failed operation.",
            kind="write",
            owner_role="team_worker",
        )
        now = utc_now()
        payload = {
            "repo_root": str(self.repo.resolve()),
            "worktree_path": str((self.base / "missing-worktree").resolve()),
            "branch": "codex/team/missing-worktree",
            "target_branch": "main",
            "base_ref": "HEAD",
            "base_commit": git(self.repo, "rev-parse", "HEAD"),
        }
        with self.team.connection() as connection:
            connection.execute(
                """
                INSERT INTO external_operations(
                    operation_id, operation_type, status, goal_id, task_id,
                    actor, payload_json, created_at, updated_at
                ) VALUES (
                    'OP-MISSING-WORKTREE', 'worktree_create', 'running', ?, ?,
                    'teamd', ?, ?, ?
                )
                """,
                (
                    task.get("goal_id"),
                    task["task_id"],
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                ),
            )
        recovered = self.worktrees.recover_pending()
        self.assertEqual(recovered[0]["status"], "failed")
        with self.team.connection(write=False) as connection:
            operation = connection.execute(
                """
                SELECT status, error FROM external_operations
                WHERE operation_id = 'OP-MISSING-WORKTREE'
                """
            ).fetchone()
        self.assertEqual(operation["status"], "failed")
        self.assertIn("did not complete", operation["error"])

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

    def test_archive_claim_races_preserve_terminal_invariants(self) -> None:
        project = self.team.register_project(self.repo)
        project_task = self.team.create_task(
            self.repo,
            title="Project archive race",
            description="Claim and project archive are serialized.",
            acceptance="No archived project contains a running task.",
            kind="read",
            owner_role="team_explorer",
        )
        barrier = threading.Barrier(2)

        def outcome(action: object) -> tuple[str, object]:
            barrier.wait(timeout=5)
            try:
                return "ok", action()
            except TeamError as exc:
                return "error", str(exc)

        with ThreadPoolExecutor(max_workers=2) as executor:
            project_outcomes = list(
                executor.map(
                    outcome,
                    [
                        lambda: self.team.claim_task(
                            project_task["task_id"],
                            actor="project-racer",
                        ),
                        lambda: self.team.set_project_archived(
                            project["project_id"],
                            True,
                        ),
                    ],
                )
            )
        current_project = self.team.get_project(project["project_id"])
        current_task = self.team.get_task(project_task["task_id"])
        self.assertFalse(
            bool(current_project["archived_at"])
            and current_task["status"] == "running"
        )
        self.assertEqual(
            sum(item[0] == "ok" for item in project_outcomes),
            1,
        )
        if current_project["archived_at"]:
            self.team.set_project_archived(project["project_id"], False)

        task = self.team.create_task(
            self.repo,
            title="Task archive race",
            description="Claim and task archive are serialized.",
            acceptance="A task cannot be archived and running together.",
            kind="read",
            owner_role="team_explorer",
        )
        barrier = threading.Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as executor:
            task_outcomes = list(
                executor.map(
                    outcome,
                    [
                        lambda: self.team.claim_task(
                            task["task_id"],
                            actor="task-racer",
                        ),
                        lambda: self.team.set_task_archived(
                            task["task_id"],
                            True,
                        ),
                    ],
                )
            )
        current_task = self.team.get_task(task["task_id"])
        self.assertFalse(
            bool(current_task["archived_at"])
            and current_task["status"] == "running"
        )
        self.assertEqual(
            sum(item[0] == "ok" for item in task_outcomes),
            1,
        )

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

    def test_archive_is_idempotent_and_traces_trusted_actor(self) -> None:
        task = self.team.create_task(
            self.repo,
            title="Archive idempotence",
            description="Repeated archive requests must not rewrite history.",
            acceptance="State, version, and events change only on transitions.",
            kind="read",
            owner_role="team_explorer",
        )
        with patch.dict(
            os.environ,
            {
                "CODEX_TEAM_ROLE": "team_worker",
                "CODEX_TEAM_ACTOR": "managed-worker",
            },
            clear=False,
        ):
            with self.assertRaisesRegex(TeamError, "managed role team_worker"):
                self.team.set_task_archived(
                    str(task["task_id"]),
                    True,
                    actor="human",
                )
        self.assertIsNone(
            self.team.get_task(str(task["task_id"]))["archived_at"]
        )

        with patch.dict(
            os.environ,
            {
                "CODEX_TEAM_ROLE": "lead",
                "CODEX_TEAM_ACTOR": "",
            },
            clear=False,
        ):
            archived = self.team.set_task_archived(
                str(task["task_id"]),
                True,
                actor="lead",
            )
        self.assertIsNotNone(archived["archived_at"])
        version = archived["version"]
        events_after_first = self.team.get_task_events(
            str(task["task_id"])
        )["events"]
        archive_events = [
            event
            for event in events_after_first
            if event["event_type"] == "archived"
        ]
        self.assertEqual(len(archive_events), 1)
        self.assertEqual(archive_events[0]["actor"], "lead")

        repeated = self.team.set_task_archived(
            str(task["task_id"]),
            True,
            actor="lead",
        )
        self.assertEqual(repeated["version"], version)
        self.assertEqual(
            len(
                [
                    event
                    for event in self.team.get_task_events(
                        str(task["task_id"])
                    )["events"]
                    if event["event_type"] == "archived"
                ]
            ),
            1,
        )

        restored = self.team.set_task_archived(
            str(task["task_id"]),
            False,
            actor="lead",
        )
        self.assertIsNone(restored["archived_at"])
        restored_version = restored["version"]
        restored_again = self.team.set_task_archived(
            str(task["task_id"]),
            False,
            actor="lead",
        )
        self.assertEqual(restored_again["version"], restored_version)
        self.assertEqual(
            len(
                [
                    event
                    for event in self.team.get_task_events(
                        str(task["task_id"])
                    )["events"]
                    if event["event_type"] == "restored"
                ]
            ),
            1,
        )

    def test_archived_parents_block_manual_restore(self) -> None:
        task = self.team.create_task(
            self.repo,
            title="Project archived task",
            description="Manual restore must not bypass a parent archive.",
            acceptance="The project must be restored first.",
            kind="read",
            owner_role="team_explorer",
        )
        project = self.team.get_project(str(task["project_id"]))
        self.team.set_task_archived(str(task["task_id"]), True)
        self.team.set_project_archived(str(project["project_id"]), True)
        with self.assertRaisesRegex(TeamError, "project is archived"):
            self.team.set_task_archived(
                str(task["task_id"]),
                False,
            )
        self.team.set_project_archived(str(project["project_id"]), False)
        self.team.set_task_archived(str(task["task_id"]), False)

        manager = ClusterManager(
            self.settings,
            team=self.team,
            worktrees=self.worktrees,
        )
        goal = manager.create_goal(
            self.repo,
            objective="Goal archive restore guard",
            autonomy="supervised",
        )
        goal_task = self.team.create_task(
            self.repo,
            title="Goal archived task",
            description="The parent goal owns the archive state.",
            acceptance="The goal must be restored first.",
            kind="read",
            owner_role="team_explorer",
            goal_id=goal["goal_id"],
        )
        manager.set_goal_status(goal["goal_id"], "active")
        manager.set_goal_status(goal["goal_id"], "completed")
        manager.set_goal_archived(goal["goal_id"], True)
        with self.assertRaisesRegex(TeamError, "restore the goal first"):
            self.team.set_task_archived(
                str(goal_task["task_id"]),
                False,
            )
        restored_goal = manager.set_goal_archived(
            goal["goal_id"],
            False,
        )
        self.assertIsNone(restored_goal["archived_at"])
        self.assertIsNone(
            self.team.get_task(str(goal_task["task_id"]))["archived_at"]
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
        self.assertEqual(agent["profile_contract_status"], "historical")
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

    def test_dashboard_projects_failed_agent_latest_job_usage(self) -> None:
        manager = ClusterManager(
            self.settings,
            team=self.team,
            worktrees=self.worktrees,
        )
        goal = manager.create_goal(
            self.repo,
            objective="Diagnose failed attempt usage",
        )
        now = utc_now()
        with self.team.connection() as connection:
            connection.execute(
                """
                INSERT INTO cluster_agents(
                    agent_id, project_id, role, workstream_key, display_name,
                    thread_id, status, model, model_provider,
                    total_input_tokens, total_cached_input_tokens,
                    total_uncached_input_tokens, context_tokens,
                    turn_count, created_at, updated_at
                ) VALUES (
                    'A-FAILED-USAGE', ?, 'team_explorer', 'failed-usage',
                    'Failed Usage Explorer', 'thread-failed-usage', 'failed',
                    'test-model', 'test-provider', 0, 0, 0, 0, 1, ?, ?
                )
                """,
                (goal["project_id"], now, now),
            )
            connection.execute(
                """
                INSERT INTO cluster_jobs(
                    job_id, goal_id, kind, role, status, attempt,
                    log_path, output_path, started_at, finished_at,
                    error, agent_id, runtime, input_tokens,
                    cached_input_tokens, uncached_input_tokens,
                    output_tokens, context_tokens, model_context_window,
                    usage_source, telemetry_complete,
                    usage_missing_fields, correlation_id, prompt_hash,
                    thread_action, outcome, settled_at
                ) VALUES (
                    'J-FAILED-USAGE', ?, 'worker', 'team_explorer', 'failed',
                    1, ?, ?, ?, ?, 'provider failure', 'A-FAILED-USAGE',
                    'app_server', 800, 600, 200, 120, 4096, 128000,
                    'app_server.token_usage', 1, '[]', 'J-FAILED-USAGE',
                    ?, 'fresh', 'failed', ?
                )
                """,
                (
                    goal["goal_id"],
                    str(self.base / "failed-usage.log"),
                    str(self.base / "failed-usage.out"),
                    now,
                    now,
                    "a" * 64,
                    now,
                ),
            )

        dashboard = self.team.dashboard_snapshot(str(goal["project_id"]))
        agent = next(
            item
            for item in dashboard["cluster_agents"]
            if item["agent_id"] == "A-FAILED-USAGE"
        )
        self.assertEqual(agent["cache_usage_source"], "latest_job")
        self.assertEqual(agent["latest_job_id"], "J-FAILED-USAGE")
        self.assertEqual(agent["latest_job_status"], "failed")
        self.assertEqual(agent["latest_job_outcome"], "failed")
        self.assertEqual(agent["latest_job_thread_action"], "fresh")
        self.assertEqual(agent["latest_job_cached_input_tokens"], 600)
        self.assertEqual(agent["latest_job_uncached_input_tokens"], 200)
        self.assertEqual(agent["ledger_total_input_tokens"], 800)
        self.assertEqual(agent["ledger_total_cached_input_tokens"], 600)
        self.assertEqual(agent["ledger_total_uncached_input_tokens"], 200)
        self.assertEqual(agent["cache_hit_rate"], 0.75)

    def test_native_thread_registration_and_correlated_task_events(self) -> None:
        task = self.team.create_task(
            self.repo,
            title="Native thread registration",
            description="Register the real native session and task prompt.",
            acceptance="Dashboard and events carry thread correlation.",
            kind="read",
            owner_role="team_explorer",
        )
        thread_id = "01a1cafe-1111-7222-8333-944455556666"
        sessions = self.base / "native-sessions"
        session = sessions / "2026" / "11" / "10" / f"rollout-{thread_id}.jsonl"
        session.parent.mkdir(parents=True)
        session.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "session_meta",
                            "payload": {
                                "source": {
                                    "subagent": {
                                        "thread_spawn": {
                                            "parent_thread_id": "parent-thread",
                                            "agent_path": "/root/native-explorer",
                                            "agent_nickname": "Erdos",
                                            "agent_role": "team_explorer",
                                        }
                                    }
                                }
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "user",
                                "content": [
                                    {
                                        "type": "input_text",
                                        "text": f"Execute task {task['task_id']}",
                                    }
                                ],
                            },
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        manager = ClusterManager(
            self.settings,
            team=self.team,
            worktrees=self.worktrees,
            runtime_mode="exec",
            agent_command=["codex"],
        )
        with patch.dict(
            os.environ,
            {"CODEX_NATIVE_SESSIONS_ROOT": str(sessions)},
            clear=False,
        ):
            registered = manager.register_native_agent(
                self.repo,
                thread_id,
                "team_explorer",
                "native-stream",
                actor="native-explorer",
                task_id=str(task["task_id"]),
            )
        agent = registered["agent"]
        self.assertEqual(agent["runtime"], "native")
        self.assertEqual(agent["prompt_status"], "delivered")
        self.assertEqual(agent["parent_thread_id"], "parent-thread")
        self.assertEqual(agent["agent_nickname"], "Erdos")

        claimed = self.team.claim_task(
            str(task["task_id"]),
            actor="native-explorer",
        )
        self.assertEqual(claimed["status"], "running")
        events = self.team.get_task_events(str(task["task_id"]))["events"]
        claim_event = next(
            event for event in events if event["event_type"] == "claimed"
        )
        self.assertEqual(claim_event["thread_id"], thread_id)
        self.assertEqual(claim_event["correlation_id"], f"native:{thread_id}")
        self.assertEqual(claim_event["assignment_id"], thread_id)
        dashboard = self.team.dashboard_snapshot(str(task["project_id"]))
        dashboard_agent = next(
            item
            for item in dashboard["cluster_agents"]
            if item["agent_id"] == agent["agent_id"]
        )
        self.assertEqual(dashboard_agent["runtime"], "native")
        self.assertEqual(dashboard_agent["prompt_status"], "delivered")

    def test_native_heartbeat_refreshes_agent_freshness_atomically(self) -> None:
        task = self.team.create_task(
            self.repo,
            title="Native heartbeat freshness",
            description="Heartbeat must refresh the bound native agent.",
            acceptance="Task lease and agent freshness commit together.",
            kind="read",
            owner_role="team_explorer",
        )
        project_id = str(task["project_id"])
        agent_id = "A-NATIVE-HEARTBEAT"
        now = utc_now()
        with self.team.connection() as connection:
            connection.execute(
                """
                INSERT INTO cluster_agents(
                    agent_id, project_id, role, workstream_key, display_name,
                    thread_id, runtime, actor, prompt_status, status,
                    current_task_id, last_seen_at, session_stale,
                    created_at, updated_at
                ) VALUES (
                    ?, ?, 'team_explorer', 'native-heartbeat',
                    'Native heartbeat', 'native-heartbeat-thread', 'native',
                    'native-heartbeat-actor', 'delivered', 'loaded', ?,
                    '2000-01-01T00:00:00Z', 1, ?, ?
                )
                """,
                (
                    agent_id,
                    project_id,
                    task["task_id"],
                    now,
                    now,
                ),
            )
        claimed = self.team.claim_task(
            str(task["task_id"]),
            actor="native-heartbeat-actor",
        )
        token = str(claimed["claim_token"])
        with self.team.connection() as connection:
            connection.execute(
                """
                UPDATE cluster_agents
                SET last_seen_at = '2000-01-01T00:00:00Z',
                    session_stale = 1
                WHERE agent_id = ?
                """,
                (agent_id,),
            )
        heartbeat = self.team.heartbeat(
            str(task["task_id"]),
            actor="native-heartbeat-actor",
            claim_token=token,
        )
        with self.team.connection(write=False) as connection:
            agent = connection.execute(
                """
                SELECT last_seen_at, session_stale, updated_at
                FROM cluster_agents WHERE agent_id = ?
                """,
                (agent_id,),
            ).fetchone()
        self.assertNotEqual(agent["last_seen_at"], "2000-01-01T00:00:00Z")
        self.assertEqual(agent["session_stale"], 0)
        self.assertGreaterEqual(
            agent["updated_at"],
            heartbeat["updated_at"],
        )

        task_before = self.team.get_task(str(task["task_id"]))
        agent_before = dict(agent)
        with patch.object(
            TeamService,
            "_event",
            side_effect=RuntimeError("simulated event failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "event failure"):
                self.team.heartbeat(
                    str(task["task_id"]),
                    actor="native-heartbeat-actor",
                    claim_token=token,
                )
        task_after = self.team.get_task(str(task["task_id"]))
        with self.team.connection(write=False) as connection:
            agent_after = dict(
                connection.execute(
                    """
                    SELECT last_seen_at, session_stale, updated_at
                    FROM cluster_agents WHERE agent_id = ?
                    """,
                    (agent_id,),
                ).fetchone()
            )
        self.assertEqual(task_after["version"], task_before["version"])
        self.assertEqual(task_after["lease_until"], task_before["lease_until"])
        self.assertEqual(agent_after, agent_before)

    def test_native_prompt_missing_blocks_claim_until_refresh(self) -> None:
        task = self.team.create_task(
            self.repo,
            title="Native prompt missing",
            description="A created thread without the task prompt must not claim.",
            acceptance="Claim is blocked until prompt delivery is verified.",
            kind="read",
            owner_role="team_explorer",
        )
        thread_id = "01a1cafe-3333-7444-8555-966677778888"
        sessions = self.base / "native-missing"
        session = sessions / f"rollout-{thread_id}.jsonl"
        session.parent.mkdir(parents=True)
        session.write_text(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "Only AGENTS and environment context",
                            }
                        ],
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        manager = ClusterManager(
            self.settings,
            team=self.team,
            worktrees=self.worktrees,
            runtime_mode="exec",
            agent_command=["codex"],
        )
        with patch.dict(
            os.environ,
            {"CODEX_NATIVE_SESSIONS_ROOT": str(sessions)},
            clear=False,
        ):
            registered = manager.register_native_agent(
                self.repo,
                thread_id,
                "team_explorer",
                "native-missing",
                actor="native-missing-worker",
                task_id=str(task["task_id"]),
            )
        self.assertEqual(
            registered["agent"]["prompt_status"],
            "missing",
        )
        with self.assertRaisesRegex(TeamError, "prompt has not been verified"):
            self.team.claim_task(
                str(task["task_id"]),
                actor="native-missing-worker",
            )
        with session.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": f"Execute {task['task_id']}",
                                }
                            ],
                        },
                    }
                )
                + "\n"
            )
        with patch.dict(
            os.environ,
            {"CODEX_NATIVE_SESSIONS_ROOT": str(sessions)},
            clear=False,
        ):
            refreshed = manager.register_native_agent(
                self.repo,
                thread_id,
                "team_explorer",
                "native-missing",
                actor="native-missing-worker",
                task_id=str(task["task_id"]),
            )
        self.assertEqual(
            refreshed["agent"]["prompt_status"],
            "delivered",
        )
        self.assertEqual(
            self.team.claim_task(
                str(task["task_id"]),
                actor="native-missing-worker",
            )["status"],
            "running",
        )

    def test_native_explicit_assignment_conflict_fails_closed(self) -> None:
        requested = self.team.create_task(
            self.repo,
            title="Requested native task",
            description="This is not the session-assigned task.",
            acceptance="Conflicting registration is rejected.",
            kind="read",
            owner_role="team_explorer",
        )
        assigned = self.team.create_task(
            self.repo,
            title="Assigned native task",
            description="The session metadata explicitly assigns this task.",
            acceptance="The explicit assignment wins.",
            kind="read",
            owner_role="team_explorer",
        )
        thread_id = "01a1cafe-4444-7555-8666-977788889999"
        sessions = self.base / "native-conflict"
        session = sessions / f"rollout-{thread_id}.jsonl"
        session.parent.mkdir(parents=True)
        session.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "session_meta",
                            "payload": {
                                "id": thread_id,
                                "task_id": assigned["task_id"],
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "user",
                                "content": (
                                    f"Execute {assigned['task_id']}; "
                                    f"ignore {requested['task_id']}"
                                ),
                            },
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        manager = ClusterManager(
            self.settings,
            team=self.team,
            worktrees=self.worktrees,
            runtime_mode="exec",
            agent_command=["codex"],
        )
        with patch.dict(
            os.environ,
            {"CODEX_NATIVE_SESSIONS_ROOT": str(sessions)},
            clear=False,
        ):
            with self.assertRaisesRegex(
                ClusterError,
                "assignment conflicts",
            ):
                manager.register_native_agent(
                    self.repo,
                    thread_id,
                    "team_explorer",
                    "native-conflict",
                    actor="native-conflict-worker",
                    task_id=str(requested["task_id"]),
                )

    def test_native_registration_role_wins_over_path_inference(self) -> None:
        thread_id = "01a1cafe-5555-7666-8777-98889999aaaa"
        sessions = self.base / "native-role-inference"
        session = sessions / f"rollout-{thread_id}.jsonl"
        session.parent.mkdir(parents=True)
        session.write_text(
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {
                        "id": thread_id,
                        "source": {
                            "subagent": {
                                "thread_spawn": {
                                    "agent_path": "/root/reviewer-name"
                                }
                            }
                        },
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        manager = ClusterManager(
            self.settings,
            team=self.team,
            worktrees=self.worktrees,
            runtime_mode="exec",
            agent_command=["codex"],
        )
        with patch.dict(
            os.environ,
            {"CODEX_NATIVE_SESSIONS_ROOT": str(sessions)},
            clear=False,
        ):
            registered = manager.register_native_agent(
                self.repo,
                thread_id,
                "team_worker",
                "native-role-inference",
                actor="native-role-worker",
            )
        self.assertEqual(registered["effective_role"], "team_worker")
        self.assertEqual(
            registered["agent"]["identity_source"],
            "registration",
        )
        self.assertEqual(
            registered["agent"]["identity_confidence"],
            "medium",
        )

    def test_native_explicit_role_conflict_fails_closed(self) -> None:
        thread_id = "01a1cafe-6666-7777-8888-9999aaaabbbb"
        sessions = self.base / "native-role-conflict"
        session = sessions / f"rollout-{thread_id}.jsonl"
        session.parent.mkdir(parents=True)
        session.write_text(
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {
                        "id": thread_id,
                        "agent_role": "team_explorer",
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        manager = ClusterManager(
            self.settings,
            team=self.team,
            worktrees=self.worktrees,
            runtime_mode="exec",
            agent_command=["codex"],
        )
        with patch.dict(
            os.environ,
            {"CODEX_NATIVE_SESSIONS_ROOT": str(sessions)},
            clear=False,
        ):
            with self.assertRaisesRegex(
                ClusterError,
                "session role conflicts",
            ):
                manager.register_native_agent(
                    self.repo,
                    thread_id,
                    "team_worker",
                    "native-role-conflict",
                    actor="native-role-worker",
                )
        project = self.team.project_for_repo(self.repo)
        self.assertFalse(
            any(
                agent["thread_id"] == thread_id
                for agent in manager.list_agents(
                    str(project["project_id"])
                )
            )
        )

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
        manager.set_goal_status(goal["goal_id"], "completed")
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
        manager.set_goal_status(
            goal["goal_id"],
            "failed",
            reason="historical failure",
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
        manager.set_goal_status(
            goal["goal_id"],
            "failed",
            reason="historical failure",
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
                "authority_token": self.team.operator_authority(),
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
                "authority_token": self.team.operator_authority(),
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

    def test_mcp_request_id_replays_result_and_rejects_reuse_conflicts(self) -> None:
        mcp = TeamMcpServer(self.settings)
        payload = {
            "repo_root": str(self.repo),
            "title": "Idempotent task creation",
            "description": "A retry must not create a second task.",
            "acceptance": "The original result is returned.",
            "kind": "read",
            "owner_role": "team_explorer",
        }

        def create(message_id: str) -> dict[str, object]:
            response = _process(
                mcp,
                {
                    "id": message_id,
                    "method": "tools/call",
                    "params": {
                        "name": "team_write",
                        "arguments": {
                            "operation": "team_create_task",
                            "arguments": payload,
                            "request_id": "create-task-stable",
                        },
                    },
                },
            )
            self.assertIsNotNone(response)
            result = response["result"]
            self.assertFalse(result["isError"])
            return result["structuredContent"]

        with patch.dict(
            os.environ,
            {
                "CODEX_TEAM_MCP_ACCESS": "full",
                "CODEX_TEAM_MCP_SURFACE": "core",
            },
            clear=False,
        ):
            first = create("first-create")
            second = create("retry-create")
            fetched = mcp.call_tool(
                "team_get_task",
                {"task_id": first["task_id"]},
            )
            with self.assertRaisesRegex(TeamError, "different payload"):
                mcp.call_tool(
                    "team_create_task",
                    {**payload, "title": "Different payload"},
                    request_id="create-task-stable",
                )
            with self.assertRaisesRegex(TeamError, "different operation"):
                mcp.call_tool(
                    "team_claim_task",
                    payload,
                    request_id="create-task-stable",
                )

        self.assertEqual(first, second)
        self.assertEqual(fetched["task_id"], first["task_id"])
        with self.team.connection(write=False) as connection:
            task_count = connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE title = ?",
                ("Idempotent task creation",),
            ).fetchone()[0]
            event_count = connection.execute(
                """
                SELECT COUNT(*) FROM task_events
                WHERE task_id = ? AND event_type = 'created'
                """,
                (first["task_id"],),
            ).fetchone()[0]
        self.assertEqual(task_count, 1)
        self.assertEqual(event_count, 1)

    def test_mcp_request_id_concurrent_retry_executes_once(self) -> None:
        payload = {
            "repo_root": str(self.repo),
            "title": "Concurrent idempotent creation",
            "description": "Only one caller may execute the mutation.",
            "acceptance": "Both callers receive the original result.",
            "kind": "read",
            "owner_role": "team_explorer",
        }
        servers = [TeamMcpServer(self.settings), TeamMcpServer(self.settings)]
        barrier = threading.Barrier(2)

        def create(server: TeamMcpServer) -> dict[str, object]:
            barrier.wait(timeout=5)
            return server.call_tool(
                "team_create_task",
                payload,
                request_id="create-concurrent-stable",
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(create, servers))

        self.assertEqual(results[0], results[1])
        with self.team.connection(write=False) as connection:
            task_count = connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE title = ?",
                ("Concurrent idempotent creation",),
            ).fetchone()[0]
            event_count = connection.execute(
                """
                SELECT COUNT(*) FROM task_events
                WHERE task_id = ? AND event_type = 'created'
                """,
                (results[0]["task_id"],),
            ).fetchone()[0]
        self.assertEqual(task_count, 1)
        self.assertEqual(event_count, 1)

    def test_mcp_request_id_replays_message_without_duplicate_event(self) -> None:
        mcp = TeamMcpServer(self.settings)
        task = self.team.create_task(
            self.repo,
            title="Idempotent message",
            description="Repeated delivery must not duplicate audit records.",
            acceptance="One message and one event exist.",
            kind="read",
            owner_role="team_explorer",
        )
        arguments = {
            "repo_root": str(self.repo),
            "sender": "lead",
            "recipient": "worker",
            "body": "one durable message",
            "task_id": task["task_id"],
        }
        first = mcp.call_tool(
            "team_add_message",
            arguments,
            request_id="message-stable",
        )
        second = mcp.call_tool(
            "team_add_message",
            arguments,
            request_id="message-stable",
        )

        self.assertEqual(second, first)
        messages = self.team.list_messages(
            self.repo,
            task_id=task["task_id"],
        )
        events = self.team.get_task_events(task["task_id"])["events"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(
            [event["event_type"] for event in events].count("message"),
            1,
        )

    def test_mcp_transient_failure_does_not_poison_request_id(self) -> None:
        mcp = TeamMcpServer(self.settings)
        original_call = mcp._call_tool_once
        attempts = {"count": 0}

        def fail_once(name: str, arguments: dict[str, object]) -> object:
            if name == "team_create_task" and attempts["count"] == 0:
                attempts["count"] += 1
                raise OSError("simulated delivery interruption")
            return original_call(name, arguments)

        mcp._call_tool_once = fail_once  # type: ignore[method-assign]
        payload = {
            "repo_root": str(self.repo),
            "title": "Transient receipt retry",
            "description": "A pre-effect transport failure is retryable.",
            "acceptance": "Exactly one task is created.",
            "kind": "read",
            "owner_role": "team_explorer",
        }
        with self.assertRaisesRegex(OSError, "delivery interruption"):
            mcp.call_tool(
                "team_create_task",
                payload,
                request_id="transient-create-stable",
            )
        with self.team.connection(write=False) as connection:
            failed_receipt = connection.execute(
                """
                SELECT status FROM request_receipts WHERE request_id = ?
                """,
                ("transient-create-stable",),
            ).fetchone()
        self.assertIsNone(failed_receipt)

        created = mcp.call_tool(
            "team_create_task",
            payload,
            request_id="transient-create-stable",
        )
        with self.team.connection(write=False) as connection:
            task_count = connection.execute(
                """
                SELECT COUNT(*) FROM tasks WHERE title = ?
                """,
                ("Transient receipt retry",),
            ).fetchone()[0]
        self.assertEqual(task_count, 1)
        self.assertEqual(created["title"], "Transient receipt retry")

    def test_mcp_message_commit_recovers_without_duplicate_delivery(
        self,
    ) -> None:
        mcp = TeamMcpServer(self.settings)

        def crash_before_receipt_finish(*args: object, **kwargs: object) -> None:
            raise OSError("simulated message receipt crash")

        mcp._finish_request_receipt = (  # type: ignore[method-assign]
            crash_before_receipt_finish
        )
        payload = {
            "repo_root": str(self.repo),
            "sender": "lead",
            "recipient": "worker",
            "body": "exactly one durable delivery",
        }
        with self.assertRaisesRegex(OSError, "message receipt crash"):
            mcp.call_tool(
                "team_add_message",
                payload,
                request_id="crash-safe-message",
            )

        retry_server = TeamMcpServer(self.settings)
        recovered = retry_server.call_tool(
            "team_add_message",
            payload,
            request_id="crash-safe-message",
        )
        replay = retry_server.call_tool(
            "team_add_message",
            payload,
            request_id="crash-safe-message",
        )
        messages = self.team.list_messages(self.repo)
        self.assertEqual(len(messages), 1)
        self.assertEqual(recovered, replay)
        self.assertEqual(recovered["body"], payload["body"])

    def test_mcp_pending_receipt_recovers_committed_result_after_crash(
        self,
    ) -> None:
        mcp = TeamMcpServer(self.settings)

        def crash_before_receipt_finish(*args: object, **kwargs: object) -> None:
            raise OSError("simulated process death after commit")

        mcp._finish_request_receipt = (  # type: ignore[method-assign]
            crash_before_receipt_finish
        )
        payload = {
            "repo_root": str(self.repo),
            "title": "Crash-safe receipt",
            "description": "The task commit precedes receipt completion.",
            "acceptance": "Retry recovers the committed task.",
            "kind": "read",
            "owner_role": "team_explorer",
        }
        with self.assertRaisesRegex(OSError, "process death"):
            mcp.call_tool(
                "team_create_task",
                payload,
                request_id="crash-safe-create",
            )
        with self.team.connection(write=False) as connection:
            receipt = connection.execute(
                """
                SELECT status FROM request_receipts WHERE request_id = ?
                """,
                ("crash-safe-create",),
            ).fetchone()
            effect = connection.execute(
                """
                SELECT status FROM external_operations
                WHERE operation_id = ? AND operation_type = 'team_create_task'
                """,
                ("crash-safe-create",),
            ).fetchone()
        self.assertEqual(receipt["status"], "pending")
        self.assertEqual(effect["status"], "completed")

        retry_server = TeamMcpServer(self.settings)
        recovered = retry_server.call_tool(
            "team_create_task",
            payload,
            request_id="crash-safe-create",
        )
        with self.team.connection(write=False) as connection:
            task_count = connection.execute(
                """
                SELECT COUNT(*) FROM tasks WHERE title = ?
                """,
                ("Crash-safe receipt",),
            ).fetchone()[0]
            completed = connection.execute(
                """
                SELECT status FROM request_receipts WHERE request_id = ?
                """,
                ("crash-safe-create",),
            ).fetchone()
        self.assertEqual(task_count, 1)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(recovered["title"], "Crash-safe receipt")

    def test_mcp_pending_receipt_with_dead_owner_is_reclaimed_once(
        self,
    ) -> None:
        mcp = TeamMcpServer(self.settings)
        payload = {
            "repo_root": str(self.repo),
            "title": "Dead owner recovery",
            "description": "A crashed pre-effect owner is safely reclaimed.",
            "acceptance": "The retry executes once and completes the receipt.",
            "kind": "read",
            "owner_role": "team_explorer",
        }
        payload_hash = sha256_text(canonical_json(payload))
        now = utc_now()
        owner = json.dumps(
            {
                "owner_token": "dead-owner",
                "pid": 999_999_999,
                "instance_id": "not-running",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        with self.team.connection() as connection:
            connection.execute(
                """
                INSERT INTO request_receipts(
                    request_id, operation, payload_hash, status,
                    result_json, error, created_at, updated_at
                ) VALUES (?, ?, ?, 'pending', NULL, ?, ?, ?)
                """,
                (
                    "dead-owner-create",
                    "team_create_task",
                    payload_hash,
                    owner,
                    now,
                    now,
                ),
            )

        created = mcp.call_tool(
            "team_create_task",
            payload,
            request_id="dead-owner-create",
        )
        retried = mcp.call_tool(
            "team_create_task",
            payload,
            request_id="dead-owner-create",
        )
        self.assertEqual(created, retried)
        with self.team.connection(write=False) as connection:
            task_count = connection.execute(
                """
                SELECT COUNT(*) FROM tasks WHERE title = ?
                """,
                ("Dead owner recovery",),
            ).fetchone()[0]
            receipt = connection.execute(
                """
                SELECT status FROM request_receipts WHERE request_id = ?
                """,
                ("dead-owner-create",),
            ).fetchone()
        self.assertEqual(task_count, 1)
        self.assertEqual(receipt["status"], "completed")

    def test_concurrent_block_and_submit_have_one_state_winner(self) -> None:
        task = self.team.create_task(
            self.repo,
            title="Block submit race",
            description="Only one transition may own the running claim.",
            acceptance="The final state is blocked or review, never both.",
            kind="read",
            owner_role="team_explorer",
        )
        claimed = self.team.claim_task(task["task_id"], actor="race-worker")
        token = str(claimed["claim_token"])
        barrier = threading.Barrier(2)

        def block() -> object:
            barrier.wait(timeout=5)
            try:
                return self.team.block_task(
                    task["task_id"],
                    actor="race-worker",
                    reason="blocked concurrently",
                    claim_token=token,
                )
            except TeamError as exc:
                return exc

        def submit() -> object:
            barrier.wait(timeout=5)
            try:
                return self.team.submit_task(
                    task["task_id"],
                    actor="race-worker",
                    claim_token=token,
                    result="submitted concurrently",
                    tests="race fixture",
                )
            except TeamError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = [
                executor.submit(block),
                executor.submit(submit),
            ]
            results = [future.result(timeout=10) for future in outcomes]

        self.assertEqual(
            sum(isinstance(result, dict) for result in results),
            1,
        )
        self.assertEqual(
            sum(isinstance(result, TeamError) for result in results),
            1,
        )
        current = self.team.get_task(task["task_id"])
        self.assertIn(current["status"], {"blocked", "review"})
        event_types = [
            event["event_type"]
            for event in self.team.get_task_events(task["task_id"])["events"]
        ]
        self.assertEqual(
            sum(event in {"blocked", "submitted"} for event in event_types),
            1,
        )

    def test_concurrent_heartbeat_and_block_cannot_restore_running_state(self) -> None:
        task = self.team.create_task(
            self.repo,
            title="Heartbeat block race",
            description="A heartbeat must not overwrite a blocked state.",
            acceptance="The final state is blocked.",
            kind="read",
            owner_role="team_explorer",
        )
        claimed = self.team.claim_task(task["task_id"], actor="lease-worker")
        token = str(claimed["claim_token"])
        barrier = threading.Barrier(2)

        def heartbeat() -> object:
            barrier.wait(timeout=5)
            try:
                return self.team.heartbeat(
                    task["task_id"],
                    actor="lease-worker",
                    claim_token=token,
                )
            except TeamError as exc:
                return exc

        def block() -> object:
            barrier.wait(timeout=5)
            try:
                return self.team.block_task(
                    task["task_id"],
                    actor="lease-worker",
                    reason="block wins the terminal state",
                    claim_token=token,
                )
            except TeamError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as executor:
            heartbeat_future = executor.submit(heartbeat)
            block_future = executor.submit(block)
            heartbeat_result = heartbeat_future.result(timeout=10)
            block_result = block_future.result(timeout=10)

        self.assertIsInstance(block_result, dict)
        self.assertTrue(
            isinstance(heartbeat_result, dict)
            or isinstance(heartbeat_result, TeamError)
        )
        self.assertEqual(self.team.get_task(task["task_id"])["status"], "blocked")

    def test_concurrent_recovery_and_submit_have_one_state_winner(self) -> None:
        manager = ClusterManager(
            self.settings,
            team=self.team,
            worktrees=self.worktrees,
        )
        goal = manager.create_goal(
            self.repo,
            objective="Recover or submit once",
            autonomy="auto",
        )
        task = self.team.create_task(
            self.repo,
            title="Recovery submit race",
            description="Recovery must not overwrite a submitted task.",
            acceptance="Exactly one transition wins.",
            kind="read",
            owner_role="team_explorer",
            goal_id=goal["goal_id"],
        )
        claimed = self.team.claim_task(task["task_id"], actor="recovery-worker")
        token = str(claimed["claim_token"])
        barrier = threading.Barrier(2)

        def recover() -> bool:
            barrier.wait(timeout=5)
            return manager._recover_task(
                task["task_id"],
                goal["goal_id"],
                reason="simulated worker recovery",
                expected_actor="recovery-worker",
            )

        def submit() -> object:
            barrier.wait(timeout=5)
            try:
                return self.team.submit_task(
                    task["task_id"],
                    actor="recovery-worker",
                    claim_token=token,
                    result="submitted before recovery",
                    tests="race fixture",
                )
            except TeamError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as executor:
            recovery_future = executor.submit(recover)
            submit_future = executor.submit(submit)
            recovery_result = recovery_future.result(timeout=10)
            submit_result = submit_future.result(timeout=10)

        self.assertEqual(
            int(recovery_result) + int(isinstance(submit_result, dict)),
            1,
        )
        current = self.team.get_task(task["task_id"])
        self.assertIn(current["status"], {"ready", "review"})
        with self.team.connection(write=False) as connection:
            recovered_event_count = connection.execute(
                """
                SELECT COUNT(*) FROM cluster_events
                WHERE goal_id = ? AND event_type = 'worker_recovered'
                """,
                (goal["goal_id"],),
            ).fetchone()[0]
        self.assertEqual(recovered_event_count, int(recovery_result))

    def test_reservation_release_and_submit_have_one_state_winner(self) -> None:
        manager = ClusterManager(
            self.settings,
            team=self.team,
            worktrees=self.worktrees,
        )
        goal = manager.create_goal(
            self.repo,
            objective="Release or submit once",
            autonomy="auto",
        )
        task = self.team.create_task(
            self.repo,
            title="Reservation submit race",
            description="Reservation cleanup must not overwrite submission.",
            acceptance="Exactly one transition wins.",
            kind="read",
            owner_role="team_explorer",
            goal_id=goal["goal_id"],
        )
        claimed = self.team.claim_task(
            task["task_id"],
            actor="reserved-worker",
        )
        token = str(claimed["claim_token"])
        barrier = threading.Barrier(2)

        def release() -> bool:
            barrier.wait(timeout=5)
            return manager._release_worker_reservation(
                task["task_id"],
                goal["goal_id"],
                "reserved-worker",
                reason="simulated startup failure",
            )

        def submit() -> object:
            barrier.wait(timeout=5)
            try:
                return self.team.submit_task(
                    task["task_id"],
                    actor="reserved-worker",
                    claim_token=token,
                    result="submitted before reservation release",
                    tests="race fixture",
                )
            except TeamError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as executor:
            release_future = executor.submit(release)
            submit_future = executor.submit(submit)
            release_result = release_future.result(timeout=10)
            submit_result = submit_future.result(timeout=10)

        self.assertEqual(
            int(release_result) + int(isinstance(submit_result, dict)),
            1,
        )
        current = self.team.get_task(task["task_id"])
        self.assertIn(current["status"], {"ready", "review"})
        with self.team.connection(write=False) as connection:
            release_event_count = connection.execute(
                """
                SELECT COUNT(*) FROM cluster_events
                WHERE goal_id = ?
                  AND event_type = 'worker_reservation_released'
                """,
                (goal["goal_id"],),
            ).fetchone()[0]
        self.assertEqual(release_event_count, int(release_result))

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
