"""Regression tests for hardening round 2.

Covers:
- dashboard_summary shares dashboard_snapshot's cluster_summary semantics
- transient dispatch failures defer instead of blocking the task
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from codex_team.cluster import ClusterManager
from codex_team.config import Settings
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


def _settings(base: Path) -> Settings:
    return Settings(
        project_root=Path(__file__).resolve().parents[1],
        home=base / "team-home",
        db_path=base / "team-home" / "team.sqlite",
        logs_dir=base / "team-home" / "logs",
    )


def _repo(base: Path, name: str) -> Path:
    repo = base / name
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "round2@example.invalid")
    git(repo, "config", "user.name", "Round Two")
    (repo / "README.md").write_text("# fixture\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "initial")
    return repo


class SummarySemanticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.repo = _repo(self.base, "repo")
        self.settings = _settings(self.base)
        self.team = TeamService(self.settings)
        self.manager = ClusterManager(
            self.settings,
            team=self.team,
            runtime_mode="exec",
            agent_command=["codex"],
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _insert_job(
        self,
        goal_id: str,
        job_id: str,
        *,
        kind: str,
        task_id: str | None,
        started_at: str,
    ) -> None:
        with self.team.connection() as connection:
            connection.execute(
                """
                INSERT INTO cluster_jobs(
                    job_id, goal_id, task_id, kind, role, actor, status,
                    attempt, log_path, output_path, started_at, runtime
                ) VALUES (?, ?, ?, ?, 'team_worker', ?, 'running',
                          1, '', '', ?, 'exec')
                """,
                (
                    job_id,
                    goal_id,
                    task_id,
                    kind,
                    f"auto-{job_id}",
                    started_at,
                ),
            )

    def test_summary_matches_snapshot_semantics(self) -> None:
        goal = self.manager.create_goal(
            self.repo,
            objective="Summary semantics parity",
            autonomy="auto",
        )
        goal_id = str(goal["goal_id"])
        self.manager.set_goal_status(goal_id, "active")
        blocked_task = self.team.create_task(
            self.repo,
            title="Blocked task",
            description="fixture",
            acceptance="fixture",
            kind="read",
            owner_role="team_explorer",
            goal_id=goal_id,
        )
        self.team.block_task(
            str(blocked_task["task_id"]),
            actor="lead",
            reason="fixture blocker",
        )
        review_task = self.team.create_task(
            self.repo,
            title="Review task",
            description="fixture",
            acceptance="fixture",
            kind="read",
            owner_role="team_explorer",
            goal_id=goal_id,
        )
        claimed = self.team.claim_task(
            str(review_task["task_id"]), actor="round2-worker"
        )
        self.team.submit_task(
            str(review_task["task_id"]),
            actor="round2-worker",
            claim_token=str(claimed["claim_token"]),
            result="fixture",
            tests="fixture",
        )
        ready_task = self.team.create_task(
            self.repo,
            title="Aged ready task",
            description="fixture",
            acceptance="fixture",
            kind="read",
            owner_role="team_explorer",
            goal_id=goal_id,
        )
        aged = (
            datetime.now(UTC) - timedelta(seconds=600)
        ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        with self.team.connection() as connection:
            connection.execute(
                "UPDATE tasks SET created_at = ?, updated_at = ? "
                "WHERE task_id = ?",
                (aged, aged, ready_task["task_id"]),
            )
        running_task = self.team.create_task(
            self.repo,
            title="Running task",
            description="fixture",
            acceptance="fixture",
            kind="read",
            owner_role="team_explorer",
            goal_id=goal_id,
        )
        self.team.claim_task(
            str(running_task["task_id"]), actor="round2-runner"
        )
        started = (
            datetime.now(UTC) - timedelta(seconds=120)
        ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        self._insert_job(
            goal_id,
            "J-ROUND2-WORKER",
            kind="worker",
            task_id=str(running_task["task_id"]),
            started_at=started,
        )
        self._insert_job(
            goal_id,
            "J-ROUND2-LEAD",
            kind="lead",
            task_id=None,
            started_at=started,
        )
        with patch.dict(
            "os.environ",
            {
                "CODEX_TEAM_CLUSTER_MAX_JOBS": "16",
                "CODEX_TEAM_CLUSTER_MAX_WORKERS": "3",
                "CODEX_TEAM_HEARTBEAT_WARNING_SECONDS": "300",
            },
        ):
            summary = self.team.dashboard_summary()["cluster_summary"]
            snapshot = self.team.dashboard_snapshot()["cluster_summary"]
        for field in (
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
            "oldest_heartbeat_age_seconds",
            "fairness_wait_count",
            "attention_count",
            "oldest_claim_wait_seconds",
        ):
            self.assertEqual(
                summary[field],
                snapshot[field],
                f"cluster_summary.{field} diverged between summary and snapshot",
            )
        self.assertEqual(summary["attention_count"], 3)
        self.assertEqual(summary["queued_goals"], 1)
        self.assertEqual(summary["active_executions"], 2)

    def test_summary_stays_lightweight(self) -> None:
        self.manager.create_goal(
            self.repo,
            objective="Lightweight probe",
            autonomy="auto",
        )
        for index in range(30):
            self.team.create_task(
                self.repo,
                title=f"Task {index}",
                description="fixture " * 40,
                acceptance="fixture",
                kind="read",
                owner_role="team_explorer",
            )
        summary = self.team.dashboard_summary()
        self.assertNotIn("tasks", summary)
        self.assertNotIn("cluster_goals", summary)
        self.assertIn("cluster_summary", summary)
        self.assertIn("revision", summary)


class TransientDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.repo = _repo(self.base, "repo")
        self.settings = _settings(self.base)
        self.team = TeamService(self.settings)
        self.manager = ClusterManager(
            self.settings,
            team=self.team,
            runtime_mode="exec",
            agent_command=["codex"],
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _ready_goal_and_task(self) -> tuple[dict, dict]:
        goal = self.manager.create_goal(
            self.repo,
            objective="Transient dispatch fixture",
            autonomy="auto",
        )
        self.manager.set_goal_status(str(goal["goal_id"]), "active")
        task = self.team.create_task(
            self.repo,
            title="Dispatch target",
            description="fixture",
            acceptance="fixture",
            kind="read",
            owner_role="team_explorer",
            goal_id=str(goal["goal_id"]),
        )
        return self.manager.get_goal(str(goal["goal_id"])), self.team.get_task(
            str(task["task_id"])
        )

    def test_transient_dispatch_failure_defers_not_blocks(self) -> None:
        goal, task = self._ready_goal_and_task()
        with patch.object(
            self.manager,
            "_dispatch_worker",
            side_effect=ConnectionError("simulated transport reset"),
        ):
            job, changed, keep_goal = self.manager._dispatch_next_job(
                goal, [task]
            )
        self.assertIsNone(job)
        self.assertTrue(changed)
        self.assertFalse(keep_goal)
        current = self.team.get_task(str(task["task_id"]))
        self.assertEqual(current["status"], "ready")
        self.assertIsNone(current["claimed_by"])
        with self.team.connection(write=False) as connection:
            events = connection.execute(
                """
                SELECT event_type FROM cluster_events
                WHERE goal_id = ? ORDER BY event_id
                """,
                (goal["goal_id"],),
            ).fetchall()
        event_types = [str(row["event_type"]) for row in events]
        self.assertIn("dispatch_deferred", event_types)
        self.assertNotIn("dispatch_failed", event_types)

    def test_permanent_dispatch_failure_still_blocks(self) -> None:
        goal, task = self._ready_goal_and_task()
        with patch.object(
            self.manager,
            "_dispatch_worker",
            side_effect=ValueError("unsupported worker configuration"),
        ):
            job, changed, keep_goal = self.manager._dispatch_next_job(
                goal, [task]
            )
        self.assertIsNone(job)
        self.assertTrue(changed)
        self.assertTrue(keep_goal)
        current = self.team.get_task(str(task["task_id"]))
        self.assertEqual(current["status"], "blocked")

    def test_transient_release_preserves_foreign_claim(self) -> None:
        goal, task = self._ready_goal_and_task()
        self.team.claim_task(str(task["task_id"]), actor="external-human")
        released = self.manager._release_auto_worker_reservation(
            str(task["task_id"]),
            str(goal["goal_id"]),
            reason="fixture",
        )
        self.assertFalse(released)
        current = self.team.get_task(str(task["task_id"]))
        self.assertEqual(current["status"], "running")
        self.assertEqual(current["claimed_by"], "external-human")


if __name__ == "__main__":
    unittest.main()