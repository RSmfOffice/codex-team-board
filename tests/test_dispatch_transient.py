"""Regression tests for dispatch error classification and bounded retries."""

from __future__ import annotations

import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_team.cluster import (
    DISPATCH_TRANSIENT_MAX_DEFERRALS,
    ClusterManager,
)
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


class DispatchClassificationTests(unittest.TestCase):
    def test_permanent_errors_are_not_transient(self) -> None:
        for exc in (
            PermissionError("permission denied"),
            FileNotFoundError("no such file"),
            NotADirectoryError("not a directory"),
            sqlite3.OperationalError("no such column: foo"),
            sqlite3.OperationalError("no such table: bar"),
        ):
            self.assertFalse(
                ClusterManager._dispatch_error_is_transient(exc),
                f"{exc!r} should be permanent",
            )

    def test_recoverable_errors_are_transient(self) -> None:
        for exc in (
            ConnectionError("connection reset"),
            TimeoutError("timed out"),
            sqlite3.OperationalError("database is locked"),
            sqlite3.OperationalError("database is busy"),
        ):
            self.assertTrue(
                ClusterManager._dispatch_error_is_transient(exc),
                f"{exc!r} should be transient",
            )


class DispatchRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.email", "dispatch@example.invalid")
        git(self.repo, "config", "user.name", "Dispatch Test")
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
            self.settings,
            team=self.team,
            runtime_mode="exec",
            agent_command=["codex"],
        )
        goal = self.manager.create_goal(
            self.repo, objective="Dispatch retry fixture", autonomy="auto"
        )
        self.goal_id = str(goal["goal_id"])
        self.manager.set_goal_status(self.goal_id, "active")
        task = self.team.create_task(
            self.repo,
            title="Dispatch target",
            description="fixture",
            acceptance="fixture",
            kind="read",
            owner_role="team_explorer",
            goal_id=self.goal_id,
        )
        self.task_id = str(task["task_id"])

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _goal_and_tasks(self) -> tuple[dict, list[dict]]:
        return self.manager.get_goal(self.goal_id), [
            self.team.get_task(self.task_id)
        ]

    def test_permanent_error_blocks_immediately(self) -> None:
        goal, tasks = self._goal_and_tasks()
        with patch.object(
            self.manager,
            "_dispatch_worker",
            side_effect=PermissionError("permission denied"),
        ):
            job, changed, keep = self.manager._dispatch_next_job(goal, tasks)
        self.assertIsNone(job)
        self.assertEqual(self.team.get_task(self.task_id)["status"], "blocked")
        self.assertNotIn(self.task_id, self.manager._dispatch_deferrals)

    def test_transient_error_defers_then_blocks_after_cap(self) -> None:
        with patch.object(
            self.manager,
            "_dispatch_worker",
            side_effect=ConnectionError("connection reset"),
        ):
            for attempt in range(1, DISPATCH_TRANSIENT_MAX_DEFERRALS + 1):
                goal, tasks = self._goal_and_tasks()
                job, changed, keep = self.manager._dispatch_next_job(goal, tasks)
                self.assertIsNone(job)
                self.assertEqual(
                    self.team.get_task(self.task_id)["status"], "ready"
                )
                self.assertEqual(
                    self.manager._dispatch_deferrals[self.task_id]["count"],
                    attempt,
                )
                # Skip the backoff so the next attempt is evaluated immediately.
                self.manager._dispatch_deferrals[self.task_id]["retry_after"] = 0
            # One more transient failure exhausts the cap and blocks.
            goal, tasks = self._goal_and_tasks()
            job, changed, keep = self.manager._dispatch_next_job(goal, tasks)
            self.assertIsNone(job)
            self.assertEqual(
                self.team.get_task(self.task_id)["status"], "blocked"
            )
            self.assertNotIn(self.task_id, self.manager._dispatch_deferrals)

    def test_backoff_skips_dispatch(self) -> None:
        import time

        self.manager._dispatch_deferrals[self.task_id] = {
            "count": 1,
            "retry_after": time.monotonic() + 3600,
        }
        goal, tasks = self._goal_and_tasks()
        with patch.object(
            self.manager, "_dispatch_worker"
        ) as dispatch_mock:
            job, changed, keep = self.manager._dispatch_next_job(goal, tasks)
        dispatch_mock.assert_not_called()
        self.assertIsNone(job)
        self.assertFalse(changed)
        self.assertEqual(self.team.get_task(self.task_id)["status"], "ready")


if __name__ == "__main__":
    unittest.main()