from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from codex_team.cluster import (
    _ClusterAppServerEvents,
    ClusterManager,
    _redact_sensitive_file,
    _redact_sensitive_log,
)
from codex_team.agent_pool import AgentPool
from codex_team.app_server import AppServerRuntime
from codex_team.cli import build_parser
from codex_team.config import Settings
from codex_team.db import utc_now
from codex_team.mcp import TeamMcpServer, _tool_definitions
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


def _wait_for(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met before timeout")


class StubAppServerRuntime:
    def __init__(
        self,
        *,
        fail_start: int = 0,
        fail_restart: int = 0,
    ) -> None:
        self.fail_start = fail_start
        self.fail_restart = fail_restart
        self.start_calls = 0
        self.restart_calls = 0
        self.stop_calls = 0
        self.start_threads: list[tuple[str, str | None]] = []
        self.resume_threads: list[str] = []
        self.turns: list[dict[str, object]] = []
        self.interrupts: list[tuple[str, str]] = []
        self.unsubscribed: list[str] = []
        self.event_handler: object | None = None
        self._thread_sequence = 0
        self._turn_sequence = 0
        self._lock = threading.Lock()

    def start(self) -> dict:
        self.start_calls += 1
        if self.start_calls <= self.fail_start:
            raise RuntimeError("startup failed")
        return self.health()

    def restart(self) -> dict:
        self.restart_calls += 1
        if self.restart_calls <= self.fail_restart:
            raise RuntimeError("restart failed")
        return self.health()

    def stop(self) -> None:
        self.stop_calls += 1

    def health(self) -> dict:
        return {"healthy": True, "running": True}

    def start_thread(
        self,
        cwd: Path,
        developer_instructions: str | None = None,
        model: str | None = None,
        model_provider: str | None = None,
        service_name: str | None = None,
    ) -> str:
        with self._lock:
            self._thread_sequence += 1
            thread_id = f"stub-thread-{self._thread_sequence}"
            self.start_threads.append((str(cwd), developer_instructions))
            return thread_id

    def resume_thread(self, thread_id: str, exclude_turns: bool = True) -> str:
        self.resume_threads.append(thread_id)
        return thread_id

    def start_turn(
        self,
        thread_id: str,
        input_text: str,
        cwd: Path | None = None,
        sandbox_mode: object = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> dict[str, str]:
        with self._lock:
            self._turn_sequence += 1
            turn_id = f"stub-turn-{self._turn_sequence}"
            self.turns.append(
                {
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "input": input_text,
                    "cwd": str(cwd) if cwd is not None else None,
                    "sandbox_mode": sandbox_mode,
                }
            )
            return {"turn_id": turn_id, "status": "inProgress"}

    def interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        self.interrupts.append((thread_id, turn_id))

    def unsubscribe_thread(self, thread_id: str) -> str:
        self.unsubscribed.append(thread_id)
        return "unsubscribed"


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
                "workspace-write",
            )
            self.assertEqual(
                manager._worker_sandbox_mode({"kind": "review"}),
                "workspace-write",
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

    def test_each_goal_gets_three_workers_without_a_shared_four_job_pool(
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
                max_workers=DEFAULT_GOAL_WORKER_CAPACITY,
                max_jobs=DEFAULT_GLOBAL_JOB_CAPACITY,
                lead_cooldown_seconds=0,
            )
            first_repo = _repo(base, "first-workers")
            second_repo = _repo(base, "second-workers")
            first_goal, _ = self._goal_with_ready_tasks(
                manager,
                team,
                first_repo,
                4,
            )
            second_goal, _ = self._goal_with_ready_tasks(
                manager,
                team,
                second_repo,
                4,
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

            self.assertEqual(len(result["scheduled"]), 6)
            first_count = sum(
                goal_id == first_goal["goal_id"]
                for goal_id, _task_id in dispatched
            )
            second_count = sum(
                goal_id == second_goal["goal_id"]
                for goal_id, _task_id in dispatched
            )
            self.assertEqual(first_count, 3)
            self.assertEqual(second_count, 3)
            self.assertEqual(manager._active_job_count(), 6)

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
            self.assertEqual(DEFAULT_GLOBAL_JOB_CAPACITY, 16)
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
            self.assertEqual(capped.max_jobs, 32)
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

    def test_fake_app_server_reuses_thread_across_worktrees_with_turn_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            repo = _repo(base, "app-server-reuse")
            (repo / "README.md").write_text("# App Server\n", encoding="utf-8")
            git(repo, "add", "README.md")
            git(repo, "commit", "-m", "initial")
            team.init_project(repo)
            git(repo, "add", ".codex", "AGENTS.md")
            git(repo, "commit", "-m", "install team policy")

            trace_path = base / "app-server-trace.jsonl"
            runtime = AppServerRuntime(
                [
                    sys.executable,
                    str(Path(__file__).with_name("fake_app_server.py")),
                ],
                request_timeout=2.0,
                startup_timeout=2.0,
                env={
                    "PYTHONUNBUFFERED": "1",
                    "FAKE_APP_SERVER_TRACE": str(trace_path),
                },
            )
            self.addCleanup(runtime.stop)
            manager = ClusterManager(
                settings,
                team=team,
                runtime=runtime,
                runtime_mode="app_server",
                max_workers=2,
            )
            runtime.event_handler = _ClusterAppServerEvents(manager)
            goal = manager.create_goal(
                repo,
                objective="Exercise App Server thread reuse",
                workstream_key="feature-cache",
                continuity_reason="related implementation turns",
            )
            tasks = [
                team.create_task(
                    repo,
                    title=f"Reuse write task {index}",
                    description="Exercise a different worktree on one thread.",
                    acceptance="The turn uses the current worktree.",
                    kind="write",
                    owner_role="team_worker",
                    actor="lead",
                    goal_id=goal["goal_id"],
                    continuity_mode="reuse",
                )
                for index in (1, 2)
            ]

            first = manager._dispatch_worker(
                manager.get_goal(goal["goal_id"]),
                manager.team.get_task(tasks[0]["task_id"]),
            )
            _wait_for(
                lambda: manager.app_turns.get(first["job_id"], {}).get("status")
                == "completed"
            )
            with team.connection() as connection:
                connection.execute(
                    """
                    UPDATE tasks
                    SET status = 'review', updated_at = ?, version = version + 1
                    WHERE task_id = ?
                    """,
                    (utc_now(), tasks[0]["task_id"]),
                )
            manager._reconcile_jobs()
            first_job = next(
                item
                for item in manager.get_goal(goal["goal_id"])["jobs"]
                if item["job_id"] == first["job_id"]
            )
            self.assertEqual(first_job["status"], "completed")
            self.assertEqual(first_job["input_tokens"], 120)
            self.assertEqual(first_job["cached_input_tokens"], 20)
            self.assertEqual(first_job["uncached_input_tokens"], 100)
            self.assertEqual(first_job["first_input_tokens"], 120)
            self.assertEqual(first_job["context_tokens"], 75)
            self.assertEqual(first_job["model_context_window"], 200)

            second = manager._dispatch_worker(
                manager.get_goal(goal["goal_id"]),
                manager.team.get_task(tasks[1]["task_id"]),
            )
            _wait_for(
                lambda: manager.app_turns.get(second["job_id"], {}).get("status")
                == "completed"
            )
            messages = [
                json.loads(line)
                for line in trace_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            starts = [
                item for item in messages if item.get("method") == "thread/start"
            ]
            resumes = [
                item for item in messages if item.get("method") == "thread/resume"
            ]
            turns = [
                item for item in messages if item.get("method") == "turn/start"
            ]
            self.assertEqual(len(starts), 1)
            self.assertEqual(len(resumes), 1)
            self.assertEqual(len(turns), 2)
            instructions = starts[0]["params"]["developerInstructions"]
            self.assertNotIn(goal["goal_id"], instructions)
            self.assertNotIn(tasks[0]["task_id"], instructions)
            self.assertNotIn(str(Path(settings.logs_dir)), instructions)
            self.assertIn(
                goal["goal_id"],
                turns[0]["params"]["input"][0]["text"],
            )
            self.assertIn(
                tasks[0]["task_id"],
                turns[0]["params"]["input"][0]["text"],
            )
            self.assertNotEqual(
                turns[0]["params"]["cwd"],
                turns[1]["params"]["cwd"],
            )
            self.assertEqual(
                turns[0]["params"]["sandboxPolicy"]["type"],
                "dangerFullAccess",
            )
            self.assertEqual(
                turns[1]["params"]["sandboxPolicy"]["type"],
                "dangerFullAccess",
            )
            agent = manager.list_agents(str(goal["project_id"]))[0]
            self.assertEqual(agent["thread_id"], first["thread_id"])
            self.assertEqual(agent["last_input_tokens"], 120)
            self.assertEqual(agent["last_uncached_input_tokens"], 100)

    @unittest.skipUnless(
        os.environ.get("CODEX_TEAM_REAL_APP_SERVER_TEST") == "1",
        "set CODEX_TEAM_REAL_APP_SERVER_TEST=1 to run the installed App Server",
    )
    def test_installed_codex_app_server_thread_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            runtime = AppServerRuntime(
                request_timeout=20.0,
                startup_timeout=20.0,
                stop_timeout=5.0,
            )
            self.addCleanup(runtime.stop)
            started = runtime.start()
            self.assertTrue(started["healthy"])
            thread_id = runtime.start_thread(
                workspace,
                developer_instructions="Stable integration test instructions.",
                service_name="codex-team-integration-test",
            )
            self.assertIn(thread_id, runtime.list_loaded_threads())
            self.assertEqual(runtime.unsubscribe_thread(thread_id), "unsubscribed")

    def test_context_overflow_resets_agent_and_retries_new_thread_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            repo = _repo(base, "context-reset")
            project = team.init_project(repo)
            runtime = StubAppServerRuntime()
            manager = ClusterManager(
                settings,
                team=team,
                runtime=runtime,
                runtime_mode="app_server",
            )
            goal = manager.create_goal(repo, objective="Reset context")
            task = team.create_task(
                repo,
                title="Explore context",
                description="Trigger one context reset.",
                acceptance="A second thread is used once.",
                kind="read",
                owner_role="team_explorer",
                goal_id=goal["goal_id"],
            )
            first = manager._dispatch_worker(
                manager.get_goal(goal["goal_id"]),
                team.get_task(task["task_id"]),
            )
            manager.app_turns[first["job_id"]]["context_window_exceeded"] = True
            manager.app_turns[first["job_id"]]["status"] = "failed"
            manager.app_turns[first["job_id"]]["error"] = {
                "codexErrorInfo": "contextWindowExceeded",
                "message": "contextWindowExceeded",
            }
            manager._reconcile_jobs()
            self.assertEqual(
                team.get_task(task["task_id"])["status"],
                "ready",
            )
            failed_agent = AgentPool(settings).list_agents(
                project["project_id"]
            )[0]
            self.assertEqual(failed_agent["status"], "failed")
            self.assertIsNotNone(failed_agent["reset_requested_at"])

            second = manager._dispatch_worker(
                manager.get_goal(goal["goal_id"]),
                team.get_task(task["task_id"]),
            )
            self.assertNotEqual(first["thread_id"], second["thread_id"])
            manager.app_turns[second["job_id"]]["context_window_exceeded"] = True
            manager.app_turns[second["job_id"]]["status"] = "failed"
            manager.app_turns[second["job_id"]]["error"] = {
                "codexErrorInfo": "contextWindowExceeded",
                "message": "contextWindowExceeded",
            }
            manager._reconcile_jobs()
            self.assertEqual(
                team.get_task(task["task_id"])["status"],
                "blocked",
            )
            goal_result = manager.get_goal(goal["goal_id"])
            resets = [
                event
                for event in goal_result["events"]
                if event["event_type"] == "agent_context_reset"
            ]
            self.assertEqual(len(resets), 2)
            self.assertEqual(len(runtime.start_threads), 2)

    def test_related_goals_reuse_lead_thread_by_workstream(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            repo = _repo(base, "related-goals")
            project = team.init_project(repo)
            runtime = StubAppServerRuntime()
            manager = ClusterManager(
                settings,
                team=team,
                runtime=runtime,
                runtime_mode="app_server",
                lead_cooldown_seconds=0,
            )
            first = manager.create_goal(
                repo,
                objective="First related stage",
                workstream_key="feature-cache",
                continuity_reason="same feature staged work",
            )
            first_job = manager._dispatch_lead(first, [])
            manager.app_turns[first_job["job_id"]]["status"] = "completed"
            manager._reconcile_jobs()
            first_goal = manager.get_goal(first["goal_id"])
            self.assertTrue(first_goal["lead_thread_id"])
            stored = AgentPool(settings).list_agents(project["project_id"])
            self.assertEqual(stored[0]["status"], "stored")
            manager.set_goal_status(
                first["goal_id"],
                "completed",
                reason="first stage complete",
            )

            second = manager.create_goal(
                repo,
                objective="Second related stage",
                workstream_key="feature-cache",
                continuity_reason="same feature staged work",
            )
            manager._dispatch_lead(second, [])
            self.assertEqual(len(runtime.start_threads), 1)
            self.assertEqual(runtime.resume_threads, [first_job["thread_id"]])
            instructions = runtime.start_threads[0][1] or ""
            self.assertNotIn(first["goal_id"], instructions)
            self.assertNotIn(second["goal_id"], instructions)

    def test_auto_write_workers_default_to_fresh_threads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            repo = _repo(base, "auto-write-fresh")
            (repo / "README.md").write_text("# Fresh\n", encoding="utf-8")
            git(repo, "add", "README.md")
            git(repo, "commit", "-m", "initial")
            team.init_project(repo)
            git(repo, "add", ".codex", "AGENTS.md")
            git(repo, "commit", "-m", "install team policy")
            runtime = StubAppServerRuntime()
            manager = ClusterManager(
                settings,
                team=team,
                runtime=runtime,
                runtime_mode="app_server",
            )
            goal = manager.create_goal(repo, objective="Fresh writes")
            tasks = [
                team.create_task(
                    repo,
                    title=f"Write {index}",
                    description="Auto write must not reuse.",
                    acceptance="A distinct thread is used.",
                    kind="write",
                    owner_role="team_worker",
                    goal_id=goal["goal_id"],
                )
                for index in (1, 2)
            ]
            first = manager._dispatch_worker(
                manager.get_goal(goal["goal_id"]),
                team.get_task(tasks[0]["task_id"]),
            )
            manager.app_turns[first["job_id"]]["status"] = "completed"
            with team.connection() as connection:
                connection.execute(
                    """
                    UPDATE tasks
                    SET status = 'review', updated_at = ?, version = version + 1
                    WHERE task_id = ?
                    """,
                    (utc_now(), tasks[0]["task_id"]),
                )
            manager._reconcile_jobs()
            second = manager._dispatch_worker(
                manager.get_goal(goal["goal_id"]),
                team.get_task(tasks[1]["task_id"]),
            )
            self.assertNotEqual(first["thread_id"], second["thread_id"])
            self.assertEqual(len(runtime.start_threads), 2)
            self.assertEqual(runtime.resume_threads, [])

    def test_app_server_timeout_and_cancel_interrupt_active_turns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            repo = _repo(base, "interrupts")
            runtime = StubAppServerRuntime()
            manager = ClusterManager(
                settings,
                team=team,
                runtime=runtime,
                runtime_mode="app_server",
            )
            timeout_goal = manager.create_goal(
                repo,
                objective="Timeout active turn",
            )
            timeout_task = team.create_task(
                repo,
                title="Timeout turn",
                description="Timeout must interrupt.",
                acceptance="turn/interrupt is called.",
                kind="read",
                owner_role="team_explorer",
                goal_id=timeout_goal["goal_id"],
            )
            manager._dispatch_worker(
                manager.get_goal(timeout_goal["goal_id"]),
                team.get_task(timeout_task["task_id"]),
            )
            manager.worker_timeout_seconds = -1
            manager._reconcile_jobs()
            self.assertEqual(len(runtime.interrupts), 1)

            manager.set_goal_status(
                timeout_goal["goal_id"],
                "failed",
                reason="timeout test complete",
            )
            cancel_goal = manager.create_goal(
                repo,
                objective="Cancel active turn",
            )
            cancel_task = team.create_task(
                repo,
                title="Cancel turn",
                description="Cancellation must interrupt.",
                acceptance="turn/interrupt is called and task is blocked.",
                kind="read",
                owner_role="team_explorer",
                goal_id=cancel_goal["goal_id"],
            )
            manager._dispatch_worker(
                manager.get_goal(cancel_goal["goal_id"]),
                team.get_task(cancel_task["task_id"]),
            )
            team.claim_task(cancel_task["task_id"], actor="cancel-stub")
            manager.cancel_goal(cancel_goal["goal_id"], reason="test cancel")
            self.assertEqual(len(runtime.interrupts), 2)
            self.assertEqual(
                team.get_task(cancel_task["task_id"])["status"],
                "blocked",
            )
            canceled_job = manager.get_goal(cancel_goal["goal_id"])["jobs"][0]
            self.assertEqual(canceled_job["status"], "interrupted")

    def test_daemon_restart_marks_app_server_turn_lost_and_recovers_task(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            repo = _repo(base, "daemon-restart")
            first_runtime = StubAppServerRuntime()
            manager = ClusterManager(
                settings,
                team=team,
                runtime=first_runtime,
                runtime_mode="app_server",
            )
            goal = manager.create_goal(repo, objective="Recover after restart")
            task = team.create_task(
                repo,
                title="Recover active work",
                description="The daemon restarts while the turn is active.",
                acceptance="The turn is lost and the task returns to ready.",
                kind="read",
                owner_role="team_explorer",
                goal_id=goal["goal_id"],
            )
            dispatched = manager._dispatch_worker(
                manager.get_goal(goal["goal_id"]),
                team.get_task(task["task_id"]),
            )
            team.claim_task(task["task_id"], actor="restart-stub")

            restarted = ClusterManager(
                settings,
                team=team,
                runtime=StubAppServerRuntime(),
                runtime_mode="app_server",
                lead_cooldown_seconds=0,
            )
            restarted._reconcile_jobs()
            jobs = restarted.get_goal(goal["goal_id"])["jobs"]
            lost = next(job for job in jobs if job["job_id"] == dispatched["job_id"])
            self.assertEqual(lost["status"], "lost")
            self.assertEqual(team.get_task(task["task_id"])["status"], "ready")
            self.assertIn(
                "app_server_turn_lost",
                [
                    event["event_type"]
                    for event in restarted.get_goal(goal["goal_id"])["events"]
                ],
            )

    def test_tick_applies_agent_reset_request_and_unsubscribes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            repo = _repo(base, "agent-reset")
            project = team.init_project(repo)
            pool = AgentPool(settings)
            agent = pool.register_thread(
                project["project_id"],
                "team_explorer",
                "main",
                "reset-thread",
                "",
                "",
                "reset explorer",
            )
            team.reset_agent(
                agent["agent_id"],
                actor="human",
                reason="test reset",
            )
            runtime = StubAppServerRuntime()
            manager = ClusterManager(
                settings,
                team=team,
                runtime=runtime,
                runtime_mode="app_server",
            )
            manager.tick()
            self.assertEqual(runtime.unsubscribed, ["reset-thread"])
            reset_agent = pool.list_agents(project["project_id"])[0]
            self.assertEqual(reset_agent["status"], "superseded")
            self.assertIsNone(reset_agent["reset_requested_at"])

    def test_tick_synchronizes_project_archive_with_agents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            repo = _repo(base, "archive-agents")
            project = team.init_project(repo)
            pool = AgentPool(settings)
            agent = pool.register_thread(
                project["project_id"],
                "team_explorer",
                "main",
                "archive-agent-thread",
                "",
                "",
                "archive explorer",
            )
            manager = ClusterManager(settings, team=team)
            team.set_project_archived(project["project_id"], True)
            manager.tick()
            self.assertEqual(pool.list_agents(project["project_id"]), [])
            archived = pool.list_agents(
                project["project_id"],
                include_archived=True,
            )
            self.assertEqual(archived[0]["agent_id"], agent["agent_id"])
            self.assertIsNotNone(archived[0]["archived_at"])

            team.set_project_archived(project["project_id"], False)
            manager.tick()
            restored = pool.list_agents(project["project_id"])
            self.assertEqual(restored[0]["agent_id"], agent["agent_id"])
            self.assertIsNone(restored[0]["archived_at"])

    def test_app_server_fallback_retries_once_then_runs_exec(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            repo = _repo(base, "fallback")
            recovered = StubAppServerRuntime(fail_start=1)
            recovered_manager = ClusterManager(
                settings,
                team=team,
                runtime=recovered,
                runtime_mode="app_server",
            )
            recovered_goal = recovered_manager.create_goal(
                repo,
                objective="Recover App Server",
            )
            self.assertEqual(
                recovered_manager._select_job_runtime(
                    recovered_goal,
                    None,
                    "lead",
                ),
                "app_server",
            )
            self.assertEqual(recovered.start_calls, 1)
            self.assertEqual(recovered.restart_calls, 1)

            failed_runtime = StubAppServerRuntime(
                fail_start=1,
                fail_restart=1,
            )
            fallback_manager = ClusterManager(
                settings,
                team=team,
                runtime=failed_runtime,
                runtime_mode="app_server",
            )
            fallback_goal = fallback_manager.create_goal(
                _repo(base, "fallback-exec"),
                objective="Fallback to exec",
            )
            self.assertEqual(
                fallback_manager._select_job_runtime(
                    fallback_goal,
                    None,
                    "lead",
                ),
                "exec",
            )
            self.assertIn(
                "agent_runtime_fallback",
                [
                    event["event_type"]
                    for event in fallback_manager.get_goal(
                        fallback_goal["goal_id"]
                    )["events"]
                ],
            )

    def test_default_runtime_is_exec_and_workstream_contract_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            with patch.dict(os.environ, {"CODEX_TEAM_AGENT_RUNTIME": ""}):
                manager = ClusterManager(
                    settings,
                    team=TeamService(settings),
                )
            self.assertEqual(manager.agent_runtime, "exec")
            repo = _repo(base, "workstream")
            goal = manager.create_goal(
                repo,
                objective="Persist continuity",
                workstream_key="shared-feature",
                continuity_reason="same feature follow-up",
            )
            self.assertEqual(goal["workstream_key"], "shared-feature")
            self.assertEqual(
                goal["continuity_reason"],
                "same feature follow-up",
            )
            default_repo = _repo(base, "default-workstream")
            default_goal = manager.ensure_goal(
                default_repo,
                objective="Default workstream",
            )["goal"]
            self.assertEqual(
                default_goal["workstream_key"],
                default_goal["goal_id"],
            )
            enriched = manager.ensure_goal(
                default_repo,
                objective="Adopt a stable workstream",
                workstream_key="adopted-workstream",
                continuity_reason="same active scope",
            )["goal"]
            self.assertEqual(enriched["workstream_key"], "adopted-workstream")
            updated = manager.set_goal_workstream(
                goal["goal_id"],
                workstream_key="renamed-feature",
                continuity_reason="renamed scope remains related",
            )
            self.assertEqual(updated["workstream_key"], "renamed-feature")

    def test_cli_and_mcp_expose_continuity_and_agent_controls(self) -> None:
        parser = build_parser()
        task_args = parser.parse_args(
            [
                "task",
                "create",
                "--title",
                "Continuity",
                "--description",
                "Persist a non-default mode.",
                "--acceptance",
                "Mode is parsed.",
                "--kind",
                "write",
                "--owner-role",
                "team_worker",
                "--continuity-mode",
                "reuse",
            ]
        )
        self.assertEqual(task_args.continuity_mode, "reuse")
        ensure_args = parser.parse_args(
            [
                "cluster",
                "ensure",
                "--objective",
                "Resume related work",
                "--workstream-key",
                "feature-cache",
                "--continuity-reason",
                "same feature",
                "--no-daemon",
            ]
        )
        self.assertEqual(ensure_args.workstream_key, "feature-cache")
        self.assertEqual(ensure_args.continuity_reason, "same feature")
        workstream_args = parser.parse_args(
            [
                "cluster",
                "set-workstream",
                "G-EXAMPLE",
                "--workstream-key",
                "renamed",
                "--continuity-reason",
                "related",
            ]
        )
        self.assertEqual(workstream_args.workstream_key, "renamed")
        agent_args = parser.parse_args(["agent", "reset", "A-EXAMPLE"])
        self.assertEqual(agent_args.agent_command, "reset")

        tool_names = {item["name"] for item in _tool_definitions()}
        self.assertTrue(
            {
                "team_agent_list",
                "team_agent_reset",
                "team_goal_set_workstream",
            }.issubset(tool_names)
        )

    def test_mcp_agent_and_workstream_tools_round_trip_database_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            repo = _repo(base, "mcp-control")
            project = team.init_project(repo)
            manager = ClusterManager(settings, team=team)
            goal = manager.create_goal(
                repo,
                objective="MCP workstream control",
            )
            pool = AgentPool(settings)
            agent = pool.register_thread(
                project["project_id"],
                "team_explorer",
                "main",
                "mcp-agent-thread",
                "",
                "",
                "MCP explorer",
            )
            server = TeamMcpServer(settings)
            updated = server.call_tool(
                "team_goal_set_workstream",
                {
                    "goal_id": goal["goal_id"],
                    "workstream_key": "mcp-workstream",
                    "continuity_reason": "related MCP work",
                },
            )
            self.assertEqual(updated["workstream_key"], "mcp-workstream")
            listed = server.call_tool(
                "team_agent_list",
                {"project_id": project["project_id"]},
            )
            self.assertEqual(listed["agents"][0]["agent_id"], agent["agent_id"])
            reset = server.call_tool(
                "team_agent_reset",
                {
                    "agent_id": agent["agent_id"],
                    "actor": "human",
                    "reason": "test reset through MCP",
                },
            )
            self.assertIsNotNone(reset["reset_requested_at"])

    def test_agent_cache_benchmark_emits_json_markdown_and_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            fresh_path = base / "fresh.jsonl"
            reuse_path = base / "reuse.jsonl"
            fresh_path.write_text(
                json.dumps(
                    {
                        "first_turn_latency_ms": 1000,
                        "task_success": True,
                        "total_uncached_input_tokens": 1000,
                        "total_input_tokens": 1400,
                        "total_cached_input_tokens": 400,
                        "last_input_tokens": 500,
                        "last_cached_input_tokens": 100,
                        "last_uncached_input_tokens": 400,
                        "last_output_tokens": 20,
                        "last_context_tokens": 700,
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            reuse_path.write_text(
                json.dumps(
                    {
                        "first_turn_latency_ms": 900,
                        "task_success": True,
                        "total_uncached_input_tokens": 800,
                        "total_input_tokens": 1400,
                        "total_cached_input_tokens": 600,
                        "last_input_tokens": 500,
                        "last_cached_input_tokens": 250,
                        "last_uncached_input_tokens": 250,
                        "last_output_tokens": 20,
                        "last_context_tokens": 700,
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            json_output = base / "report.json"
            markdown_output = base / "report.md"
            result = subprocess.run(
                [
                    sys.executable,
                    str(
                        Path(__file__).resolve().parents[1]
                        / "scripts"
                        / "agent-cache-benchmark.py"
                    ),
                    "--fresh",
                    str(fresh_path),
                    "--reuse",
                    str(reuse_path),
                    "--json",
                    str(json_output),
                    "--markdown",
                    str(markdown_output),
                ],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(json_output.read_text(encoding="utf-8"))
            self.assertTrue(report["comparison"]["meets_gate"])
            self.assertAlmostEqual(
                report["comparison"]["uncached_input_change"],
                -0.2,
            )
            markdown = markdown_output.read_text(encoding="utf-8")
            self.assertIn("uncached input falls by at least 10%", markdown)
            self.assertIn("Result: PASS", markdown)


if __name__ == "__main__":
    unittest.main()
