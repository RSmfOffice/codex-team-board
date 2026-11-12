"""Cross-scope consistency tests for the dashboard read model.

Verifies that summary and snapshot agree on cluster_summary semantics and that
canceled-task visibility is uniform across summary, snapshot and events for
every scope/archive combination.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from codex_team.cluster import ClusterManager
from codex_team.config import Settings
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


SUMMARY_FIELDS = (
    "goals_total",
    "goals_active",
    "goals_paused",
    "goals_failed",
    "ready_tasks",
    "running_tasks",
    "review_tasks",
    "blocked_tasks",
    "active_jobs",
    "active_executions",
    "external_executions",
    "native_executions",
    "active_workers",
    "active_leads",
    "global_jobs",
    "global_capacity",
    "worker_capacity_per_goal",
    "system_jobs",
    "system_capacity",
    "queued_goals",
    "heartbeat_overdue",
    "fairness_wait_count",
    "attention_count",
)


class ReadModelConsistencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.email", "consistency@example.invalid")
        git(self.repo, "config", "user.name", "Consistency Test")
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
        self.manager = ClusterManager(
            self.settings, team=self.team, runtime_mode="exec", agent_command=["codex"]
        )
        self.project = self.team.register_project(self.repo)
        self.project_id = str(self.project["project_id"])
        goal = self.manager.create_goal(
            self.repo, objective="Consistency fixture", autonomy="auto"
        )
        self.goal_id = str(goal["goal_id"])
        self.manager.set_goal_status(self.goal_id, "active")
        self.canceled_task = self.team.create_task(
            self.repo,
            title="Canceled task",
            description="fixture",
            acceptance="fixture",
            kind="read",
            owner_role="team_explorer",
            goal_id=self.goal_id,
        )
        self.team.cancel_task(
            str(self.canceled_task["task_id"]), actor="lead", reason="fixture"
        )
        self.ready_task = self.team.create_task(
            self.repo,
            title="Ready task",
            description="fixture",
            acceptance="fixture",
            kind="read",
            owner_role="team_explorer",
            goal_id=self.goal_id,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_canceled_hidden_in_all_scopes_and_views(self) -> None:
        canceled_id = str(self.canceled_task["task_id"])
        for project_id in (None, self.project_id):
            for include_archived in (False, True):
                with self.subTest(project=project_id, archived=include_archived):
                    summary = self.team.dashboard_summary(
                        project_id, include_archived=include_archived
                    )
                    snapshot = self.team.dashboard_snapshot(
                        project_id, include_archived=include_archived
                    )
                    events = self.team.dashboard_events(
                        project_id, include_archived=include_archived
                    )
                    self.assertEqual(summary["counts"].get("canceled", 0), 0)
                    snapshot_ids = {t["task_id"] for t in snapshot["tasks"]}
                    self.assertNotIn(canceled_id, snapshot_ids)
                    event_ids = {e["task_id"] for e in events["events"]}
                    self.assertNotIn(canceled_id, event_ids)

    def test_summary_matches_snapshot_cluster_summary(self) -> None:
        for project_id in (None, self.project_id):
            for include_archived in (False, True):
                with self.subTest(project=project_id, archived=include_archived):
                    summary = self.team.dashboard_summary(
                        project_id, include_archived=include_archived
                    )["cluster_summary"]
                    snapshot = self.team.dashboard_snapshot(
                        project_id, include_archived=include_archived
                    )["cluster_summary"]
                    for field in SUMMARY_FIELDS:
                        self.assertEqual(
                            summary[field], snapshot[field], f"{field}"
                        )

    def test_archived_task_visibility_consistent(self) -> None:
        # A standalone task (no goal) can be archived directly; tasks inside an
        # unfinished goal may only be archived via goal cascade.
        archived_task = self.team.create_task(
            self.repo,
            title="Archived task",
            description="fixture",
            acceptance="fixture",
            kind="read",
            owner_role="team_explorer",
        )
        self.team.set_task_archived(
            str(archived_task["task_id"]), True, actor="human"
        )
        archived_id = str(archived_task["task_id"])
        for project_id in (None, self.project_id):
            hidden = self.team.dashboard_snapshot(
                project_id, include_archived=False
            )
            shown = self.team.dashboard_snapshot(
                project_id, include_archived=True
            )
            self.assertNotIn(
                archived_id, {t["task_id"] for t in hidden["tasks"]}
            )
            self.assertIn(
                archived_id, {t["task_id"] for t in shown["tasks"]}
            )


if __name__ == "__main__":
    unittest.main()