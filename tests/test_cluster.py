from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from codex_team.cluster import (
    ClusterManager,
    _redact_sensitive_file,
    _redact_sensitive_log,
)
from codex_team.config import Settings
from codex_team.db import utc_now
from codex_team.scheduling import (
    DEFAULT_GLOBAL_JOB_CAPACITY,
    DEFAULT_GOAL_WORKER_CAPACITY,
    DEFAULT_HEARTBEAT_CRITICAL_SECONDS,
    DEFAULT_HEARTBEAT_WARNING_SECONDS,
    DEFAULT_LEAD_TIMEOUT_SECONDS,
    DEFAULT_WORKER_TIMEOUT_SECONDS,
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
    git(repo, "config", "user.email", "cluster-test@example.invalid")
    git(repo, "config", "user.name", "Cluster Test")
    return repo


def _iso_seconds_ago(seconds: int) -> str:
    return (
        (datetime.now(UTC) - timedelta(seconds=seconds))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


class ClusterSchedulerTests(unittest.TestCase):
    def _goal_with_ready_tasks(
        self,
        manager: ClusterManager,
        team: TeamService,
        repo: Path,
        count: int,
    ) -> tuple[dict, list[dict]]:
        goal = manager.create_goal(repo, objective=f"Run {count} fair tasks")
        tasks = [
            team.create_task(
                repo,
                title=f"Ready task {index}",
                description=f"Exercise scheduler task {index}.",
                acceptance="Scheduler dispatch is observed.",
                kind="read",
                owner_role="team_explorer",
                actor="lead",
                goal_id=goal["goal_id"],
            )
            for index in range(count)
        ]
        return goal, tasks

    def _insert_job(
        self,
        team: TeamService,
        goal_id: str,
        *,
        job_id: str,
        kind: str,
        status: str,
        started_at: str,
        task_id: str | None = None,
        pid: int | None = None,
    ) -> None:
        with team.connection() as connection:
            connection.execute(
                """
                INSERT INTO cluster_jobs(
                    job_id, goal_id, task_id, kind, role, status, pid,
                    attempt, log_path, output_path, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    goal_id,
                    task_id,
                    kind,
                    kind,
                    status,
                    pid,
                    str(team.settings.logs_dir / f"{job_id}.jsonl"),
                    str(team.settings.logs_dir / f"{job_id}.md"),
                    started_at,
                    None if status in {"starting", "running"} else started_at,
                ),
            )

    def _fake_running_dispatch(
        self,
        manager: ClusterManager,
        team: TeamService,
        dispatched: list[tuple[str, str]],
    ):
        def dispatch(goal: dict, task: dict) -> dict:
            sequence = len(dispatched) + 1
            job_id = f"J-TEST-{sequence}"
            started_at = utc_now()
            with team.connection() as connection:
                connection.execute(
                    """
                    UPDATE tasks
                    SET status = 'running', claimed_by = 'test-worker',
                        updated_at = ?, version = version + 1
                    WHERE task_id = ?
                    """,
                    (started_at, task["task_id"]),
                )
                connection.execute(
                    """
                    INSERT INTO cluster_jobs(
                        job_id, goal_id, task_id, kind, role, status, pid,
                        attempt, log_path, output_path, started_at
                    ) VALUES (?, ?, ?, 'worker', 'team_explorer', 'running', ?,
                              1, ?, ?, ?)
                    """,
                    (
                        job_id,
                        goal["goal_id"],
                        task["task_id"],
                        os.getpid(),
                        str(team.settings.logs_dir / f"{job_id}.jsonl"),
                        str(team.settings.logs_dir / f"{job_id}.md"),
                        started_at,
                    ),
                )
                manager._event(
                    connection,
                    goal["goal_id"],
                    "job_started",
                    "teamd",
                    {
                        "job_id": job_id,
                        "task_id": task["task_id"],
                        "kind": "worker",
                    },
                )
            dispatched.append((goal["goal_id"], task["task_id"]))
            return {
                "job_id": job_id,
                "goal_id": goal["goal_id"],
                "task_id": task["task_id"],
                "kind": "worker",
            }

        return dispatch

    def test_cluster_logs_redact_claim_tokens(self) -> None:
        secret = "claim-secret-value"
        direct = '{"claim_token":"' + secret + '","ok":true}'
        escaped = '{"text":"{\\"claim_token\\":\\"' + secret + '\\"}"}'
        self.assertNotIn(secret, _redact_sensitive_log(direct))
        self.assertNotIn(secret, _redact_sensitive_log(escaped))
        self.assertIn("[REDACTED]", _redact_sensitive_log(direct))
        self.assertIn("[REDACTED]", _redact_sensitive_log(escaped))

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "job.jsonl"
            path.write_text(escaped + "\n", encoding="utf-8")
            self.assertTrue(_redact_sensitive_file(path))
            self.assertNotIn(secret, path.read_text(encoding="utf-8"))
            self.assertFalse(_redact_sensitive_file(path))

    def test_write_and_auto_lead_jobs_use_git_writable_sandbox(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = Settings(
                project_root=Path(__file__).resolve().parents[1],
                home=base / "team-home",
                db_path=base / "team-home" / "team.sqlite",
                logs_dir=base / "team-home" / "logs",
            )
            manager = ClusterManager(
                settings,
                team=TeamService(settings),
                agent_command=["codex"],
            )
            output = base / "last.md"
            initial = manager._build_command(
                base,
                output,
                resume_thread_id=None,
                sandbox_mode="danger-full-access",
            )
            resumed = manager._build_command(
                base,
                output,
                resume_thread_id="thread-123",
                sandbox_mode="danger-full-access",
            )

            self.assertIn("danger-full-access", initial)
            self.assertIn('sandbox_mode="danger-full-access"', resumed)
            self.assertNotIn("workspace-write", initial)
            self.assertIn(
                "team_heartbeat",
                manager.team.role_prompt("team_reviewer")["prompt"],
            )
            self.assertEqual(
                manager._worker_sandbox_mode({"kind": "read"}),
                "read-only",
            )
            self.assertEqual(
                manager._worker_sandbox_mode({"kind": "review"}),
                "read-only",
            )
            self.assertEqual(
                manager._worker_sandbox_mode({"kind": "write"}),
                "danger-full-access",
            )

    def test_lead_resume_worker_retry_and_goal_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repo = base / "repo"
            repo.mkdir()
            git(repo, "init", "-b", "main")
            git(repo, "config", "user.email", "cluster-test@example.invalid")
            git(repo, "config", "user.name", "Cluster Test")
            (repo / "README.md").write_text("# Cluster test\n", encoding="utf-8")
            git(repo, "add", "README.md")
            git(repo, "commit", "-m", "initial")
            settings = Settings(
                project_root=Path(__file__).resolve().parents[1],
                home=base / "team-home",
                db_path=base / "team-home" / "team.sqlite",
                logs_dir=base / "team-home" / "logs",
            )
            team = TeamService(settings)
            team.init_project(repo)
            git(repo, "add", ".codex", "AGENTS.md")
            git(repo, "commit", "-m", "install team policy")
            fake_agent = Path(__file__).resolve().parent / "fake_codex_agent.py"
            manager = ClusterManager(
                settings,
                team=team,
                agent_command=[sys.executable, str(fake_agent)],
                max_workers=2,
                lead_cooldown_seconds=0,
            )
            with patch.dict(
                os.environ,
                {
                    "CODEX_TEAM_HOME": str(settings.home),
                    "PYTHONPATH": str(settings.project_root / "src"),
                },
            ):
                goal = manager.create_goal(
                    repo,
                    objective="Create a greeting file automatically.",
                    autonomy="auto",
                    max_worker_attempts=3,
                    max_lead_attempts=5,
                )
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    manager.tick()
                    goal = manager.get_goal(goal["goal_id"])
                    if goal["status"] in {"completed", "failed"}:
                        break
                    time.sleep(0.08)

            self.assertEqual(goal["status"], "completed")
            self.assertEqual(goal["tasks"][0]["status"], "done")
            lead_jobs = [job for job in goal["jobs"] if job["kind"] == "lead"]
            worker_jobs = [job for job in goal["jobs"] if job["kind"] == "worker"]
            self.assertGreaterEqual(len(lead_jobs), 3)
            self.assertGreaterEqual(len(worker_jobs), 2)
            resumed = sorted(
                [job for job in lead_jobs if job["thread_id"]],
                key=lambda item: item["attempt"],
            )
            self.assertEqual(resumed[0]["thread_id"], resumed[1]["thread_id"])
            self.assertIn(
                "worker_recovered",
                [event["event_type"] for event in goal["events"]],
            )

    def test_global_capacity_and_round_robin_are_fair_across_goals(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            manager = ClusterManager(
                settings,
                team=team,
                agent_command=["codex"],
                max_workers=DEFAULT_GOAL_WORKER_CAPACITY,
                max_jobs=4,
                lead_cooldown_seconds=0,
            )
            first_repo = _repo(base, "first")
            second_repo = _repo(base, "second")
            first_goal, _ = self._goal_with_ready_tasks(
                manager,
                team,
                first_repo,
                3,
            )
            second_goal, _ = self._goal_with_ready_tasks(
                manager,
                team,
                second_repo,
                3,
            )
            dispatched: list[tuple[str, str]] = []
            with patch.object(
                manager,
                "_dispatch_worker",
                side_effect=self._fake_running_dispatch(
                    manager,
                    team,
                    dispatched,
                ),
            ):
                result = manager.tick()

            self.assertEqual(len(result["scheduled"]), 4)
            self.assertEqual(manager._active_job_count(), 4)
            goal_ids = [goal_id for goal_id, _ in dispatched]
            self.assertEqual(goal_ids.count(first_goal["goal_id"]), 2)
            self.assertEqual(goal_ids.count(second_goal["goal_id"]), 2)
            self.assertEqual(
                set(goal_ids[:2]),
                {first_goal["goal_id"], second_goal["goal_id"]},
            )
            self.assertEqual(
                set(goal_ids[2:]),
                {first_goal["goal_id"], second_goal["goal_id"]},
            )

            for goal in (first_goal, second_goal):
                events = manager.get_goal(goal["goal_id"])["events"]
                deferred = [
                    event
                    for event in events
                    if event["event_type"] == "fairness_deferred"
                ]
                self.assertEqual(len(deferred), 1)
                self.assertEqual(deferred[0]["data"]["capacity"], 4)
                self.assertEqual(
                    deferred[0]["data"]["reason"],
                    "global capacity full",
                )
                self.assertEqual(deferred[0]["data"]["ready_tasks"], 1)

    def test_existing_active_job_fills_capacity_and_deferral_is_deduplicated(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            manager = ClusterManager(
                settings,
                team=team,
                agent_command=["codex"],
                max_jobs=1,
                lead_cooldown_seconds=0,
            )
            busy_repo = _repo(base, "busy")
            waiting_repo = _repo(base, "waiting")
            busy_goal = manager.create_goal(busy_repo, objective="Own the slot")
            waiting_goal, _ = self._goal_with_ready_tasks(
                manager,
                team,
                waiting_repo,
                2,
            )
            self._insert_job(
                team,
                busy_goal["goal_id"],
                job_id="J-ACTIVE-LEAD",
                kind="lead",
                status="running",
                started_at=utc_now(),
                pid=os.getpid(),
            )
            dispatched: list[tuple[str, str]] = []
            with patch.object(
                manager,
                "_dispatch_worker",
                side_effect=self._fake_running_dispatch(
                    manager,
                    team,
                    dispatched,
                ),
            ):
                first = manager.tick()
                second = manager.tick()

            self.assertEqual(first["scheduled"], [])
            self.assertEqual(second["scheduled"], [])
            self.assertEqual(dispatched, [])
            self.assertEqual(manager._active_job_count(), 1)
            events = manager.get_goal(waiting_goal["goal_id"])["events"]
            deferred = [
                event
                for event in events
                if event["event_type"] == "fairness_deferred"
            ]
            self.assertEqual(len(deferred), 1)
            self.assertEqual(deferred[0]["data"]["ready_tasks"], 2)
            self.assertEqual(deferred[0]["data"]["capacity"], 1)
            with team.connection() as connection:
                connection.execute(
                    """
                    UPDATE cluster_events
                    SET created_at = ?
                    WHERE goal_id = ? AND event_type = 'fairness_deferred'
                    """,
                    (_iso_seconds_ago(61), waiting_goal["goal_id"]),
                )
            third = manager.tick()
            self.assertEqual(third["scheduled"], [])
            events = manager.get_goal(waiting_goal["goal_id"])["events"]
            deferred = [
                event
                for event in events
                if event["event_type"] == "fairness_deferred"
            ]
            self.assertEqual(len(deferred), 2)

    def test_per_goal_worker_capacity_stops_at_default_three(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            manager = ClusterManager(
                settings,
                team=team,
                agent_command=["codex"],
                max_jobs=6,
                lead_cooldown_seconds=0,
            )
            repo = _repo(base, "worker-cap")
            goal, _ = self._goal_with_ready_tasks(
                manager,
                team,
                repo,
                5,
            )
            dispatched: list[tuple[str, str]] = []
            with patch.object(
                manager,
                "_dispatch_worker",
                side_effect=self._fake_running_dispatch(
                    manager,
                    team,
                    dispatched,
                ),
            ):
                result = manager.tick()

            self.assertEqual(len(result["scheduled"]), 3)
            self.assertEqual(len(dispatched), 3)
            self.assertTrue(
                all(goal_id == goal["goal_id"] for goal_id, _ in dispatched)
            )
            self.assertEqual(manager._active_job_count(), 3)

    def test_least_recently_started_goal_runs_first(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            manager = ClusterManager(
                settings,
                team=team,
                agent_command=["codex"],
                max_jobs=1,
                lead_cooldown_seconds=0,
            )
            recent_repo = _repo(base, "recent")
            old_repo = _repo(base, "old")
            recent_goal, _ = self._goal_with_ready_tasks(
                manager,
                team,
                recent_repo,
                1,
            )
            old_goal, _ = self._goal_with_ready_tasks(
                manager,
                team,
                old_repo,
                1,
            )
            self._insert_job(
                team,
                recent_goal["goal_id"],
                job_id="J-RECENT-LEAD",
                kind="lead",
                status="completed",
                started_at=_iso_seconds_ago(10),
            )
            self._insert_job(
                team,
                old_goal["goal_id"],
                job_id="J-OLD-LEAD",
                kind="lead",
                status="completed",
                started_at=_iso_seconds_ago(3600),
            )
            dispatched: list[tuple[str, str]] = []
            with patch.object(
                manager,
                "_dispatch_worker",
                side_effect=self._fake_running_dispatch(
                    manager,
                    team,
                    dispatched,
                ),
            ):
                result = manager.tick()

            self.assertEqual(len(result["scheduled"]), 1)
            self.assertEqual(dispatched[0][0], old_goal["goal_id"])

    def test_scheduler_env_limits_and_long_timeouts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            environment = {
                "CODEX_TEAM_CLUSTER_MAX_JOBS": "6",
                "CODEX_TEAM_CLUSTER_MAX_WORKERS": "2",
                "CODEX_TEAM_WORKER_TIMEOUT_SECONDS": "4800",
                "CODEX_TEAM_LEAD_TIMEOUT_SECONDS": "1800",
                "CODEX_TEAM_HEARTBEAT_WARNING_SECONDS": "120",
                "CODEX_TEAM_HEARTBEAT_CRITICAL_SECONDS": "60",
            }
            with patch.dict(os.environ, environment):
                manager = ClusterManager(
                    settings,
                    team=TeamService(settings),
                    agent_command=["codex"],
                )
            self.assertEqual(manager.max_jobs, 6)
            self.assertEqual(manager.max_workers, 2)
            self.assertEqual(manager.worker_timeout_seconds, 4800)
            self.assertEqual(manager.lead_timeout_seconds, 1800)
            self.assertEqual(manager.heartbeat_warning_seconds, 120)
            self.assertEqual(manager.heartbeat_critical_seconds, 120)
            old_job = {
                "kind": "worker",
                "started_at": _iso_seconds_ago(4700),
            }
            timed_out_job = {
                "kind": "worker",
                "started_at": _iso_seconds_ago(4900),
            }
            timed_out_lead = {
                "kind": "lead",
                "started_at": _iso_seconds_ago(1900),
            }
            self.assertFalse(manager._job_timed_out(old_job))
            self.assertTrue(manager._job_timed_out(timed_out_job))
            self.assertTrue(manager._job_timed_out(timed_out_lead))
            self.assertEqual(
                DEFAULT_WORKER_TIMEOUT_SECONDS,
                2 * 60 * 60,
            )
            self.assertEqual(
                DEFAULT_LEAD_TIMEOUT_SECONDS,
                45 * 60,
            )
            self.assertEqual(DEFAULT_GLOBAL_JOB_CAPACITY, 4)
            with patch.dict(
                os.environ,
                {
                    "CODEX_TEAM_CLUSTER_MAX_JOBS": "999",
                    "CODEX_TEAM_CLUSTER_MAX_WORKERS": "999",
                },
            ):
                capped = ClusterManager(
                    settings,
                    team=TeamService(settings),
                    agent_command=["codex"],
                )
            self.assertEqual(capped.max_jobs, 16)
            self.assertEqual(capped.max_workers, 8)

    def test_goal_does_not_complete_while_a_lead_job_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            manager = ClusterManager(
                settings,
                team=team,
                agent_command=["codex"],
            )
            repo = _repo(base, "goal-completion")
            goal, tasks = self._goal_with_ready_tasks(
                manager,
                team,
                repo,
                1,
            )
            manager.set_goal_status(goal["goal_id"], "active")
            with team.connection() as connection:
                connection.execute(
                    "UPDATE tasks SET status = 'done' WHERE task_id = ?",
                    (tasks[0]["task_id"],),
                )
            self._insert_job(
                team,
                goal["goal_id"],
                job_id="J-ACTIVE-LEAD",
                kind="lead",
                status="running",
                started_at=_iso_seconds_ago(5),
                pid=os.getpid(),
            )

            self.assertTrue(manager._prepare_goal(manager.get_goal(goal["goal_id"])))
            self.assertEqual(
                manager.get_goal(goal["goal_id"])["status"],
                "active",
            )

            with team.connection() as connection:
                connection.execute(
                    """
                    UPDATE cluster_jobs
                    SET status = 'completed', finished_at = ?
                    WHERE job_id = 'J-ACTIVE-LEAD'
                    """,
                    (utc_now(),),
                )
            self.assertFalse(manager._prepare_goal(manager.get_goal(goal["goal_id"])))
            self.assertEqual(
                manager.get_goal(goal["goal_id"])["status"],
                "completed",
            )

    def test_ensure_goal_reuses_active_goal_and_never_resumes_paused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            manager = ClusterManager(
                settings,
                team=team,
                agent_command=["codex"],
            )
            repo = _repo(base, "ensure-goal")

            first = manager.ensure_goal(
                repo,
                objective="Implement the feature cluster",
            )
            second = manager.ensure_goal(
                repo,
                objective="Continue the same feature cluster",
            )

            self.assertTrue(first["created"])
            self.assertFalse(first["reused"])
            self.assertFalse(second["created"])
            self.assertTrue(second["reused"])
            self.assertEqual(
                first["goal"]["goal_id"],
                second["goal"]["goal_id"],
            )
            manager.set_goal_status(first["goal"]["goal_id"], "paused")
            paused = manager.ensure_goal(
                repo,
                objective="Must not auto-resume",
            )
            self.assertFalse(paused["created"])
            self.assertTrue(paused["reused"])
            self.assertTrue(paused["requires_resume"])
            self.assertEqual(
                paused["goal"]["goal_id"],
                first["goal"]["goal_id"],
            )
            self.assertEqual(paused["goal"]["status"], "paused")
            self.assertIn(
                "goal_reused",
                [
                    event["event_type"]
                    for event in manager.get_goal(first["goal"]["goal_id"])["events"]
                ],
            )

    def test_worker_heartbeat_warning_and_critical_alert_without_kill(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            manager = ClusterManager(
                settings,
                team=team,
                agent_command=["codex"],
                lead_cooldown_seconds=0,
            )
            repo = _repo(base, "health")
            goal, tasks = self._goal_with_ready_tasks(
                manager,
                team,
                repo,
                1,
            )
            task = tasks[0]
            team.claim_task(task["task_id"], actor="health-worker")
            self._insert_job(
                team,
                goal["goal_id"],
                job_id="J-HEALTH",
                task_id=task["task_id"],
                kind="worker",
                status="running",
                started_at=_iso_seconds_ago(20 * 60),
                pid=os.getpid(),
            )
            with team.connection() as connection:
                connection.execute(
                    """
                    UPDATE task_events
                    SET created_at = ?
                    WHERE task_id = ? AND event_type = 'claimed'
                    """,
                    (_iso_seconds_ago(20 * 60), task["task_id"]),
                )
                team._event(
                    connection,
                    task["task_id"],
                    "progress",
                    "health-worker",
                    {"note": "recent progress"},
                )

            manager.tick()
            events = manager.get_goal(goal["goal_id"])["events"]
            self.assertNotIn(
                "worker_heartbeat_warning",
                [event["event_type"] for event in events],
            )

            with team.connection() as connection:
                connection.execute(
                    """
                    UPDATE task_events
                    SET created_at = ?
                    WHERE task_id = ? AND event_type = 'progress'
                    """,
                    (_iso_seconds_ago(6 * 60), task["task_id"]),
                )
            manager.tick()
            manager.tick()
            events = manager.get_goal(goal["goal_id"])["events"]
            warnings = [
                event
                for event in events
                if event["event_type"] == "worker_heartbeat_warning"
            ]
            self.assertEqual(len(warnings), 1)
            self.assertEqual(warnings[0]["data"]["action"], "alert")
            self.assertEqual(warnings[0]["data"]["severity"], "warning")

            with team.connection() as connection:
                connection.execute(
                    """
                    UPDATE task_events
                    SET created_at = ?
                    WHERE task_id = ? AND event_type = 'progress'
                    """,
                    (_iso_seconds_ago(16 * 60), task["task_id"]),
                )
            manager.tick()
            events = manager.get_goal(goal["goal_id"])["events"]
            critical = [
                event
                for event in events
                if event["event_type"] == "worker_heartbeat_critical"
            ]
            self.assertEqual(len(critical), 1)
            self.assertEqual(critical[0]["data"]["action"], "alert")
            self.assertEqual(critical[0]["data"]["severity"], "critical")
            self.assertEqual(
                team.get_task(task["task_id"])["status"],
                "running",
            )


if __name__ == "__main__":
    unittest.main()
