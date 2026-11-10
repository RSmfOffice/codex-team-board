from __future__ import annotations

import io
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from codex_team import __version__
from codex_team.cluster import (
    _ClusterAppServerEvents,
    ClusterManager,
    LEAD_DEVELOPER_INSTRUCTIONS,
    TICK_PHASE_ORDER,
    _agent_cache_profile,
    _redact_sensitive_file,
    _redact_sensitive_log,
    _stable_developer_instructions,
)
from codex_team.agent_pool import CURRENT_AGENT_PROFILE, AgentPool
from codex_team.app_server import AppServerEvents, AppServerRuntime
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
from codex_team.service import TeamError, TeamService


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


def _manager_profile(
    manager: ClusterManager,
    goal: Mapping[str, object],
    role: str,
    instructions: str,
    *,
    cwd: Path,
) -> str:
    _, profile = _agent_cache_profile(
        goal,
        role,
        instructions,
        cwd=cwd,
        sandbox_mode="danger-full-access",
        runtime_version=__version__,
        runtime_identity=(
            f"{manager.agent_runtime}:" + " ".join(manager.agent_command)
        ),
    )
    return profile


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


def _deliver_token_usage(
    events: AppServerEvents,
    thread_id: str,
    turn_id: str,
    token_usage: Mapping[str, object],
) -> None:
    """Route a usage notification through the production mapping callback."""

    runtime = AppServerRuntime(command=["unused"], event_handler=events)
    event_queue: queue.Queue = queue.Queue()
    runtime._event_queue = event_queue
    runtime._dispatch_notification(
        "thread/tokenUsage/updated",
        {
            "threadId": thread_id,
            "turnId": turn_id,
            "tokenUsage": token_usage,
        },
    )
    event = event_queue.get_nowait()
    runtime._invoke_event_handler(
        event.callback_name,
        event.params,
        **event.kwargs,
    )


class StubAppServerRuntime:
    def __init__(
        self,
        *,
        fail_start: int = 0,
        fail_restart: int = 0,
        fail_resume: int = 0,
        fail_start_thread: int = 0,
        fail_turn: int = 0,
        disconnect_turn_attempt: int | None = None,
        restart_start_thread_attempt: int | None = None,
        restart_resume_attempt: int | None = None,
        restart_turn_attempt: int | None = None,
        stderr_lines: tuple[str, ...] = (),
    ) -> None:
        self.fail_start = fail_start
        self.fail_restart = fail_restart
        self.fail_resume = fail_resume
        self.fail_start_thread = fail_start_thread
        self.fail_turn = fail_turn
        self.disconnect_turn_attempt = disconnect_turn_attempt
        self.restart_start_thread_attempt = restart_start_thread_attempt
        self.restart_resume_attempt = restart_resume_attempt
        self.restart_turn_attempt = restart_turn_attempt
        self.stderr_lines = stderr_lines
        self.start_calls = 0
        self.restart_calls = 0
        self.stop_calls = 0
        self.resume_attempts = 0
        self.start_thread_attempts = 0
        self.turn_attempts = 0
        self.start_threads: list[tuple[str, str | None]] = []
        self.resume_threads: list[str] = []
        self.turns: list[dict[str, object]] = []
        self.interrupts: list[tuple[str, str]] = []
        self.unsubscribed: list[str] = []
        self.event_handler: object | None = None
        self._thread_sequence = 0
        self._turn_sequence = 0
        self._healthy = False
        self._generation = 0
        self._loaded_threads: set[str] = set()
        self._lock = threading.RLock()

    def start(self) -> dict:
        with self._lock:
            self.start_calls += 1
            if self.start_calls <= self.fail_start:
                raise RuntimeError("startup failed")
            if not self._healthy:
                self._generation += 1
                self._loaded_threads.clear()
            self._healthy = True
            return self.health()

    def restart(self) -> dict:
        with self._lock:
            self.restart_calls += 1
            if self.restart_calls <= self.fail_restart:
                raise RuntimeError("restart failed")
            self._generation += 1
            self._healthy = True
            self._loaded_threads.clear()
            return self.health()

    def stop(self) -> None:
        with self._lock:
            self.stop_calls += 1
            self._loaded_threads.clear()

    def health(self) -> dict:
        with self._lock:
            return {
                "healthy": self._healthy,
                "running": self._healthy,
                "generation": self._generation,
            }

    def list_loaded_threads(self) -> list[str]:
        with self._lock:
            return sorted(self._loaded_threads)

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def start_thread(
        self,
        cwd: Path,
        developer_instructions: str | None = None,
        model: str | None = None,
        model_provider: str | None = None,
        service_name: str | None = None,
    ) -> str:
        with self._lock:
            self.start_thread_attempts += 1
            if self.start_thread_attempts <= self.fail_start_thread:
                raise RuntimeError("thread/start failed")
            if (
                self.start_thread_attempts
                == self.restart_start_thread_attempt
            ):
                self._generation += 1
                self._healthy = True
                self._loaded_threads.clear()
                raise RuntimeError(
                    "thread/start failed after an automatic runtime restart"
                )
            self._thread_sequence += 1
            thread_id = f"stub-thread-{self._thread_sequence}"
            self.start_threads.append((str(cwd), developer_instructions))
            self._loaded_threads.add(thread_id)
            return thread_id

    def resume_thread(self, thread_id: str, exclude_turns: bool = True) -> str:
        with self._lock:
            self.resume_attempts += 1
            if self.resume_attempts <= self.fail_resume:
                raise RuntimeError("modelProviderMismatch during thread/resume")
            if self.resume_attempts == self.restart_resume_attempt:
                self._generation += 1
                self._healthy = True
                self._loaded_threads.clear()
                raise RuntimeError(
                    "thread/resume failed after an automatic runtime restart"
                )
            self.resume_threads.append(thread_id)
            self._loaded_threads.add(thread_id)
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
            self.turn_attempts += 1
            if self.turn_attempts <= self.fail_turn:
                raise RuntimeError("turn/start failed")
            if self.turn_attempts == self.disconnect_turn_attempt:
                self._healthy = False
                raise RuntimeError("turn/start disconnected")
            if self.turn_attempts == self.restart_turn_attempt:
                self._generation += 1
                self._healthy = True
                self._loaded_threads.clear()
                raise RuntimeError(
                    "turn/start failed after an automatic runtime restart"
                )
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
            self._loaded_threads.add(thread_id)
            return {"turn_id": turn_id, "status": "inProgress"}

    def interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        self.interrupts.append((thread_id, turn_id))

    def unsubscribe_thread(self, thread_id: str) -> str:
        self.unsubscribed.append(thread_id)
        return "unsubscribed"


class FakeExecProcess:
    pid = 424243

    def __init__(self) -> None:
        self.stdin = io.StringIO()
        self.stdout = io.StringIO()

    def poll(self) -> None:
        return None

    def __enter__(self) -> FakeExecProcess:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None


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
        def dispatch(goal: dict, task: dict, **kwargs) -> dict:
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

    def test_tick_phases_have_fixed_order_and_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = _settings(Path(temporary))
            manager = ClusterManager(
                settings,
                team=TeamService(settings),
                runtime_mode="exec",
                agent_command=["codex"],
            )
            with (
                patch.object(
                    manager,
                    "_reconcile_jobs",
                    return_value={"count": 1},
                ) as settle,
                patch.object(
                    manager.worktrees,
                    "recover_pending",
                    return_value=[],
                ) as recover,
                patch.object(
                    manager,
                    "_apply_agent_reset_requests",
                    return_value={"count": 0},
                ) as reset,
                patch.object(
                    manager,
                    "_sync_project_agent_archive",
                    return_value={"count": 0},
                ) as archive,
                patch.object(
                    manager,
                    "list_goals",
                    return_value=[],
                ) as prepare,
                patch.object(
                    manager,
                    "_schedule_goals",
                    return_value=[],
                ) as dispatch,
                patch.object(
                    manager,
                    "_check_worker_health",
                    return_value={"count": 0},
                ) as worker_health,
                patch.object(
                    manager,
                    "_check_goal_stalls",
                    return_value={"count": 0},
                ) as goal_health,
            ):
                result = manager.tick()

            self.assertTrue(result["ok"])
            self.assertEqual(result["phase_order"], list(TICK_PHASE_ORDER))
            self.assertEqual(
                [phase["name"] for phase in result["phases"]],
                list(TICK_PHASE_ORDER),
            )
            self.assertEqual(result["failed_phase"], None)
            self.assertEqual(result["skipped_phases"], [])
            self.assertTrue(
                all(
                    phase["ok"]
                    and isinstance(phase["duration_ms"], float)
                    and isinstance(phase["counts"], dict)
                    for phase in result["phases"]
                )
            )
            for mocked in (
                settle,
                recover,
                reset,
                archive,
                prepare,
                dispatch,
                worker_health,
                goal_health,
            ):
                self.assertTrue(mocked.called)

    def test_tick_phase_failure_stops_and_publishes_redacted_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = _settings(Path(temporary))
            manager = ClusterManager(
                settings,
                team=TeamService(settings),
                runtime_mode="exec",
                agent_command=["codex"],
            )
            with (
                patch.object(
                    manager,
                    "_reconcile_jobs",
                    side_effect=RuntimeError(
                        "claim_token=top-secret-value"
                    ),
                ),
                patch.object(
                    manager.worktrees,
                    "recover_pending",
                    side_effect=AssertionError("recover should be skipped"),
                ),
                patch.object(
                    manager,
                    "_schedule_goals",
                    side_effect=AssertionError("dispatch should be skipped"),
                ),
            ):
                result = manager.tick()

        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_phase"], "settle")
        self.assertEqual(
            [phase["name"] for phase in result["phases"]],
            ["settle", "publish"],
        )
        self.assertEqual(
            result["skipped_phases"],
            [
                "recover",
                "control",
                "prepare",
                "dispatch",
                "health",
            ],
        )
        self.assertNotIn("top-secret-value", result["error"])
        self.assertIn("[REDACTED]", result["error"])

    def test_concurrent_tick_calls_are_serialized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = _settings(Path(temporary))
            manager = ClusterManager(
                settings,
                team=TeamService(settings),
                runtime_mode="exec",
                agent_command=["codex"],
            )
            state_lock = threading.Lock()
            active = 0
            max_active = 0

            def reconcile() -> dict[str, int]:
                nonlocal active, max_active
                with state_lock:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.15)
                with state_lock:
                    active -= 1
                return {"count": 0}

            with (
                patch.object(
                    manager,
                    "_reconcile_jobs",
                    side_effect=reconcile,
                ),
                patch.object(manager, "list_goals", return_value=[]),
            ):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    results = list(
                        executor.map(lambda _: manager.tick(), range(2))
                    )

        self.assertEqual(len(results), 2)
        self.assertTrue(all(result["ok"] for result in results))
        self.assertEqual(max_active, 1)

    def test_tick_reconciles_planning_goal_through_active_to_completed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            repo = _repo(base, "terminal-reconciliation")
            manager = ClusterManager(
                settings,
                team=team,
                runtime_mode="exec",
                agent_command=["codex"],
            )
            goal = manager.create_goal(
                repo,
                objective="Complete a terminal task",
            )
            task = team.create_task(
                repo,
                title="Already complete",
                description="Exercise terminal reconciliation.",
                acceptance="Goal activates then completes.",
                kind="read",
                owner_role="team_explorer",
                goal_id=goal["goal_id"],
            )
            with team.connection() as connection:
                connection.execute(
                    """
                    UPDATE tasks
                    SET status = 'done', completed_at = ?, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (utc_now(), utc_now(), task["task_id"]),
                )

            result = manager.tick()
            current = manager.get_goal(goal["goal_id"])

        self.assertTrue(result["ok"])
        self.assertEqual(current["status"], "completed")
        status_events = [
            event["data"]["status"]
            for event in current["events"]
            if event["event_type"] == "goal_status"
        ]
        self.assertEqual(status_events[-2:], ["active", "completed"])

    def test_empty_goal_waits_for_lead_instead_of_completing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            repo = _repo(base, "empty-goal")
            manager = ClusterManager(
                settings,
                team=team,
                runtime_mode="exec",
                agent_command=["codex"],
            )
            goal = manager.create_goal(
                repo,
                objective="Plan an empty goal",
            )
            with patch.object(
                manager,
                "_dispatch_lead",
                return_value={
                    "job_id": "J-EMPTY-GOAL",
                    "goal_id": goal["goal_id"],
                    "status": "running",
                },
            ):
                result = manager.tick()
            current = manager.get_goal(goal["goal_id"])

        self.assertTrue(result["ok"])
        self.assertEqual(current["status"], "planning")
        self.assertGreaterEqual(len(result["scheduled"]), 1)

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

    def test_dispatch_reserves_claim_before_worker_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            repo = _repo(base, "claim-reservation")
            runtime = StubAppServerRuntime()
            manager = ClusterManager(
                settings,
                team=team,
                runtime=runtime,
                runtime_mode="app_server",
            )
            goal, tasks = self._goal_with_ready_tasks(
                manager,
                team,
                repo,
                1,
            )

            job = manager._dispatch_worker(
                manager.get_goal(goal["goal_id"]),
                tasks[0],
                reserve_claim=True,
            )
            expected_actor = manager._worker_actor(
                tasks[0]["owner_role"],
                tasks[0]["task_id"],
                job["job_id"],
            )
            reserved = team.get_task(tasks[0]["task_id"])
            self.assertEqual(reserved["status"], "running")
            self.assertEqual(reserved["claimed_by"], expected_actor)

            with self.assertRaisesRegex(TeamError, "already running"):
                team.claim_task(tasks[0]["task_id"], actor="external-worker")

            renewed = team.claim_task(
                tasks[0]["task_id"],
                actor=expected_actor,
            )
            self.assertEqual(renewed["claimed_by"], expected_actor)
            self.assertEqual(
                len(
                    [
                        item
                        for item in manager.get_goal(goal["goal_id"])["jobs"]
                        if item["task_id"] == tasks[0]["task_id"]
                    ]
                ),
                1,
            )

    def test_dispatch_claim_conflict_does_not_start_or_replace_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            repo = _repo(base, "claim-conflict-dispatch")
            runtime = StubAppServerRuntime()
            manager = ClusterManager(
                settings,
                team=team,
                runtime=runtime,
                runtime_mode="app_server",
            )
            goal, tasks = self._goal_with_ready_tasks(
                manager,
                team,
                repo,
                1,
            )
            external = team.claim_task(
                tasks[0]["task_id"],
                actor="external-worker",
            )

            job, changed, keep_goal = manager._dispatch_next_job(
                manager.get_goal(goal["goal_id"]),
                [tasks[0]],
            )

            self.assertIsNone(job)
            self.assertTrue(changed)
            self.assertTrue(keep_goal)
            current = team.get_task(tasks[0]["task_id"])
            self.assertEqual(current["claimed_by"], "external-worker")
            self.assertEqual(current["lease_until"], external["lease_until"])
            self.assertEqual(manager.get_goal(goal["goal_id"])["jobs"], [])
            events = manager.get_goal(goal["goal_id"])["events"]
            skipped = [
                event
                for event in events
                if event["event_type"] == "dispatch_skipped"
            ]
            self.assertEqual(len(skipped), 1)
            self.assertEqual(skipped[0]["data"]["reason"], "claim_conflict")

    def test_worker_failure_cannot_recover_another_actors_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            repo = _repo(base, "claim-conflict-recovery")
            runtime = StubAppServerRuntime()
            manager = ClusterManager(
                settings,
                team=team,
                runtime=runtime,
                runtime_mode="app_server",
            )
            goal, tasks = self._goal_with_ready_tasks(
                manager,
                team,
                repo,
                1,
            )
            job = manager._dispatch_worker(
                manager.get_goal(goal["goal_id"]),
                tasks[0],
            )
            external = team.claim_task(
                tasks[0]["task_id"],
                actor="external-worker",
            )
            job_row = next(
                item
                for item in manager.get_goal(goal["goal_id"])["jobs"]
                if item["job_id"] == job["job_id"]
            )

            manager._handle_worker_failure(
                job_row,
                "worker exited before claiming",
            )

            current = team.get_task(tasks[0]["task_id"])
            self.assertEqual(current["status"], "running")
            self.assertEqual(current["claimed_by"], "external-worker")
            self.assertEqual(current["lease_until"], external["lease_until"])
            updated_job = next(
                item
                for item in manager.get_goal(goal["goal_id"])["jobs"]
                if item["job_id"] == job["job_id"]
            )
            self.assertEqual(updated_job["status"], "lost")
            event_types = {
                event["event_type"]
                for event in manager.get_goal(goal["goal_id"])["events"]
            }
            self.assertIn("worker_claim_conflict", event_types)
            self.assertNotIn("worker_recovered", event_types)

    def test_canceled_dependency_blocks_task_and_escalates_to_lead(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            repo = _repo(base, "canceled-dependency")
            runtime = StubAppServerRuntime()
            manager = ClusterManager(
                settings,
                team=team,
                runtime=runtime,
                runtime_mode="app_server",
                lead_cooldown_seconds=0,
            )
            goal = manager.create_goal(
                repo,
                objective="Recover from a canceled dependency",
                autonomy="auto",
            )
            dependency = team.create_task(
                repo,
                title="Canceled dependency",
                description="Will be canceled before dispatch.",
                acceptance="Dependency is no longer needed.",
                kind="read",
                owner_role="team_explorer",
                goal_id=goal["goal_id"],
            )
            dependent = team.create_task(
                repo,
                title="Dependent task",
                description="Must be escalated instead of waiting forever.",
                acceptance="Task becomes blocked and Lead is dispatched.",
                kind="read",
                owner_role="team_explorer",
                goal_id=goal["goal_id"],
                depends_on=[dependency["task_id"]],
            )
            team.cancel_task(
                dependency["task_id"],
                actor="lead",
                reason="dependency replaced",
            )

            manager.tick()

            blocked = team.get_task(dependent["task_id"])
            self.assertEqual(blocked["status"], "blocked")
            self.assertIn("dependency unavailable", blocked["blocker"])
            result = manager.get_goal(goal["goal_id"])
            lead_jobs = [job for job in result["jobs"] if job["kind"] == "lead"]
            self.assertEqual(len(lead_jobs), 1)
            event_types = {
                event["event_type"]
                for event in result["events"]
            }
            self.assertIn("dependency_invalidated", event_types)

    def test_goal_stall_watchdog_pauses_cycle_and_resume_resets_counter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            repo = _repo(base, "goal-stall-cycle")
            runtime = StubAppServerRuntime()
            manager = ClusterManager(
                settings,
                team=team,
                runtime=runtime,
                runtime_mode="app_server",
                lead_cooldown_seconds=0,
            )
            manager.goal_stall_ticks = 2
            goal = manager.create_goal(
                repo,
                objective="Detect a dependency cycle stall",
                autonomy="auto",
            )
            manager.set_goal_status(goal["goal_id"], "active")
            first = team.create_task(
                repo,
                title="Cycle A",
                description="Part of a manually inserted cycle.",
                acceptance="Cycle is detected.",
                kind="read",
                owner_role="team_explorer",
                goal_id=goal["goal_id"],
            )
            second = team.create_task(
                repo,
                title="Cycle B",
                description="Part of a manually inserted cycle.",
                acceptance="Cycle is detected.",
                kind="read",
                owner_role="team_explorer",
                goal_id=goal["goal_id"],
            )
            with team.connection() as connection:
                connection.executemany(
                    """
                    INSERT INTO task_dependencies(task_id, depends_on)
                    VALUES (?, ?)
                    """,
                    (
                        (first["task_id"], second["task_id"]),
                        (second["task_id"], first["task_id"]),
                    ),
                )

            manager.tick()
            still_active = manager.get_goal(goal["goal_id"])
            self.assertEqual(still_active["status"], "active")
            self.assertEqual(still_active["stall_ticks"], 1)
            manager.tick()

            stalled = manager.get_goal(goal["goal_id"])
            self.assertEqual(stalled["status"], "paused")
            self.assertEqual(stalled["stall_ticks"], 2)
            self.assertIsNotNone(stalled["stalled_at"])
            self.assertIn("goal stalled", stalled["last_error"])
            event_types = {
                event["event_type"]
                for event in stalled["events"]
            }
            self.assertIn("goal_stalled", event_types)
            snapshot = team.dashboard_snapshot(
                goal["project_id"],
                include_archived=True,
            )
            card = next(
                item
                for item in snapshot["cluster_goals"]
                if item["goal_id"] == goal["goal_id"]
            )
            self.assertEqual(card["health"], "stalled")
            self.assertEqual(card["next_action"], "inspect_stalled_goal")

            resumed = manager.set_goal_status(
                goal["goal_id"],
                "active",
                reason="manual retry after inspecting stall",
            )
            self.assertEqual(resumed["stall_ticks"], 0)
            self.assertEqual(resumed["stall_signature"], "")
            self.assertIsNone(resumed["stalled_at"])

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

    def test_list_goals_without_status_filter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            manager = ClusterManager(
                settings,
                team=team,
                runtime_mode="exec",
            )
            repo = _repo(base, "list-goals")
            created = manager.create_goal(
                repo,
                objective="Exercise the unfiltered cluster list query",
            )
            goals = manager.list_goals()
            self.assertEqual(
                [created["goal_id"]],
                [goal["goal_id"] for goal in goals],
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
                    "PYTHONIOENCODING": "utf-8",
                    "PYTHONUTF8": "1",
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
            self.assertEqual(first_job["context_tokens"], 150)
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
            self.assertIn(goal["project_id"], instructions)
            self.assertIn("feature-cache", instructions)
            self.assertIn("team_worker", instructions)
            self.assertNotIn(goal["goal_id"], instructions)
            self.assertNotIn(tasks[0]["task_id"], instructions)
            self.assertNotIn(str(Path(settings.logs_dir)), instructions)
            self.assertNotRegex(instructions, r"\d{4}-\d{2}-\d{2}T\d{2}:")
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

    def test_app_server_reused_job_output_usage_is_cumulative_delta(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            repo = _repo(base, "app-server-output-usage")
            project = team.init_project(repo)
            runtime = StubAppServerRuntime()
            manager = ClusterManager(
                settings,
                team=team,
                runtime=runtime,
                runtime_mode="app_server",
            )
            goal = manager.create_goal(
                repo,
                objective="Measure cumulative App Server usage",
                workstream_key="usage-reuse",
                continuity_reason="two related read turns",
            )
            tasks = [
                team.create_task(
                    repo,
                    title=f"Usage turn {index}",
                    description="Exercise cumulative thread token usage.",
                    acceptance="Only the current job's cumulative tokens are recorded.",
                    kind="read",
                    owner_role="team_explorer",
                    goal_id=goal["goal_id"],
                    continuity_mode="reuse",
                )
                for index in (1, 2)
            ]

            first = manager._dispatch_worker(
                manager.get_goal(goal["goal_id"]),
                team.get_task(tasks[0]["task_id"]),
            )
            manager.app_turns[first["job_id"]].update(
                {
                    "status": "completed",
                    "usage": {
                        "total": {
                            "input": 100,
                            "cached": 50,
                            "output": 60,
                            "context": 70,
                        },
                        "last": {
                            "input": 20,
                            "cached": 10,
                            "output": 12,
                            "context": 34,
                        },
                        "modelContextWindow": 200,
                    },
                    "first_usage": {
                        "total": {
                            "input": 25,
                            "cached": 12,
                            "output": 15,
                            "context": 40,
                        },
                        "last": {
                            "input": 20,
                            "cached": 10,
                            "output": 12,
                            "context": 34,
                        },
                        "modelContextWindow": 200,
                    },
                }
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
            self.assertEqual(first_job["output_tokens"], 60)

            second = manager._dispatch_worker(
                manager.get_goal(goal["goal_id"]),
                team.get_task(tasks[1]["task_id"]),
            )
            self.assertEqual(runtime.resume_threads, [first["thread_id"]])
            manager.app_turns[second["job_id"]].update(
                {
                    "status": "completed",
                    "usage": {
                        "total": {
                            "input": 180,
                            "cached": 80,
                            "output": 90,
                            "context": 70,
                        },
                        "last": {
                            "input": 40,
                            "cached": 8,
                            "output": 20,
                            "context": 35,
                        },
                        "modelContextWindow": 200,
                    },
                    "first_usage": {
                        "total": {
                            "input": 150,
                            "cached": 65,
                            "output": 75,
                            "context": 66,
                        },
                        "last": {
                            "input": 30,
                            "cached": 12,
                            "output": 18,
                            "context": 33,
                        },
                        "modelContextWindow": 200,
                    },
                }
            )
            with team.connection() as connection:
                connection.execute(
                    """
                    UPDATE tasks
                    SET status = 'review', updated_at = ?, version = version + 1
                    WHERE task_id = ?
                    """,
                    (utc_now(), tasks[1]["task_id"]),
                )
            manager._reconcile_jobs()
            second_job = next(
                item
                for item in manager.get_goal(goal["goal_id"])["jobs"]
                if item["job_id"] == second["job_id"]
            )
            self.assertEqual(second_job["input_tokens"], 80)
            self.assertEqual(second_job["cached_input_tokens"], 30)
            self.assertEqual(second_job["uncached_input_tokens"], 50)
            self.assertEqual(second_job["output_tokens"], 30)
            self.assertEqual(second_job["first_input_tokens"], 50)
            self.assertEqual(second_job["first_cached_input_tokens"], 15)
            self.assertEqual(second_job["first_uncached_input_tokens"], 35)
            agent = manager.list_agents(project["project_id"])[0]
            self.assertEqual(agent["total_input_tokens"], 180)
            self.assertEqual(agent["total_cached_input_tokens"], 80)
            nested_fallback = manager._app_job_usage(
                {
                    "job_id": "J-NEXT",
                    "agent_id": agent["agent_id"],
                },
                {
                    "usage": {
                        "total": {
                            "input": 260,
                            "cached": 115,
                            "output": 120,
                            "context": 70,
                        },
                        "last": None,
                        "modelContextWindow": 200,
                    }
                },
            )
            self.assertEqual(nested_fallback["input_tokens"], 80)
            self.assertEqual(nested_fallback["cached_input_tokens"], 35)
            self.assertEqual(nested_fallback["uncached_input_tokens"], 45)
            self.assertEqual(nested_fallback["output_tokens"], 30)
            last_fallback = manager._app_job_usage(
                {
                    "job_id": "J-LAST",
                    "agent_id": agent["agent_id"],
                },
                {
                    "usage": {
                        "total": {},
                        "last": {
                            "input": 45,
                            "cached": 5,
                            "output": 7,
                            "context": 52,
                        },
                        "modelContextWindow": 200,
                    }
                },
            )
            self.assertEqual(last_fallback["input_tokens"], 45)
            self.assertEqual(last_fallback["cached_input_tokens"], 5)
            self.assertEqual(last_fallback["uncached_input_tokens"], 40)
            self.assertEqual(last_fallback["output_tokens"], 7)
            clamped = manager._app_job_usage(
                dict(second_job),
                {
                    "usage": {
                        "input": 180,
                        "cached": 80,
                        "output": 40,
                    }
                },
            )
            self.assertEqual(clamped["output_tokens"], 0)

    def test_app_server_job_usage_uses_final_total_across_token_events(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            repo = _repo(base, "multi-usage-events")
            team.init_project(repo)
            runtime = StubAppServerRuntime()
            manager = ClusterManager(
                settings,
                team=team,
                runtime=runtime,
                runtime_mode="app_server",
            )
            events = _ClusterAppServerEvents(manager)
            goal = manager.create_goal(
                repo,
                objective="Accumulate token usage across tool calls",
            )
            task = team.create_task(
                repo,
                title="Multiple usage events",
                description="Emit cumulative usage after each model call.",
                acceptance="The job records the final cumulative total.",
                kind="read",
                owner_role="team_explorer",
                goal_id=goal["goal_id"],
            )
            job = manager._dispatch_worker(
                manager.get_goal(goal["goal_id"]),
                team.get_task(task["task_id"]),
            )
            events.on_token_usage_updated(
                job["thread_id"],
                job["turn_id"],
                {
                    "total": {
                        "input": 120,
                        "cached": 20,
                        "output": 30,
                        "context": 150,
                    },
                    "last": {
                        "input": 50,
                        "cached": 5,
                        "output": 10,
                        "context": 60,
                    },
                    "modelContextWindow": 200,
                },
            )
            events.on_token_usage_updated(
                job["thread_id"],
                job["turn_id"],
                {
                    "total": {
                        "input": 150,
                        "cached": 25,
                        "output": 34,
                        "context": 184,
                    },
                    "last": {
                        "input": 60,
                        "cached": 6,
                        "output": 12,
                        "context": 72,
                    },
                    "modelContextWindow": 200,
                },
            )
            with manager._app_state_lock:
                manager.app_turns[job["job_id"]]["status"] = "completed"
            with team.connection() as connection:
                connection.execute(
                    """
                    UPDATE tasks
                    SET status = 'review', updated_at = ?, version = version + 1
                    WHERE task_id = ?
                    """,
                    (utc_now(), task["task_id"]),
                )
            manager._reconcile_jobs()

            with team.connection(write=False) as connection:
                persisted = connection.execute(
                    """
                    SELECT input_tokens, cached_input_tokens,
                           uncached_input_tokens, output_tokens,
                           first_input_tokens, first_cached_input_tokens,
                           first_uncached_input_tokens, context_tokens,
                           model_context_window, telemetry_complete
                    FROM cluster_jobs
                    WHERE job_id = ?
                    """,
                    (job["job_id"],),
                ).fetchone()
            self.assertEqual(persisted["input_tokens"], 150)
            self.assertEqual(persisted["cached_input_tokens"], 25)
            self.assertEqual(persisted["uncached_input_tokens"], 125)
            self.assertEqual(persisted["output_tokens"], 34)
            self.assertEqual(persisted["first_input_tokens"], 120)
            self.assertEqual(persisted["first_cached_input_tokens"], 20)
            self.assertEqual(persisted["first_uncached_input_tokens"], 100)
            self.assertEqual(persisted["context_tokens"], 184)
            self.assertEqual(persisted["model_context_window"], 200)
            self.assertEqual(persisted["telemetry_complete"], 1)

    def test_app_server_terminal_jobs_without_usage_evidence_are_unknown(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            repo = _repo(base, "missing-terminal-usage")
            project = team.init_project(repo)
            runtime = StubAppServerRuntime()
            manager = ClusterManager(
                settings,
                team=team,
                runtime=runtime,
                runtime_mode="app_server",
            )
            goal = manager.create_goal(
                repo,
                objective="Terminal jobs without usage evidence",
            )
            usage_fields = (
                "input_tokens",
                "cached_input_tokens",
                "uncached_input_tokens",
                "cache_write_input_tokens",
                "output_tokens",
                "reasoning_output_tokens",
                "first_input_tokens",
                "first_cached_input_tokens",
                "first_uncached_input_tokens",
                "context_tokens",
            )

            def dispatch(label: str) -> tuple[dict, dict]:
                task = team.create_task(
                    repo,
                    title=f"{label} without usage",
                    description="No App Server token usage event is emitted.",
                    acceptance="Unknown usage remains unknown.",
                    kind="read",
                    owner_role="team_explorer",
                    goal_id=goal["goal_id"],
                )
                job = manager._dispatch_worker(
                    manager.get_goal(goal["goal_id"]),
                    team.get_task(task["task_id"]),
                )
                return job, task

            def assert_unknown(job: dict, expected_status: str) -> None:
                with team.connection(write=False) as connection:
                    persisted = connection.execute(
                        """
                        SELECT status, usage_source, telemetry_complete,
                               model_context_window,
                               input_tokens, cached_input_tokens,
                               uncached_input_tokens,
                               cache_write_input_tokens, output_tokens,
                               reasoning_output_tokens, first_input_tokens,
                               first_cached_input_tokens,
                               first_uncached_input_tokens, context_tokens
                        FROM cluster_jobs
                        WHERE job_id = ?
                        """,
                        (job["job_id"],),
                    ).fetchone()
                self.assertEqual(persisted["status"], expected_status)
                self.assertIsNone(persisted["usage_source"])
                self.assertEqual(persisted["telemetry_complete"], 0)
                self.assertIsNone(persisted["model_context_window"])
                for field in usage_fields:
                    self.assertEqual(persisted[field], 0, field)

                dashboard = team.dashboard_snapshot(project["project_id"])
                dashboard_job = next(
                    item
                    for item in dashboard["cluster_jobs"]
                    if item["job_id"] == job["job_id"]
                )
                self.assertIsNone(dashboard_job["usage_source"])
                self.assertEqual(dashboard_job["telemetry_complete"], 0)
                self.assertIsNone(dashboard_job["model_context_window"])
                self.assertIsNone(dashboard_job["cache_hit_rate"])
                for field in usage_fields:
                    self.assertIsNone(dashboard_job[field], field)

            with self.subTest(terminal="completed"):
                completed_job, completed_task = dispatch("Completed")
                with manager._app_state_lock:
                    manager.app_turns[completed_job["job_id"]][
                        "status"
                    ] = "completed"
                with team.connection() as connection:
                    connection.execute(
                        """
                        UPDATE tasks
                        SET status = 'review', updated_at = ?,
                            version = version + 1
                        WHERE task_id = ?
                        """,
                        (utc_now(), completed_task["task_id"]),
                    )
                manager._reconcile_jobs()
                assert_unknown(completed_job, "completed")

            with self.subTest(terminal="failed"):
                failed_job, _ = dispatch("Failed")
                with manager._app_state_lock:
                    manager.app_turns[failed_job["job_id"]].update(
                        {
                            "status": "failed",
                            "error": {"message": "turn failed without usage"},
                        }
                    )
                manager._reconcile_jobs()
                assert_unknown(failed_job, "failed")

            with self.subTest(terminal="timeout"):
                timeout_job, _ = dispatch("Timed out")
                manager.worker_timeout_seconds = -1
                manager._reconcile_jobs()
                assert_unknown(timeout_job, "failed")

    def test_app_server_usage_preserves_field_level_unknowns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            repo = _repo(base, "field-usage")
            project = team.init_project(repo)
            manager = ClusterManager(
                settings,
                team=team,
                runtime=StubAppServerRuntime(),
                runtime_mode="app_server",
            )
            events = _ClusterAppServerEvents(manager)
            goal = manager.create_goal(
                repo,
                objective="Preserve field-level App Server usage",
            )

            def complete_with_usage(label: str, usage: dict) -> tuple[dict, dict]:
                task = team.create_task(
                    repo,
                    title=f"{label} usage",
                    description="Exercise field-level usage presence.",
                    acceptance="Persist observed fields and mark absent ones unknown.",
                    kind="read",
                    owner_role="team_explorer",
                    goal_id=goal["goal_id"],
                    continuity_mode="fresh",
                )
                job = manager._dispatch_worker(
                    manager.get_goal(goal["goal_id"]),
                    team.get_task(task["task_id"]),
                )
                _deliver_token_usage(
                    events,
                    job["thread_id"],
                    job["turn_id"],
                    usage,
                )
                events.on_turn_completed(
                    job["thread_id"],
                    job["turn_id"],
                    {"id": job["turn_id"], "status": "completed"},
                )
                with team.connection() as connection:
                    connection.execute(
                        """
                        UPDATE tasks
                        SET status = 'review', updated_at = ?,
                            version = version + 1
                        WHERE task_id = ?
                        """,
                        (utc_now(), task["task_id"]),
                    )
                manager._reconcile_jobs()
                with team.connection(write=False) as connection:
                    row = dict(
                        connection.execute(
                            """
                            SELECT input_tokens, cached_input_tokens,
                                   uncached_input_tokens, output_tokens,
                                   context_tokens, model_context_window,
                                   usage_source, telemetry_complete,
                                   usage_missing_fields
                            FROM cluster_jobs
                            WHERE job_id = ?
                            """,
                            (job["job_id"],),
                        ).fetchone()
                    )
                dashboard_job = next(
                    item
                    for item in team.dashboard_snapshot(
                        project["project_id"]
                    )["cluster_jobs"]
                    if item["job_id"] == job["job_id"]
                )
                return row, dashboard_job

            partial, partial_dashboard = complete_with_usage(
                "Partial",
                {
                    "total": {
                        "inputTokens": 120,
                        "totalTokens": 140,
                    },
                    "last": {
                        "inputTokens": 20,
                        "totalTokens": 40,
                    },
                    "modelContextWindow": 200,
                },
            )
            self.assertEqual(partial["input_tokens"], 120)
            self.assertIsNotNone(partial["usage_source"])
            self.assertEqual(partial["telemetry_complete"], 0)
            self.assertEqual(partial["model_context_window"], 200)
            self.assertTrue(
                {
                    "cached_input_tokens",
                    "uncached_input_tokens",
                    "output_tokens",
                }.issubset(json.loads(partial["usage_missing_fields"]))
            )
            self.assertEqual(partial_dashboard["input_tokens"], 120)
            self.assertEqual(partial_dashboard["context_tokens"], 140)
            self.assertIsNone(partial_dashboard["cached_input_tokens"])
            self.assertIsNone(partial_dashboard["uncached_input_tokens"])
            self.assertIsNone(partial_dashboard["output_tokens"])
            self.assertIsNone(partial_dashboard["cache_hit_rate"])

            metadata, metadata_dashboard = complete_with_usage(
                "Metadata only",
                {
                    "total": {},
                    "last": {},
                    "modelContextWindow": 200,
                },
            )
            self.assertIsNone(metadata["usage_source"])
            self.assertEqual(metadata["telemetry_complete"], 0)
            self.assertEqual(metadata["model_context_window"], 200)
            self.assertTrue(
                {
                    "input_tokens",
                    "cached_input_tokens",
                    "output_tokens",
                }.issubset(json.loads(metadata["usage_missing_fields"]))
            )
            self.assertIsNone(metadata_dashboard["input_tokens"])
            self.assertIsNone(metadata_dashboard["cached_input_tokens"])
            self.assertIsNone(metadata_dashboard["output_tokens"])
            self.assertEqual(metadata_dashboard["model_context_window"], 200)

            last_empty, last_empty_dashboard = complete_with_usage(
                "Last empty",
                {
                    "total": {
                        "inputTokens": 9,
                        "cachedInputTokens": 2,
                        "outputTokens": 4,
                        "totalTokens": 13,
                    },
                    "last": {},
                    "modelContextWindow": 200,
                },
            )
            self.assertEqual(last_empty["input_tokens"], 9)
            self.assertEqual(last_empty["cached_input_tokens"], 2)
            self.assertEqual(last_empty["uncached_input_tokens"], 7)
            self.assertEqual(last_empty["output_tokens"], 4)
            self.assertEqual(last_empty["telemetry_complete"], 1)
            self.assertEqual(last_empty_dashboard["input_tokens"], 9)
            self.assertEqual(last_empty_dashboard["context_tokens"], 13)

            cached_uncached, cached_uncached_dashboard = complete_with_usage(
                "Cached uncached only",
                {
                    "total": {
                        "cachedInputTokens": 30,
                        "uncachedInputTokens": 50,
                        "outputTokens": 10,
                        "totalTokens": 80,
                    },
                    "last": {},
                },
            )
            self.assertEqual(cached_uncached["telemetry_complete"], 0)
            self.assertIn(
                "input_tokens",
                json.loads(cached_uncached["usage_missing_fields"]),
            )
            self.assertIsNone(cached_uncached_dashboard["input_tokens"])
            self.assertEqual(
                cached_uncached_dashboard["cached_input_tokens"],
                30,
            )
            self.assertEqual(
                cached_uncached_dashboard["uncached_input_tokens"],
                50,
            )
            self.assertEqual(cached_uncached_dashboard["output_tokens"], 10)

            unknown, unknown_dashboard = complete_with_usage(
                "Unknown",
                {},
            )
            self.assertIsNone(unknown["usage_source"])
            self.assertEqual(unknown["telemetry_complete"], 0)
            self.assertIsNone(unknown_dashboard["input_tokens"])
            self.assertIsNone(unknown_dashboard["cached_input_tokens"])
            self.assertIsNone(unknown_dashboard["output_tokens"])

            explicit_zero, zero_dashboard = complete_with_usage(
                "Explicit zero",
                {
                    "total": {
                        "inputTokens": 0,
                        "cachedInputTokens": 0,
                        "outputTokens": 0,
                        "totalTokens": 0,
                    },
                    "last": {},
                },
            )
            self.assertIsNotNone(explicit_zero["usage_source"])
            self.assertEqual(explicit_zero["telemetry_complete"], 1)
            self.assertEqual(zero_dashboard["input_tokens"], 0)
            self.assertEqual(zero_dashboard["cached_input_tokens"], 0)
            self.assertEqual(zero_dashboard["uncached_input_tokens"], 0)
            self.assertEqual(zero_dashboard["output_tokens"], 0)

    def test_partial_app_usage_quarantines_thread_before_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            repo = _repo(base, "partial-baseline-reuse")
            project = team.init_project(repo)
            runtime = StubAppServerRuntime()
            manager = ClusterManager(
                settings,
                team=team,
                runtime=runtime,
                runtime_mode="app_server",
            )
            events = _ClusterAppServerEvents(manager)
            goal = manager.create_goal(
                repo,
                objective="Quarantine partial usage baselines",
                workstream_key="partial-baseline",
                continuity_reason="two related turns",
            )
            tasks = [
                team.create_task(
                    repo,
                    title=f"Baseline turn {index}",
                    description="Exercise usage presence before thread reuse.",
                    acceptance="Incomplete baselines cannot be reused.",
                    kind="read",
                    owner_role="team_explorer",
                    goal_id=goal["goal_id"],
                    continuity_mode="reuse",
                )
                for index in (1, 2)
            ]

            def finish(
                job: dict,
                task: dict,
                usage: Mapping[str, object],
            ) -> None:
                _deliver_token_usage(
                    events,
                    job["thread_id"],
                    job["turn_id"],
                    usage,
                )
                events.on_turn_completed(
                    job["thread_id"],
                    job["turn_id"],
                    {"id": job["turn_id"], "status": "completed"},
                )
                with team.connection() as connection:
                    connection.execute(
                        """
                        UPDATE tasks
                        SET status = 'review', updated_at = ?,
                            version = version + 1
                        WHERE task_id = ?
                        """,
                        (utc_now(), task["task_id"]),
                    )
                manager._reconcile_jobs()

            first = manager._dispatch_worker(
                manager.get_goal(goal["goal_id"]),
                team.get_task(tasks[0]["task_id"]),
            )
            finish(
                first,
                tasks[0],
                {
                    "total": {
                        "cachedInputTokens": 11,
                        "uncachedInputTokens": 13,
                        "outputTokens": 7,
                        "totalTokens": 24,
                    },
                    "last": {},
                },
            )
            with team.connection(write=False) as connection:
                first_job = dict(
                    connection.execute(
                        """
                        SELECT agent_id, usage_missing_fields,
                               telemetry_complete
                        FROM cluster_jobs
                        WHERE job_id = ?
                        """,
                        (first["job_id"],),
                    ).fetchone()
                )
                first_agent = dict(
                    connection.execute(
                        """
                        SELECT status, reset_requested_at
                        FROM cluster_agents
                        WHERE agent_id = ?
                        """,
                        (first_job["agent_id"],),
                    ).fetchone()
                )
            self.assertEqual(first_job["telemetry_complete"], 0)
            self.assertIn(
                "input_tokens",
                json.loads(first_job["usage_missing_fields"]),
            )
            self.assertEqual(first_agent["status"], "failed")
            self.assertIsNotNone(first_agent["reset_requested_at"])

            second = manager._dispatch_worker(
                manager.get_goal(goal["goal_id"]),
                team.get_task(tasks[1]["task_id"]),
            )
            self.assertNotEqual(second["thread_id"], first["thread_id"])
            self.assertEqual(runtime.resume_threads, [])
            finish(
                second,
                tasks[1],
                {
                    "total": {
                        "inputTokens": 40,
                        "cachedInputTokens": 10,
                        "outputTokens": 6,
                        "totalTokens": 46,
                    },
                    "last": {},
                },
            )
            with team.connection(write=False) as connection:
                second_job = dict(
                    connection.execute(
                        """
                        SELECT agent_id, input_tokens,
                               cached_input_tokens, output_tokens,
                               telemetry_complete
                        FROM cluster_jobs
                        WHERE job_id = ?
                        """,
                        (second["job_id"],),
                    ).fetchone()
                )
                second_agent = dict(
                    connection.execute(
                        """
                        SELECT status, reset_requested_at,
                               total_input_tokens
                        FROM cluster_agents
                        WHERE agent_id = ?
                        """,
                        (second_job["agent_id"],),
                    ).fetchone()
                )
            self.assertEqual(second_job["input_tokens"], 40)
            self.assertEqual(second_job["cached_input_tokens"], 10)
            self.assertEqual(second_job["output_tokens"], 6)
            self.assertEqual(second_job["telemetry_complete"], 1)
            self.assertEqual(second_agent["status"], "stored")
            self.assertIsNone(second_agent["reset_requested_at"])
            self.assertEqual(second_agent["total_input_tokens"], 40)

    @unittest.skipUnless(
        os.environ.get("CODEX_TEAM_REAL_APP_SERVER_TEST") == "1",
        "set CODEX_TEAM_REAL_APP_SERVER_TEST=1 to run the installed App Server",
    )
    def test_installed_codex_app_server_thread_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            completed = threading.Event()
            source_codex_home = Path(
                os.environ.get("CODEX_HOME", Path.home() / ".codex")
            ).expanduser()
            isolated_codex_home = workspace / "codex-home"
            isolated_codex_home.mkdir()
            for filename in ("config.toml", "auth.json", "models.json"):
                source = source_codex_home / filename
                if source.is_file():
                    shutil.copy2(source, isolated_codex_home / filename)

            class LifecycleEvents(AppServerEvents):
                def __init__(self) -> None:
                    self.turn_statuses: list[str] = []
                    self.token_usage: list[dict] = []

                def on_turn_completed(
                    self,
                    thread_id: str,
                    turn_id: str,
                    turn: dict,
                ) -> None:
                    self.turn_statuses.append(str(turn.get("status")))
                    completed.set()

                def on_token_usage_updated(
                    self,
                    thread_id: str,
                    turn_id: str,
                    usage: dict,
                ) -> None:
                    self.token_usage.append(dict(usage))

            events = LifecycleEvents()
            runtime = AppServerRuntime(
                request_timeout=20.0,
                startup_timeout=20.0,
                stop_timeout=5.0,
                event_handler=events,
                env={"CODEX_HOME": str(isolated_codex_home)},
            )
            try:
                started = runtime.start()
                self.assertTrue(started["healthy"])
                thread_id = runtime.start_thread(
                    workspace,
                    developer_instructions="Stable integration test instructions.",
                    service_name="codex-team-integration-test",
                )
                self.assertIn(thread_id, runtime.list_loaded_threads())
                runtime.start_turn(
                    thread_id,
                    "Reply with exactly OK. Do not inspect files.",
                    cwd=workspace,
                    sandbox_mode="read-only",
                )
                _wait_for(completed.is_set, timeout=90.0)
                self.assertIn("completed", events.turn_statuses)
                self.assertTrue(events.token_usage)
                self.assertTrue(
                    all(
                        {"total", "last", "modelContextWindow"} <= set(usage)
                        for usage in events.token_usage
                    )
                )
                self.assertEqual(
                    runtime.unsubscribe_thread(thread_id),
                    "unsubscribed",
                )
                resumed = runtime.resume_thread(thread_id)
                self.assertEqual(resumed, thread_id)
                self.assertIn(thread_id, runtime.list_loaded_threads())
                self.assertEqual(
                    runtime.unsubscribe_thread(thread_id),
                    "unsubscribed",
                )
            finally:
                runtime.stop()

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
            self.assertEqual(
                [event["data"]["retry_allowed"] for event in resets],
                [True, False],
            )
            self.assertEqual(
                {
                    event["data"]["retry_scope"]
                    for event in resets
                },
                {f"worker:{task['task_id']}"},
            )
            self.assertIn(
                "agent_context_retry_exhausted",
                [event["event_type"] for event in goal_result["events"]],
            )
            self.assertEqual(len(runtime.start_threads), 2)

    def test_lead_legacy_thread_cannot_bypass_agent_pool_compatibility(self) -> None:
        cases = (
            ("model", {"model": "other-model"}),
            ("provider", {"model_provider": "other-provider"}),
            ("workstream", {"workstream_key": "other-workstream"}),
            ("status", {"status": "failed"}),
            ("reset", {"reset_requested": True}),
        )
        with patch.dict(
            os.environ,
            {
                "CODEX_TEAM_MODEL": "current-model",
                "CODEX_TEAM_MODEL_PROVIDER": "current-provider",
            },
        ):
            for case_name, options in cases:
                with self.subTest(case=case_name), tempfile.TemporaryDirectory() as temporary:
                    base = Path(temporary)
                    settings = _settings(base)
                    team = TeamService(settings)
                    repo = _repo(base, f"legacy-{case_name}")
                    project = team.init_project(repo)
                    runtime = StubAppServerRuntime()
                    manager = ClusterManager(
                        settings,
                        team=team,
                        runtime=runtime,
                        runtime_mode="app_server",
                    )
                    goal = manager.create_goal(
                        repo,
                        objective=f"Legacy compatibility {case_name}",
                        workstream_key="legacy-workstream",
                    )
                    pool = AgentPool(settings)
                    agent = pool.register_thread(
                        project["project_id"],
                        "lead",
                        str(options.get("workstream_key", "legacy-workstream")),
                        f"legacy-{case_name}-thread",
                        str(options.get("model", "current-model")),
                        str(options.get("model_provider", "current-provider")),
                        f"legacy {case_name} lead",
                    )
                    if options.get("status") == "failed":
                        pool.mark_failed(agent["agent_id"], "status mismatch")
                    if options.get("reset_requested"):
                        pool.request_reset(
                            agent["agent_id"],
                            "reset mismatch",
                            "test",
                        )
                    with team.connection() as connection:
                        connection.execute(
                            """
                            UPDATE cluster_goals
                            SET lead_thread_id = ?, updated_at = ?
                            WHERE goal_id = ?
                            """,
                            (agent["thread_id"], utc_now(), goal["goal_id"]),
                        )

                    dispatched = manager._dispatch_lead(
                        manager.get_goal(goal["goal_id"]),
                        [],
                    )

                    self.assertEqual(runtime.resume_threads, [])
                    self.assertEqual(dispatched["thread_id"], "stub-thread-1")
                    self.assertEqual(len(runtime.start_threads), 1)

    def test_legacy_idle_lead_is_replaced_before_current_profile_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            repo = _repo(base, "legacy-profile-lead")
            project = team.init_project(repo)
            runtime = StubAppServerRuntime()
            manager = ClusterManager(
                settings,
                team=team,
                runtime=runtime,
                runtime_mode="app_server",
                lead_cooldown_seconds=0,
            )
            goal = manager.create_goal(
                repo,
                objective="Replace legacy profile",
                workstream_key="profile-workstream",
            )
            pool = AgentPool(settings)
            current_profile = _manager_profile(
                manager,
                goal,
                "lead",
                LEAD_DEVELOPER_INSTRUCTIONS,
                cwd=repo,
            )
            legacy = pool.register_thread(
                project["project_id"],
                "lead",
                "profile-workstream",
                "legacy-profile-thread",
                manager.model,
                manager.model_provider,
                "legacy profile lead",
                profile=current_profile,
            )
            with team.connection() as connection:
                connection.execute(
                    """
                    UPDATE cluster_agents SET profile = 'legacy'
                    WHERE agent_id = ?
                    """,
                    (legacy["agent_id"],),
                )
                connection.execute(
                    """
                    UPDATE cluster_goals
                    SET lead_thread_id = ?, updated_at = ?
                    WHERE goal_id = ?
                    """,
                    (legacy["thread_id"], utc_now(), goal["goal_id"]),
                )

            first = manager._dispatch_lead(
                manager.get_goal(goal["goal_id"]),
                [],
            )
            self.assertEqual(runtime.resume_threads, [])
            self.assertNotEqual(first["thread_id"], legacy["thread_id"])
            _deliver_token_usage(
                _ClusterAppServerEvents(manager),
                first["thread_id"],
                first["turn_id"],
                {
                    "total": {
                        "inputTokens": 10,
                        "cachedInputTokens": 2,
                        "outputTokens": 3,
                        "totalTokens": 13,
                    },
                    "last": {},
                    "modelContextWindow": 100,
                },
            )
            manager.app_turns[first["job_id"]]["status"] = "completed"
            manager._reconcile_jobs()

            agents = pool.list_agents(project["project_id"])
            old_agent = next(
                agent for agent in agents if agent["agent_id"] == legacy["agent_id"]
            )
            current_agent = next(
                agent for agent in agents if agent["thread_id"] == first["thread_id"]
            )
            self.assertEqual(old_agent["status"], "idle")
            self.assertEqual(old_agent["profile"], "legacy")
            self.assertEqual(current_agent["profile"], current_profile)

            second = manager._dispatch_lead(
                manager.get_goal(goal["goal_id"]),
                [],
            )
            self.assertEqual(second["thread_id"], first["thread_id"])
            self.assertEqual(len(runtime.start_threads), 1)
            self.assertEqual(runtime.resume_threads, [first["thread_id"]])

    def test_selected_lead_resume_failure_marks_old_agent_and_starts_fresh_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            repo = _repo(base, "resume-fresh")
            project = team.init_project(repo)
            runtime = StubAppServerRuntime(fail_resume=1)
            manager = ClusterManager(
                settings,
                team=team,
                runtime=runtime,
                runtime_mode="app_server",
            )
            goal = manager.create_goal(
                repo,
                objective="Resume then replace",
                workstream_key="resume-workstream",
            )
            pool = AgentPool(settings)
            current_profile = _manager_profile(
                manager,
                goal,
                "lead",
                LEAD_DEVELOPER_INSTRUCTIONS,
                cwd=repo,
            )
            old_agent = pool.register_thread(
                project["project_id"],
                "lead",
                "resume-workstream",
                "stale-thread",
                manager.model,
                manager.model_provider,
                "stale lead",
                profile=current_profile,
            )
            with team.connection() as connection:
                connection.execute(
                    """
                    UPDATE cluster_goals
                    SET lead_thread_id = ?, updated_at = ?
                    WHERE goal_id = ?
                    """,
                    (old_agent["thread_id"], utc_now(), goal["goal_id"]),
                )

            dispatched = manager._dispatch_lead(
                manager.get_goal(goal["goal_id"]),
                [],
            )

            self.assertEqual(runtime.resume_attempts, 1)
            self.assertEqual(len(runtime.start_threads), 1)
            self.assertNotEqual(dispatched["thread_id"], old_agent["thread_id"])
            failed = next(
                agent
                for agent in pool.list_agents(project["project_id"])
                if agent["agent_id"] == old_agent["agent_id"]
            )
            self.assertEqual(failed["status"], "failed")
            self.assertIsNotNone(failed["reset_requested_at"])
            self.assertIn(old_agent["thread_id"], runtime.unsubscribed)
            job = next(
                item
                for item in manager.get_goal(goal["goal_id"])["jobs"]
                if item["job_id"] == dispatched["job_id"]
            )
            self.assertEqual(job["thread_id"], dispatched["thread_id"])
            log = Path(job["log_path"]).read_text(encoding="utf-8")
            self.assertIn("agent_thread_resume_failed", log)

    def test_non_core_mcp_surface_forces_fresh_agent_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ,
            {"CODEX_TEAM_MCP_SURFACE": "legacy"},
            clear=False,
        ):
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            repo = _repo(base, "legacy-surface-fresh")
            project = team.init_project(repo)
            runtime = StubAppServerRuntime()
            manager = ClusterManager(
                settings,
                team=team,
                runtime=runtime,
                runtime_mode="app_server",
            )
            goal = manager.create_goal(
                repo,
                objective="Compatibility surface must not reuse",
                workstream_key="legacy-surface",
            )
            profile = _manager_profile(
                manager,
                goal,
                "lead",
                LEAD_DEVELOPER_INSTRUCTIONS,
                cwd=repo,
            )
            pool = AgentPool(settings)
            old_agent = pool.register_thread(
                project["project_id"],
                "lead",
                "legacy-surface",
                "old-legacy-surface",
                manager.model,
                manager.model_provider,
                "legacy surface lead",
                profile=profile,
            )
            with team.connection() as connection:
                connection.execute(
                    """
                    UPDATE cluster_goals
                    SET lead_thread_id = ?, updated_at = ?
                    WHERE goal_id = ?
                    """,
                    (old_agent["thread_id"], utc_now(), goal["goal_id"]),
                )

            dispatched = manager._dispatch_lead(
                manager.get_goal(goal["goal_id"]),
                [],
            )
            self.assertEqual([], runtime.resume_threads)
            self.assertNotEqual(dispatched["thread_id"], old_agent["thread_id"])

    def test_lead_context_overflow_retries_once_then_fails_goal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            repo = _repo(base, "lead-context-overflow")
            runtime = StubAppServerRuntime()
            manager = ClusterManager(
                settings,
                team=team,
                runtime=runtime,
                runtime_mode="app_server",
                lead_cooldown_seconds=0,
            )
            goal = manager.create_goal(repo, objective="Lead overflow")

            first = manager._dispatch_lead(manager.get_goal(goal["goal_id"]), [])
            manager.app_turns[first["job_id"]]["context_window_exceeded"] = True
            manager.app_turns[first["job_id"]]["status"] = "failed"
            manager.app_turns[first["job_id"]]["error"] = {
                "codexErrorInfo": "contextWindowExceeded",
                "message": "contextWindowExceeded",
            }
            manager._reconcile_jobs()
            first_goal = manager.get_goal(goal["goal_id"])
            self.assertEqual(first_goal["status"], "active")
            self.assertIsNone(first_goal["lead_thread_id"])

            second = manager._dispatch_lead(first_goal, [])
            manager.app_turns[second["job_id"]]["context_window_exceeded"] = True
            manager.app_turns[second["job_id"]]["status"] = "failed"
            manager.app_turns[second["job_id"]]["error"] = {
                "codexErrorInfo": "contextWindowExceeded",
                "message": "contextWindowExceeded",
            }
            manager._reconcile_jobs()
            manager.tick()

            result = manager.get_goal(goal["goal_id"])
            self.assertEqual(result["status"], "failed")
            self.assertIn("one automatic fresh-thread retry", result["last_error"])
            self.assertEqual(len(runtime.start_threads), 2)
            reset_events = [
                event
                for event in result["events"]
                if event["event_type"] == "agent_context_reset"
            ]
            self.assertEqual(len(reset_events), 2)
            self.assertEqual(reset_events[0]["data"]["retry_scope"], f"lead:{goal['goal_id']}")
            self.assertEqual(
                [event["data"]["retry_allowed"] for event in reset_events],
                [True, False],
            )
            self.assertIn(
                "agent_context_retry_exhausted",
                [event["event_type"] for event in result["events"]],
            )

    def test_app_server_start_failure_falls_back_to_exec_in_same_job(self) -> None:
        class FakeProcess:
            pid = 424242

            def __init__(self) -> None:
                self.stdin = io.StringIO()
                self.stdout = io.StringIO()

            def poll(self) -> None:
                return None

        for case_name, runtime in (
            ("thread", StubAppServerRuntime(fail_start_thread=2)),
            ("turn", StubAppServerRuntime(fail_turn=2)),
        ):
            with self.subTest(case=case_name), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                settings = _settings(base)
                team = TeamService(settings)
                repo = _repo(base, f"fallback-{case_name}")
                project = team.init_project(repo)
                manager = ClusterManager(
                    settings,
                    team=team,
                    runtime=runtime,
                    runtime_mode="app_server",
                )
                goal = manager.create_goal(
                    repo,
                    objective=f"Fallback {case_name}",
                )
                task = team.create_task(
                    repo,
                    title=f"Fallback {case_name}",
                    description="App Server startup failure must fall back.",
                    acceptance="The same job runs with exec.",
                    kind="read",
                    owner_role="team_explorer",
                    goal_id=goal["goal_id"],
                )
                process = FakeProcess()
                current_goal = manager.get_goal(goal["goal_id"])
                current_task = team.get_task(task["task_id"])
                real_popen = subprocess.Popen

                def popen(command, *args, **kwargs):
                    if command and command[0] == "git":
                        return real_popen(command, *args, **kwargs)
                    return process

                with patch(
                    "codex_team.cluster.subprocess.Popen",
                    side_effect=popen,
                ):
                    dispatched = manager._dispatch_worker(
                        current_goal,
                        current_task,
                    )

                job = next(
                    item
                    for item in manager.get_goal(goal["goal_id"])["jobs"]
                    if item["job_id"] == dispatched["job_id"]
                )
                self.assertEqual(dispatched["runtime"], "exec")
                self.assertEqual(job["runtime"], "exec")
                self.assertEqual(job["status"], "running")
                self.assertEqual(job["pid"], FakeProcess.pid)
                self.assertIsNone(job["thread_id"])
                self.assertIsNone(job["agent_id"])
                self.assertEqual(job["thread_action"], "fallback_exec")
                self.assertEqual(team.get_task(task["task_id"])["status"], "ready")
                fallback_events = [
                    event["data"]
                    for event in manager.get_goal(goal["goal_id"])["events"]
                    if event["event_type"] == "agent_runtime_fallback"
                    and event["data"].get("job_id") == dispatched["job_id"]
                ]
                self.assertEqual(len(fallback_events), 1)
                self.assertEqual(fallback_events[0]["scope"], "same_job")
                self.assertEqual(fallback_events[0]["fallback_runtime"], "exec")
                log = Path(job["log_path"]).read_text(encoding="utf-8")
                self.assertIn("agent_runtime_fallback", log)
                self.assertIn("# command=", log)
                if case_name == "thread":
                    self.assertEqual(runtime.start_thread_attempts, 1)
                    self.assertEqual(runtime.restart_calls, 0)
                else:
                    self.assertEqual(runtime.turn_attempts, 1)
                    self.assertEqual(runtime.restart_calls, 0)
                    failed = AgentPool(settings).list_agents(project["project_id"])[0]
                    self.assertEqual(failed["status"], "failed")
                    self.assertIsNotNone(failed["reset_requested_at"])

    def test_healthy_app_server_failure_only_falls_back_current_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            repo = _repo(base, "healthy-concurrent")
            runtime = StubAppServerRuntime(
                stderr_lines=("diagnostic marker",),
            )
            manager = ClusterManager(
                settings,
                team=team,
                runtime=runtime,
                runtime_mode="app_server",
            )
            goal, tasks = self._goal_with_ready_tasks(
                manager,
                team,
                repo,
                2,
            )
            first = manager._dispatch_worker(
                manager.get_goal(goal["goal_id"]),
                team.get_task(tasks[0]["task_id"]),
            )
            team.claim_task(
                tasks[0]["task_id"],
                actor=manager._worker_actor(
                    tasks[0]["owner_role"],
                    tasks[0]["task_id"],
                    first["job_id"],
                ),
            )
            runtime.fail_turn = runtime.turn_attempts + 1
            current_goal = manager.get_goal(goal["goal_id"])
            current_task = team.get_task(tasks[1]["task_id"])
            fallback_process = FakeExecProcess()
            real_popen = subprocess.Popen

            def popen(command, *args, **kwargs):
                if command and command[0] == "git":
                    return real_popen(command, *args, **kwargs)
                return fallback_process

            with patch(
                "codex_team.cluster.subprocess.Popen",
                side_effect=popen,
            ):
                second = manager._dispatch_worker(
                    current_goal,
                    current_task,
                )

            jobs = {
                job["job_id"]: job
                for job in manager.get_goal(goal["goal_id"])["jobs"]
            }
            self.assertEqual(first["runtime"], "app_server")
            self.assertEqual(second["runtime"], "exec")
            self.assertEqual(jobs[first["job_id"]]["status"], "running")
            self.assertEqual(jobs[first["job_id"]]["runtime"], "app_server")
            self.assertEqual(jobs[first["job_id"]]["runtime_generation"], 1)
            self.assertEqual(jobs[second["job_id"]]["status"], "running")
            self.assertIsNone(jobs[second["job_id"]]["runtime_generation"])
            self.assertEqual(runtime.restart_calls, 0)
            self.assertIn(first["job_id"], manager.app_turns)
            self.assertEqual(
                manager.app_turns[first["job_id"]]["runtime_generation"],
                1,
            )
            self.assertIn(
                first["thread_id"],
                runtime.list_loaded_threads(),
            )
            self.assertNotIn(second["job_id"], manager.app_turns)
            self.assertIn(
                "process remained healthy",
                jobs[second["job_id"]]["error"],
            )
            self.assertIn(
                "diagnostic marker",
                jobs[second["job_id"]]["error"],
            )
            lost_events = [
                event
                for event in manager.get_goal(goal["goal_id"])["events"]
                if event["event_type"] == "app_server_turn_lost"
            ]
            self.assertEqual(lost_events, [])

    def test_disconnect_restart_loses_and_recovers_other_turns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            repo = _repo(base, "disconnect-concurrent")
            runtime = StubAppServerRuntime(disconnect_turn_attempt=3)
            manager = ClusterManager(
                settings,
                team=team,
                runtime=runtime,
                runtime_mode="app_server",
                lead_cooldown_seconds=0,
            )
            goal, tasks = self._goal_with_ready_tasks(
                manager,
                team,
                repo,
                2,
            )
            worker_other = manager._dispatch_worker(
                manager.get_goal(goal["goal_id"]),
                team.get_task(tasks[0]["task_id"]),
            )
            team.claim_task(
                tasks[0]["task_id"],
                actor=manager._worker_actor(
                    tasks[0]["owner_role"],
                    tasks[0]["task_id"],
                    worker_other["job_id"],
                ),
            )
            lead_other = manager._dispatch_lead(
                manager.get_goal(goal["goal_id"]),
                team.list_tasks(repo, goal_id=goal["goal_id"]),
            )

            current = manager._dispatch_worker(
                manager.get_goal(goal["goal_id"]),
                team.get_task(tasks[1]["task_id"]),
            )

            result = manager.get_goal(goal["goal_id"])
            jobs = {job["job_id"]: job for job in result["jobs"]}
            self.assertEqual(current["runtime"], "app_server")
            self.assertEqual(jobs[current["job_id"]]["status"], "running")
            self.assertEqual(jobs[worker_other["job_id"]]["status"], "lost")
            self.assertEqual(jobs[lead_other["job_id"]]["status"], "lost")
            self.assertEqual(
                team.get_task(tasks[0]["task_id"])["status"],
                "ready",
            )
            self.assertIsNone(result["lead_thread_id"])
            self.assertEqual(result["lead_failures"], 1)
            self.assertEqual(runtime.restart_calls, 1)
            self.assertEqual(runtime.turn_attempts, 4)
            self.assertEqual(runtime.resume_threads, [current["thread_id"]])
            self.assertEqual(jobs[worker_other["job_id"]]["runtime_generation"], 1)
            self.assertEqual(jobs[lead_other["job_id"]]["runtime_generation"], 1)
            self.assertEqual(jobs[current["job_id"]]["runtime_generation"], 2)
            self.assertNotIn(worker_other["job_id"], manager.app_turns)
            self.assertNotIn(lead_other["job_id"], manager.app_turns)
            self.assertIn(current["job_id"], manager.app_turns)
            self.assertEqual(
                manager.app_turns[current["job_id"]]["runtime_generation"],
                2,
            )
            lost_jobs = {
                event["data"]["job_id"]
                for event in result["events"]
                if event["event_type"] == "app_server_turn_lost"
            }
            self.assertEqual(
                lost_jobs,
                {worker_other["job_id"], lead_other["job_id"]},
            )
            recovered = [
                event["data"]
                for event in result["events"]
                if event["event_type"] == "worker_recovered"
            ]
            self.assertEqual(
                recovered[-1]["job_id"],
                worker_other["job_id"],
            )
            self.assertIn("unhealthy App Server", recovered[-1]["reason"])

    def test_internal_restart_during_turn_start_isolates_old_jobs_before_return(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            repo = _repo(base, "turn-start-generation-delta")
            runtime = StubAppServerRuntime()
            manager = ClusterManager(
                settings,
                team=team,
                runtime=runtime,
                runtime_mode="app_server",
            )
            goal, tasks = self._goal_with_ready_tasks(
                manager,
                team,
                repo,
                3,
            )
            old_worker = manager._dispatch_worker(
                manager.get_goal(goal["goal_id"]),
                team.get_task(tasks[0]["task_id"]),
            )
            team.claim_task(
                tasks[0]["task_id"],
                actor=manager._worker_actor(
                    tasks[0]["owner_role"],
                    tasks[0]["task_id"],
                    old_worker["job_id"],
                ),
            )
            old_explorer = manager._dispatch_worker(
                manager.get_goal(goal["goal_id"]),
                team.get_task(tasks[1]["task_id"]),
            )
            team.claim_task(
                tasks[1]["task_id"],
                actor=manager._worker_actor(
                    tasks[1]["owner_role"],
                    tasks[1]["task_id"],
                    old_explorer["job_id"],
                ),
            )
            runtime.restart_turn_attempt = runtime.turn_attempts + 1

            current = manager._dispatch_worker(
                manager.get_goal(goal["goal_id"]),
                team.get_task(tasks[2]["task_id"]),
            )

            result = manager.get_goal(goal["goal_id"])
            jobs = {job["job_id"]: job for job in result["jobs"]}
            self.assertEqual(current["runtime"], "app_server")
            self.assertEqual(runtime.restart_calls, 0)
            self.assertEqual(runtime.generation, 2)
            self.assertEqual(jobs[old_worker["job_id"]]["status"], "lost")
            self.assertEqual(jobs[old_explorer["job_id"]]["status"], "lost")
            self.assertEqual(jobs[current["job_id"]]["status"], "running")
            self.assertEqual(
                jobs[old_worker["job_id"]]["runtime_generation"],
                1,
            )
            self.assertEqual(
                jobs[old_explorer["job_id"]]["runtime_generation"],
                1,
            )
            self.assertEqual(
                jobs[current["job_id"]]["runtime_generation"],
                2,
            )
            self.assertEqual(
                manager.app_turns[current["job_id"]]["runtime_generation"],
                2,
            )
            self.assertEqual(
                {
                    job["job_id"]
                    for job in result["jobs"]
                    if job["status"] == "lost"
                },
                {old_worker["job_id"], old_explorer["job_id"]},
            )
            self.assertEqual(
                team.get_task(tasks[0]["task_id"])["status"],
                "ready",
            )
            self.assertEqual(
                team.get_task(tasks[1]["task_id"])["status"],
                "ready",
            )
            recovered = {
                event["data"]["job_id"]
                for event in result["events"]
                if event["event_type"] == "worker_recovered"
            }
            self.assertTrue(
                {old_worker["job_id"], old_explorer["job_id"]} <= recovered
            )
            lost_events = [
                event["data"]
                for event in result["events"]
                if event["event_type"] == "app_server_turn_lost"
            ]
            self.assertEqual(len(lost_events), 2)
            self.assertTrue(
                all(
                    event["runtime_generation"] == 1
                    and event["current_runtime_generation"] == 2
                    for event in lost_events
                )
            )
            fallback_events = [
                event
                for event in result["events"]
                if event["event_type"] == "agent_runtime_fallback"
                and event["data"].get("job_id") == current["job_id"]
            ]
            self.assertEqual(fallback_events, [])

    def test_internal_restart_during_fresh_thread_start_retries_once_on_new_generation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            repo = _repo(base, "thread-start-generation-delta")
            runtime = StubAppServerRuntime()
            manager = ClusterManager(
                settings,
                team=team,
                runtime=runtime,
                runtime_mode="app_server",
            )
            goal, tasks = self._goal_with_ready_tasks(
                manager,
                team,
                repo,
                3,
            )
            old_worker = manager._dispatch_worker(
                manager.get_goal(goal["goal_id"]),
                team.get_task(tasks[0]["task_id"]),
            )
            team.claim_task(
                tasks[0]["task_id"],
                actor=manager._worker_actor(
                    tasks[0]["owner_role"],
                    tasks[0]["task_id"],
                    old_worker["job_id"],
                ),
            )
            old_explorer = manager._dispatch_worker(
                manager.get_goal(goal["goal_id"]),
                team.get_task(tasks[1]["task_id"]),
            )
            team.claim_task(
                tasks[1]["task_id"],
                actor=manager._worker_actor(
                    tasks[1]["owner_role"],
                    tasks[1]["task_id"],
                    old_explorer["job_id"],
                ),
            )
            runtime.restart_start_thread_attempt = (
                runtime.start_thread_attempts + 1
            )

            current = manager._dispatch_worker(
                manager.get_goal(goal["goal_id"]),
                team.get_task(tasks[2]["task_id"]),
            )

            result = manager.get_goal(goal["goal_id"])
            jobs = {job["job_id"]: job for job in result["jobs"]}
            self.assertEqual(current["runtime"], "app_server")
            self.assertEqual(runtime.restart_calls, 0)
            self.assertEqual(runtime.generation, 2)
            self.assertEqual(runtime.start_thread_attempts, 4)
            self.assertEqual(jobs[old_worker["job_id"]]["status"], "lost")
            self.assertEqual(jobs[old_explorer["job_id"]]["status"], "lost")
            self.assertEqual(jobs[current["job_id"]]["status"], "running")
            self.assertEqual(
                jobs[current["job_id"]]["runtime_generation"],
                2,
            )
            lost_events = [
                event["data"]
                for event in result["events"]
                if event["event_type"] == "app_server_turn_lost"
            ]
            self.assertEqual(len(lost_events), 2)
            self.assertTrue(
                all(
                    event["runtime_generation"] == 1
                    and event["current_runtime_generation"] == 2
                    for event in lost_events
                )
            )

    def test_internal_restart_during_thread_resume_isolates_jobs_and_starts_fresh_once(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            repo = _repo(base, "resume-generation-delta")
            runtime = StubAppServerRuntime()
            manager = ClusterManager(
                settings,
                team=team,
                runtime=runtime,
                runtime_mode="app_server",
            )
            project = team.init_project(repo)
            goal, tasks = self._goal_with_ready_tasks(
                manager,
                team,
                repo,
                2,
            )
            old_worker = manager._dispatch_worker(
                manager.get_goal(goal["goal_id"]),
                team.get_task(tasks[0]["task_id"]),
            )
            team.claim_task(
                tasks[0]["task_id"],
                actor=manager._worker_actor(
                    tasks[0]["owner_role"],
                    tasks[0]["task_id"],
                    old_worker["job_id"],
                ),
            )
            old_explorer = manager._dispatch_worker(
                manager.get_goal(goal["goal_id"]),
                team.get_task(tasks[1]["task_id"]),
            )
            team.claim_task(
                tasks[1]["task_id"],
                actor=manager._worker_actor(
                    tasks[1]["owner_role"],
                    tasks[1]["task_id"],
                    old_explorer["job_id"],
                ),
            )
            pool = AgentPool(settings)
            current_profile = _manager_profile(
                manager,
                goal,
                "lead",
                LEAD_DEVELOPER_INSTRUCTIONS,
                cwd=repo,
            )
            old_agent = pool.register_thread(
                project["project_id"],
                "lead",
                str(goal["workstream_key"]),
                "stale-generation-thread",
                manager.model,
                manager.model_provider,
                "stale generation lead",
                profile=current_profile,
            )
            with team.connection() as connection:
                connection.execute(
                    """
                    UPDATE cluster_goals
                    SET lead_thread_id = ?, updated_at = ?
                    WHERE goal_id = ?
                    """,
                    (old_agent["thread_id"], utc_now(), goal["goal_id"]),
                )
            runtime.restart_resume_attempt = runtime.resume_attempts + 1

            current = manager._dispatch_lead(
                manager.get_goal(goal["goal_id"]),
                team.list_tasks(repo, goal_id=goal["goal_id"]),
            )

            result = manager.get_goal(goal["goal_id"])
            jobs = {job["job_id"]: job for job in result["jobs"]}
            self.assertEqual(current["runtime"], "app_server")
            self.assertEqual(runtime.restart_calls, 0)
            self.assertEqual(runtime.generation, 2)
            self.assertEqual(runtime.resume_attempts, 1)
            self.assertEqual(len(runtime.start_threads), 3)
            self.assertEqual(jobs[old_worker["job_id"]]["status"], "lost")
            self.assertEqual(jobs[old_explorer["job_id"]]["status"], "lost")
            self.assertEqual(jobs[current["job_id"]]["status"], "running")
            self.assertNotEqual(current["thread_id"], old_agent["thread_id"])
            self.assertEqual(
                jobs[current["job_id"]]["runtime_generation"],
                2,
            )
            failed = next(
                agent
                for agent in pool.list_agents(project["project_id"])
                if agent["agent_id"] == old_agent["agent_id"]
            )
            self.assertEqual(failed["status"], "failed")
            lost_events = [
                event["data"]
                for event in result["events"]
                if event["event_type"] == "app_server_turn_lost"
            ]
            self.assertEqual(len(lost_events), 2)
            self.assertTrue(
                all(
                    event["runtime_generation"] == 1
                    and event["current_runtime_generation"] == 2
                    for event in lost_events
                )
            )

    def test_generation_switch_reconciles_old_jobs_before_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            repo = _repo(base, "generation-switch")
            runtime = StubAppServerRuntime()
            manager = ClusterManager(
                settings,
                team=team,
                runtime=runtime,
                runtime_mode="app_server",
                lead_cooldown_seconds=0,
            )
            goal, tasks = self._goal_with_ready_tasks(
                manager,
                team,
                repo,
                3,
            )
            old_worker = manager._dispatch_worker(
                manager.get_goal(goal["goal_id"]),
                team.get_task(tasks[0]["task_id"]),
            )
            team.claim_task(
                tasks[0]["task_id"],
                actor=manager._worker_actor(
                    tasks[0]["owner_role"],
                    tasks[0]["task_id"],
                    old_worker["job_id"],
                ),
            )
            old_lead = manager._dispatch_lead(
                manager.get_goal(goal["goal_id"]),
                team.list_tasks(repo, goal_id=goal["goal_id"]),
            )
            self.assertEqual(runtime.restart(), {
                "healthy": True,
                "running": True,
                "generation": 2,
            })
            current = manager._dispatch_worker(
                manager.get_goal(goal["goal_id"]),
                team.get_task(tasks[1]["task_id"]),
            )

            manager._reconcile_jobs()

            result = manager.get_goal(goal["goal_id"])
            jobs = {job["job_id"]: job for job in result["jobs"]}
            self.assertEqual(
                jobs[old_worker["job_id"]]["status"],
                "lost",
            )
            self.assertEqual(
                jobs[old_lead["job_id"]]["status"],
                "lost",
            )
            self.assertEqual(
                jobs[current["job_id"]]["status"],
                "running",
            )
            self.assertEqual(
                jobs[old_worker["job_id"]]["runtime_generation"],
                1,
            )
            self.assertEqual(
                jobs[old_lead["job_id"]]["runtime_generation"],
                1,
            )
            self.assertEqual(
                jobs[current["job_id"]]["runtime_generation"],
                2,
            )
            self.assertIn(
                "generation changed from 1 to 2",
                jobs[old_worker["job_id"]]["error"],
            )
            self.assertEqual(
                team.get_task(tasks[0]["task_id"])["status"],
                "ready",
            )
            self.assertIsNone(result["lead_thread_id"])
            self.assertEqual(result["lead_failures"], 1)
            self.assertIn(current["job_id"], manager.app_turns)
            lost_events = [
                event["data"]
                for event in result["events"]
                if event["event_type"] == "app_server_turn_lost"
            ]
            self.assertEqual(
                {event["job_id"] for event in lost_events},
                {old_worker["job_id"], old_lead["job_id"]},
            )
            self.assertTrue(
                all(
                    event["runtime_generation"] == 1
                    and event["current_runtime_generation"] == 2
                    for event in lost_events
                )
            )
            recovered = [
                event["data"]
                for event in result["events"]
                if event["event_type"] == "worker_recovered"
                and event["data"].get("job_id") == old_worker["job_id"]
            ]
            self.assertEqual(len(recovered), 1)
            self.assertIn("generation changed", recovered[0]["reason"])

    def test_initialization_failure_persists_redacted_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            repo = _repo(base, "initialization-diagnostics")
            runtime = StubAppServerRuntime(
                fail_start=1,
                fail_restart=1,
                stderr_lines=(
                    "useful startup diagnostic",
                    "claim_token=claim-secret-value",
                    "OPENAI_API_KEY=api-secret-value",
                ),
            )
            manager = ClusterManager(
                settings,
                team=team,
                runtime=runtime,
                runtime_mode="app_server",
            )
            goal = manager.create_goal(repo, objective="Diagnose startup")
            current_goal = manager.get_goal(goal["goal_id"])

            with patch(
                "codex_team.cluster.subprocess.Popen",
                return_value=FakeExecProcess(),
            ):
                dispatched = manager._dispatch_lead(
                    current_goal,
                    [],
                )

            result = manager.get_goal(goal["goal_id"])
            job = next(
                item
                for item in result["jobs"]
                if item["job_id"] == dispatched["job_id"]
            )
            fallback = next(
                event["data"]
                for event in result["events"]
                if event["event_type"] == "agent_runtime_fallback"
                and event["data"].get("job_id") == job["job_id"]
            )
            self.assertEqual(dispatched["runtime"], "exec")
            for value in (job["error"], fallback["error"]):
                self.assertIn("recent stderr", value)
                self.assertIn("useful startup diagnostic", value)
                self.assertIn("[REDACTED]", value)
                self.assertNotIn("claim-secret-value", value)
                self.assertNotIn("api-secret-value", value)

    def test_private_runtime_uses_logs_dir_for_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = _settings(Path(temporary))
            manager = ClusterManager(
                settings,
                team=TeamService(settings),
                runtime_mode="app_server",
                agent_command=["codex"],
            )
            try:
                self.assertIsNotNone(manager.runtime)
                self.assertEqual(
                    manager.runtime.stderr_log_path,
                    settings.logs_dir / "app-server.stderr.log",
                )
            finally:
                manager.close()

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
            _deliver_token_usage(
                _ClusterAppServerEvents(manager),
                first_job["thread_id"],
                first_job["turn_id"],
                {
                    "total": {
                        "inputTokens": 10,
                        "cachedInputTokens": 2,
                        "outputTokens": 3,
                        "totalTokens": 13,
                    },
                    "last": {},
                    "modelContextWindow": 100,
                },
            )
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
            second_job = manager._dispatch_lead(second, [])
            self.assertEqual(len(runtime.start_threads), 1)
            self.assertEqual(runtime.resume_threads, [first_job["thread_id"]])
            with team.connection(write=False) as connection:
                resumed_action = connection.execute(
                    """
                    SELECT thread_action FROM cluster_jobs WHERE job_id = ?
                    """,
                    (second_job["job_id"],),
                ).fetchone()
            self.assertEqual(resumed_action["thread_action"], "resume")
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

            failed_cancel_goal = manager.create_goal(
                repo,
                objective="Cancel after interrupt failure",
            )
            failed_cancel_task = team.create_task(
                repo,
                title="Cancel failed interrupt",
                description="Interrupt failure must not block cancellation.",
                acceptance="The job converges and failure is audited.",
                kind="read",
                owner_role="team_explorer",
                goal_id=failed_cancel_goal["goal_id"],
            )
            failed_cancel_job = manager._dispatch_worker(
                manager.get_goal(failed_cancel_goal["goal_id"]),
                team.get_task(failed_cancel_task["task_id"]),
            )
            with patch.object(
                runtime,
                "interrupt_turn",
                side_effect=RuntimeError("thread not found"),
            ):
                failed_cancel = manager.cancel_goal(
                    failed_cancel_goal["goal_id"],
                    reason="interrupt failure test",
                )
            self.assertEqual(failed_cancel["status"], "failed")
            converged_job = next(
                job
                for job in failed_cancel["jobs"]
                if job["job_id"] == failed_cancel_job["job_id"]
            )
            self.assertEqual(converged_job["status"], "interrupted")
            interrupt_event = next(
                event
                for event in failed_cancel["events"]
                if event["event_type"] == "app_server_interrupt_failed"
            )
            self.assertEqual(
                interrupt_event["data"]["job_id"],
                failed_cancel_job["job_id"],
            )

    def test_app_server_cancel_persists_logged_usage_without_breaking_completion(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            repo = _repo(base, "interrupted-usage")
            project = team.register_project(repo)
            runtime = StubAppServerRuntime()
            manager = ClusterManager(
                settings,
                team=team,
                runtime=runtime,
                runtime_mode="app_server",
            )
            events = _ClusterAppServerEvents(manager)

            usage_goal = manager.create_goal(
                repo,
                objective="Persist canceled turn usage",
            )
            usage_task = team.create_task(
                repo,
                title="Cancel with logged usage",
                description="The job log already contains token usage.",
                acceptance="Cancellation retains the latest usage snapshot.",
                kind="read",
                owner_role="team_explorer",
                goal_id=usage_goal["goal_id"],
            )
            usage_job = manager._dispatch_worker(
                manager.get_goal(usage_goal["goal_id"]),
                team.get_task(usage_task["task_id"]),
            )
            events.on_token_usage_updated(
                usage_job["thread_id"],
                usage_job["turn_id"],
                {
                    "total": {
                        "input": 120,
                        "cached": 20,
                        "output": 30,
                        "context": 150,
                    },
                    "last": {
                        "input": 50,
                        "cached": 5,
                        "output": 10,
                        "context": 60,
                    },
                    "modelContextWindow": 200,
                },
            )
            events.on_token_usage_updated(
                usage_job["thread_id"],
                usage_job["turn_id"],
                {
                    "total": {
                        "input": 150,
                        "cached": 25,
                        "output": 34,
                        "context": 184,
                    },
                    "last": {
                        "input": 60,
                        "cached": 6,
                        "output": 12,
                        "context": 72,
                    },
                    "modelContextWindow": 200,
                },
            )
            team.claim_task(
                usage_task["task_id"],
                actor="interrupted-usage-test",
            )
            with manager._app_state_lock:
                manager.app_turns[usage_job["job_id"]]["usage"] = {}
                manager.app_turns[usage_job["job_id"]]["first_usage"] = {}
            manager.cancel_goal(
                usage_goal["goal_id"],
                reason="usage recovery test",
            )

            with team.connection(write=False) as connection:
                persisted = connection.execute(
                    """
                    SELECT status, input_tokens, cached_input_tokens,
                           uncached_input_tokens, output_tokens,
                           first_input_tokens, first_cached_input_tokens,
                           first_uncached_input_tokens, context_tokens,
                           model_context_window, usage_source,
                           telemetry_complete, outcome, settled_at,
                           thread_action
                    FROM cluster_jobs
                    WHERE job_id = ?
                    """,
                    (usage_job["job_id"],),
                ).fetchone()
            self.assertEqual(persisted["status"], "interrupted")
            self.assertEqual(persisted["outcome"], "interrupted")
            self.assertIsNotNone(persisted["settled_at"])
            self.assertEqual(persisted["thread_action"], "fresh")
            self.assertEqual(persisted["input_tokens"], 150)
            self.assertEqual(persisted["cached_input_tokens"], 25)
            self.assertEqual(persisted["uncached_input_tokens"], 125)
            self.assertEqual(persisted["output_tokens"], 34)
            self.assertEqual(persisted["first_input_tokens"], 120)
            self.assertEqual(persisted["first_cached_input_tokens"], 20)
            self.assertEqual(persisted["first_uncached_input_tokens"], 100)
            self.assertEqual(persisted["context_tokens"], 184)
            self.assertEqual(persisted["model_context_window"], 200)
            self.assertEqual(
                persisted["usage_source"],
                "app_server.token_usage",
            )
            self.assertEqual(persisted["telemetry_complete"], 1)

            dashboard = team.dashboard_snapshot(project["project_id"])
            dashboard_job = next(
                job
                for job in dashboard["cluster_jobs"]
                if job["job_id"] == usage_job["job_id"]
            )
            self.assertEqual(dashboard_job["input_tokens"], 150)
            self.assertEqual(dashboard_job["output_tokens"], 34)

            missing_goal = manager.create_goal(
                repo,
                objective="Cancel without token usage",
            )
            missing_task = team.create_task(
                repo,
                title="Cancel without usage",
                description="No token usage event was produced.",
                acceptance="Unavailable usage remains unavailable.",
                kind="read",
                owner_role="team_explorer",
                goal_id=missing_goal["goal_id"],
            )
            missing_job = manager._dispatch_worker(
                manager.get_goal(missing_goal["goal_id"]),
                team.get_task(missing_task["task_id"]),
            )
            team.claim_task(
                missing_task["task_id"],
                actor="missing-usage-test",
            )
            manager.cancel_goal(
                missing_goal["goal_id"],
                reason="missing usage test",
            )
            with team.connection(write=False) as connection:
                missing = connection.execute(
                    """
                    SELECT usage_source, model_context_window,
                           telemetry_complete
                    FROM cluster_jobs WHERE job_id = ?
                    """,
                    (missing_job["job_id"],),
                ).fetchone()
            self.assertIsNone(missing["usage_source"])
            self.assertIsNone(missing["model_context_window"])
            self.assertEqual(missing["telemetry_complete"], 0)
            dashboard = team.dashboard_snapshot(project["project_id"])
            missing_dashboard_job = next(
                job
                for job in dashboard["cluster_jobs"]
                if job["job_id"] == missing_job["job_id"]
            )
            self.assertIsNone(missing_dashboard_job["input_tokens"])
            self.assertIsNone(missing_dashboard_job["output_tokens"])

            preserved_goal = manager.create_goal(
                repo,
                objective="Preserve existing usage",
            )
            preserved_task = team.create_task(
                repo,
                title="Cancel with existing usage",
                description="The job already has valid usage but no new log.",
                acceptance="Cancellation does not clear valid usage.",
                kind="read",
                owner_role="team_explorer",
                goal_id=preserved_goal["goal_id"],
            )
            preserved_job = manager._dispatch_worker(
                manager.get_goal(preserved_goal["goal_id"]),
                team.get_task(preserved_task["task_id"]),
            )
            with team.connection() as connection:
                connection.execute(
                    """
                    UPDATE cluster_jobs
                    SET input_tokens = 9, cached_input_tokens = 4,
                        uncached_input_tokens = 5, output_tokens = 3,
                        usage_source = 'existing.valid', telemetry_complete = 1
                    WHERE job_id = ?
                    """,
                    (preserved_job["job_id"],),
                )
            team.claim_task(
                preserved_task["task_id"],
                actor="preserved-usage-test",
            )
            manager.cancel_goal(
                preserved_goal["goal_id"],
                reason="preserved usage test",
            )
            with team.connection(write=False) as connection:
                preserved = connection.execute(
                    """
                    SELECT input_tokens, cached_input_tokens,
                           uncached_input_tokens, output_tokens,
                           usage_source, telemetry_complete
                    FROM cluster_jobs WHERE job_id = ?
                    """,
                    (preserved_job["job_id"],),
                ).fetchone()
            self.assertEqual(preserved["input_tokens"], 9)
            self.assertEqual(preserved["cached_input_tokens"], 4)
            self.assertEqual(preserved["uncached_input_tokens"], 5)
            self.assertEqual(preserved["output_tokens"], 3)
            self.assertEqual(preserved["usage_source"], "existing.valid")
            self.assertEqual(preserved["telemetry_complete"], 1)

            completed_goal = manager.create_goal(
                repo,
                objective="Complete with token usage",
            )
            completed_task = team.create_task(
                repo,
                title="Complete normally",
                description="Normal completion still records token usage.",
                acceptance="Normal completion records the cumulative total.",
                kind="read",
                owner_role="team_explorer",
                goal_id=completed_goal["goal_id"],
            )
            completed_job = manager._dispatch_worker(
                manager.get_goal(completed_goal["goal_id"]),
                team.get_task(completed_task["task_id"]),
            )
            events.on_token_usage_updated(
                completed_job["thread_id"],
                completed_job["turn_id"],
                {
                    "total": {
                        "input": 100,
                        "cached": 10,
                        "output": 20,
                        "context": 120,
                    },
                    "last": {
                        "input": 40,
                        "cached": 4,
                        "output": 8,
                        "context": 48,
                    },
                    "modelContextWindow": 200,
                },
            )
            events.on_turn_completed(
                completed_job["thread_id"],
                completed_job["turn_id"],
                {
                    "id": completed_job["turn_id"],
                    "status": "completed",
                },
            )
            with team.connection() as connection:
                connection.execute(
                    """
                    UPDATE tasks
                    SET status = 'review', updated_at = ?, version = version + 1
                    WHERE task_id = ?
                    """,
                    (utc_now(), completed_task["task_id"]),
                )
            manager._reconcile_jobs()
            with team.connection(write=False) as connection:
                completed = connection.execute(
                    """
                    SELECT status, input_tokens, cached_input_tokens,
                           output_tokens, telemetry_complete
                    FROM cluster_jobs WHERE job_id = ?
                    """,
                    (completed_job["job_id"],),
                ).fetchone()
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["input_tokens"], 100)
            self.assertEqual(completed["cached_input_tokens"], 10)
            self.assertEqual(completed["output_tokens"], 20)
            self.assertEqual(completed["telemetry_complete"], 1)

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
            _deliver_token_usage(
                _ClusterAppServerEvents(manager),
                dispatched["thread_id"],
                dispatched["turn_id"],
                {
                    "total": {
                        "inputTokens": 100,
                        "cachedInputTokens": 80,
                        "outputTokens": 20,
                        "totalTokens": 120,
                    },
                    "last": {},
                    "modelContextWindow": 1000,
                },
            )
            team.claim_task(
                task["task_id"],
                actor=manager._worker_actor(
                    task["owner_role"],
                    task["task_id"],
                    dispatched["job_id"],
                ),
            )

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
            self.assertEqual(lost["outcome"], "lost")
            self.assertIsNotNone(lost["settled_at"])
            self.assertEqual(lost["input_tokens"], 100)
            self.assertEqual(lost["cached_input_tokens"], 80)
            self.assertEqual(lost["uncached_input_tokens"], 20)
            self.assertEqual(lost["output_tokens"], 20)
            self.assertEqual(lost["context_tokens"], 120)
            self.assertEqual(lost["model_context_window"], 1000)
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

    def test_attempt_ledger_records_prompt_profile_action_and_settlement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            repo = _repo(base, "attempt-ledger")
            team.init_project(repo)
            runtime = StubAppServerRuntime()
            manager = ClusterManager(
                settings,
                team=team,
                runtime=runtime,
                runtime_mode="app_server",
            )
            goal = manager.create_goal(repo, objective="Record attempt identity")
            task = team.create_task(
                repo,
                title="Record attempt",
                description="Persist identity for one App Server job.",
                acceptance="Attempt ledger fields are complete.",
                kind="read",
                owner_role="team_explorer",
                goal_id=goal["goal_id"],
            )
            dispatched = manager._dispatch_worker(
                manager.get_goal(goal["goal_id"]),
                team.get_task(task["task_id"]),
            )
            with team.connection(write=False) as connection:
                started = connection.execute(
                    """
                    SELECT correlation_id, prompt_hash, cache_profile,
                           thread_action, outcome, settled_at, workflow_epoch
                    FROM cluster_jobs WHERE job_id = ?
                    """,
                    (dispatched["job_id"],),
                ).fetchone()
            self.assertEqual(started["correlation_id"], dispatched["job_id"])
            self.assertEqual(len(started["prompt_hash"]), 64)
            self.assertIn("cache-v1", started["cache_profile"])
            self.assertEqual(
                started["workflow_epoch"],
                task["workflow_epoch"],
            )
            self.assertEqual(started["thread_action"], "fresh")
            self.assertEqual(started["outcome"], "")
            self.assertIsNone(started["settled_at"])

            _deliver_token_usage(
                _ClusterAppServerEvents(manager),
                dispatched["thread_id"],
                dispatched["turn_id"],
                {
                    "total": {
                        "inputTokens": 100,
                        "cachedInputTokens": 80,
                        "outputTokens": 20,
                        "totalTokens": 120,
                    },
                    "last": {},
                    "modelContextWindow": 1000,
                },
            )
            actor = manager._worker_actor(
                str(task["owner_role"]),
                str(task["task_id"]),
                str(dispatched["job_id"]),
            )
            claimed = team.claim_task(task["task_id"], actor=actor)
            team.submit_task(
                task["task_id"],
                actor=actor,
                claim_token=claimed["claim_token"],
                result="attempt ledger fixture",
                tests="fixture",
            )
            manager.app_turns[dispatched["job_id"]]["status"] = "completed"
            manager._reconcile_jobs()

            with team.connection(write=False) as connection:
                settled = connection.execute(
                    """
                    SELECT status, outcome, settled_at, thread_action
                    FROM cluster_jobs WHERE job_id = ?
                    """,
                    (dispatched["job_id"],),
                ).fetchone()
            self.assertEqual(settled["status"], "completed")
            self.assertEqual(settled["outcome"], "completed")
            self.assertIsNotNone(settled["settled_at"])
            self.assertEqual(settled["thread_action"], "fresh")
            events = team.get_task_events(task["task_id"])["events"]
            claimed_event = next(
                event
                for event in reversed(events)
                if event["event_type"] == "claimed"
                and event["job_id"] == dispatched["job_id"]
            )
            self.assertEqual(claimed_event["job_id"], dispatched["job_id"])
            self.assertEqual(claimed_event["thread_id"], dispatched["thread_id"])
            self.assertEqual(
                claimed_event["correlation_id"],
                dispatched["job_id"],
            )

    def test_default_runtime_is_app_server_and_workstream_contract_is_persisted(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            with patch.dict(os.environ, {"CODEX_TEAM_AGENT_RUNTIME": ""}):
                manager = ClusterManager(
                    settings,
                    team=TeamService(settings),
                )
            self.assertEqual(manager.agent_runtime, "app_server")
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

    def test_explicit_exec_runtime_can_disable_thread_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = _settings(Path(temporary))
            with patch.dict(
                os.environ,
                {"CODEX_TEAM_AGENT_RUNTIME": "exec"},
            ):
                manager = ClusterManager(
                    settings,
                    team=TeamService(settings),
                )
            self.assertEqual(manager.agent_runtime, "exec")
            self.assertIsNone(manager.runtime)

    def test_checkpoint_and_prompt_layout_are_cache_friendly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            manager = ClusterManager(
                settings,
                team=TeamService(settings),
            )
            repo = _repo(base, "checkpoint")
            goal = manager.create_goal(
                repo,
                objective="Implement durable continuity",
                workstream_key="context-v2",
            )
            updated = manager.set_goal_checkpoint(
                goal["goal_id"],
                summary=(
                    "Decisions: use fresh subagents.\n"
                    "Tests: parser and cluster suites.\n"
                    "Next: measure cache."
                ),
            )
            self.assertIn(
                "fresh subagents",
                updated["checkpoint_summary"],
            )
            self.assertTrue(updated["checkpoint_updated_at"])
            latest = manager.latest_workstream_checkpoint(
                goal["project_id"],
                "context-v2",
            )
            self.assertEqual(latest["goal_id"], goal["goal_id"])
            manager.set_goal_status(
                goal["goal_id"],
                "active",
                reason="checkpoint archive test",
            )
            manager.set_goal_status(
                goal["goal_id"],
                "completed",
                reason="checkpoint archive test",
            )
            manager.set_goal_archived(
                goal["goal_id"],
                True,
                actor="test",
            )
            archived_latest = manager.latest_workstream_checkpoint(
                goal["project_id"],
                "context-v2",
            )
            self.assertEqual(archived_latest["goal_id"], goal["goal_id"])
            inherited = manager._checkpoint_context(
                {
                    **goal,
                    "checkpoint_summary": "",
                    "checkpoint_updated_at": None,
                }
            )
            self.assertEqual(inherited["source"], "latest_workstream")
            prompt = _stable_developer_instructions(
                goal,
                "team_explorer",
                "ROLE_CONTRACT_MARKER",
            )
            self.assertTrue(
                prompt.startswith("# Managed Agent Role Contract")
            )
            self.assertLess(
                prompt.index("ROLE_CONTRACT_MARKER"),
                prompt.index("## Thread Scope"),
            )

    def test_agent_cache_profile_tracks_worktree_policy_sandbox_and_surface(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "repo"
            worktree = base / "worktree"
            root.mkdir()
            worktree.mkdir()
            (root / "AGENTS.md").write_text("root policy", encoding="utf-8")
            (worktree / "AGENTS.md").write_text(
                "worktree policy",
                encoding="utf-8",
            )
            goal = {
                "goal_id": "G-CACHE",
                "project_id": "P-CACHE",
                "root_path": str(root),
                "workstream_key": "cache-profile",
            }
            _, root_profile = _agent_cache_profile(
                goal,
                "team_worker",
                "ROLE",
                sandbox_mode="workspace-write",
            )
            _, worktree_profile = _agent_cache_profile(
                goal,
                "team_worker",
                "ROLE",
                cwd=worktree,
                sandbox_mode="workspace-write",
            )
            _, dangerous_profile = _agent_cache_profile(
                goal,
                "team_worker",
                "ROLE",
                cwd=worktree,
                sandbox_mode="danger-full-access",
            )
            self.assertNotEqual(root_profile, worktree_profile)
            self.assertNotEqual(worktree_profile, dangerous_profile)
            with patch.dict(
                os.environ,
                {"CODEX_TEAM_MCP_SURFACE": "legacy"},
                clear=False,
            ):
                _, legacy_profile = _agent_cache_profile(
                    goal,
                    "team_worker",
                    "ROLE",
                    cwd=worktree,
                    sandbox_mode="workspace-write",
                )
            self.assertNotEqual(worktree_profile, legacy_profile)

    def test_create_goal_does_not_rewrite_project_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            repo = _repo(base, "goal-policy")
            project = team.init_project(repo)
            agents_path = repo / "AGENTS.md"
            before = agents_path.read_bytes()
            manager = ClusterManager(
                settings,
                team=team,
                runtime_mode="exec",
            )
            with patch.object(
                team,
                "init_project",
                side_effect=AssertionError("create_goal must not re-init project"),
            ):
                goal = manager.create_goal(
                    repo,
                    objective="Do not rewrite policy",
                )
            self.assertEqual(goal["project_id"], project["project_id"])
            self.assertEqual(agents_path.read_bytes(), before)

    def test_ensure_goal_does_not_initialize_or_rewrite_project_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            repo = _repo(base, "ensure-goal-policy")
            agents_path = repo / "AGENTS.md"
            agents_path.write_text("stable policy\n", encoding="utf-8")
            before = agents_path.read_bytes()
            manager = ClusterManager(
                settings,
                team=team,
                runtime_mode="exec",
            )
            with patch.object(
                team,
                "init_project",
                side_effect=AssertionError("ensure_goal must not initialize project"),
            ):
                first = manager.ensure_goal(
                    repo,
                    objective="Create a goal without prompt-file writes",
                )
            self.assertTrue(first["created"])
            self.assertFalse((repo / ".codex").exists())

            project = team.register_project(repo)
            with patch.object(
                team,
                "init_project",
                side_effect=AssertionError("ensure_goal must not re-init project"),
            ):
                second = manager.ensure_goal(
                    repo,
                    objective="Reuse the same goal without writing policy",
                )
            self.assertEqual(project["project_id"], second["goal"]["project_id"])
            self.assertEqual(before, agents_path.read_bytes())

    def test_lead_board_digest_bounds_completed_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            manager = ClusterManager(
                settings,
                team=TeamService(settings),
            )
            goal = {
                "goal_id": "G-DIGEST",
                "objective": "Bound history",
                "autonomy": "auto",
                "project_id": "P-DIGEST",
                "root_path": str(base),
                "workstream_key": "digest",
            }
            tasks = [
                {
                    "task_id": f"T-{index:02d}",
                    "title": f"Done {index}",
                    "kind": "read",
                    "status": "done",
                    "owner_role": "team_explorer",
                    "priority": 2,
                    "completed_at": f"2026-11-08T00:{index:02d}:00Z",
                    "updated_at": f"2026-11-08T00:{index:02d}:00Z",
                    "result": "x" * 2000,
                    "tests": "ok",
                    "commit_sha": "",
                }
                for index in range(10)
            ]
            tasks.append(
                {
                    "task_id": "T-ACTIVE",
                    "title": "Active",
                    "description": "Still running",
                    "acceptance": "Finish",
                    "kind": "write",
                    "status": "running",
                    "owner_role": "team_worker",
                    "priority": 1,
                    "blocked_by": [],
                }
            )
            digest = manager._lead_board_digest(
                goal,
                tasks,
                {"summary": "durable checkpoint"},
            )
            self.assertEqual(digest["counts"], {"done": 10, "running": 1})
            self.assertEqual(len(digest["active_tasks"]), 1)
            self.assertEqual(len(digest["recent_completed"]), 8)
            self.assertEqual(digest["omitted_completed_count"], 2)
            self.assertIn(
                "...[truncated]",
                digest["recent_completed"][0]["result"],
            )
            self.assertEqual(
                digest["checkpoint"]["summary"],
                "durable checkpoint",
            )

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
        checkpoint_args = parser.parse_args(
            [
                "cluster",
                "checkpoint",
                "G-EXAMPLE",
                "--summary",
                "Durable checkpoint",
            ]
        )
        self.assertEqual(checkpoint_args.summary, "Durable checkpoint")
        migrate_args = parser.parse_args(
            [
                "cluster",
                "migrate-workflow",
                "G-EXAMPLE",
                "--actor",
                "human",
                "--reason",
                "explicit upgrade",
            ]
        )
        self.assertEqual(migrate_args.cluster_command, "migrate-workflow")
        self.assertEqual(migrate_args.reason, "explicit upgrade")
        agent_args = parser.parse_args(["agent", "reset", "A-EXAMPLE"])
        self.assertEqual(agent_args.agent_command, "reset")

        tool_names = {item["name"] for item in _tool_definitions()}
        self.assertTrue(
            {
                "team_agent_register_native",
                "team_agent_list",
                "team_agent_reset",
                "team_goal_set_checkpoint",
                "team_goal_set_workstream",
                "team_goal_migrate_workflow",
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
            checkpoint = server.call_tool(
                "team_goal_set_checkpoint",
                {
                    "goal_id": goal["goal_id"],
                    "summary": "MCP durable checkpoint",
                },
            )
            self.assertEqual(
                checkpoint["checkpoint_summary"],
                "MCP durable checkpoint",
            )
            with team.connection() as connection:
                connection.execute(
                    "UPDATE cluster_goals SET workflow_epoch = 'legacy-unbound' WHERE goal_id = ?",
                    (goal["goal_id"],),
                )
            migrated = server.call_tool(
                "team_goal_migrate_workflow",
                {
                    "goal_id": goal["goal_id"],
                    "actor": "human",
                    "reason": "MCP explicit migration",
                },
            )
            self.assertTrue(
                migrated["workflow_epoch"].startswith("workflow-v4:")
            )
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
            task = team.create_task(
                repo,
                title="Native MCP registration",
                description="Register a native thread through Core dispatcher.",
                acceptance="Native metadata is persisted.",
                kind="read",
                owner_role="team_explorer",
                goal_id=goal["goal_id"],
            )
            thread_id = "01a1cafe-2222-7333-8444-955566667777"
            sessions = base / "native-sessions"
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
                                    "text": f"Execute {task['task_id']}",
                                }
                            ],
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"CODEX_NATIVE_SESSIONS_ROOT": str(sessions)},
                clear=False,
            ):
                native = server.call_tool(
                    "team_agent_register_native",
                    {
                        "repo_root": str(repo),
                        "thread_id": thread_id,
                        "role": "team_explorer",
                        "workstream_key": "native-mcp",
                        "actor": "native-mcp-worker",
                        "task_id": task["task_id"],
                    },
                )
            self.assertEqual(native["agent"]["runtime"], "native")
            self.assertEqual(
                native["agent"]["prompt_status"],
                "delivered",
            )
            self.assertTrue(native["same_task_only"])

    def test_mcp_resume_daemon_failure_keeps_goal_paused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = _settings(Path(temporary))
            team = TeamService(settings)
            repo = _repo(Path(temporary), "mcp-resume-failure")
            manager = ClusterManager(settings, team=team)
            goal = manager.create_goal(
                repo,
                objective="Keep paused when daemon resume fails",
            )
            manager.set_goal_status(
                goal["goal_id"],
                "paused",
                reason="fixture pause",
            )
            server = TeamMcpServer(settings)
            try:
                with patch(
                    "codex_team.mcp.ensure_daemon",
                    side_effect=RuntimeError("daemon failed"),
                ):
                    with self.assertRaisesRegex(
                        TeamError,
                        "failed to start",
                    ):
                        server.call_tool(
                            "team_cluster_control",
                            {
                                "goal_id": goal["goal_id"],
                                "action": "resume",
                            },
                        )
                self.assertEqual(
                    manager.get_goal(goal["goal_id"])["status"],
                    "paused",
                )
            finally:
                server.cluster.close()
                manager.close()

    def test_agent_cache_benchmark_emits_json_markdown_and_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            fresh_path = base / "fresh.jsonl"
            reuse_path = base / "reuse.jsonl"
            fresh_path.write_text(
                "\n".join(
                    json.dumps(item, sort_keys=True)
                    for item in (
                        {
                            "first_turn_latency_ms": 1000,
                            "task_success": True,
                            "total_uncached_input_tokens": 1000,
                            "last_uncached_input_tokens": 400,
                        },
                        {
                            "first_turn_latency_ms": 900,
                            "task_success": True,
                            "total_uncached_input_tokens": 1200,
                            "last_uncached_input_tokens": 200,
                        },
                        {
                            "first_turn_latency_ms": 1100,
                            "task_success": True,
                            "total_uncached_input_tokens": 1400,
                            "last_uncached_input_tokens": 600,
                        },
                        {
                            "first_turn_latency_ms": 1,
                            "task_success": False,
                            "total_uncached_input_tokens": 1,
                            "last_uncached_input_tokens": 1,
                        },
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            reuse_path.write_text(
                "\n".join(
                    json.dumps(item, sort_keys=True)
                    for item in (
                        {
                            "first_turn_latency_ms": 900,
                            "task_success": True,
                            "total_uncached_input_tokens": 2000,
                            "last_uncached_input_tokens": 250,
                        },
                        {
                            "first_turn_latency_ms": 1000,
                            "task_success": True,
                            "total_uncached_input_tokens": 3000,
                            "last_uncached_input_tokens": 350,
                        },
                        {
                            "first_turn_latency_ms": 800,
                            "task_success": True,
                            "total_uncached_input_tokens": 4000,
                            "last_uncached_input_tokens": 150,
                        },
                        {
                            "first_turn_latency_ms": 1,
                            "task_success": False,
                            "total_uncached_input_tokens": 1,
                            "last_uncached_input_tokens": 1,
                        },
                    )
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
            self.assertEqual(
                report["variants"]["fresh"]["sample_count"],
                4,
            )
            self.assertEqual(
                report["variants"]["fresh"]["successful_sample_count"],
                3,
            )
            self.assertEqual(
                report["variants"]["fresh"]["median"][
                    "last_uncached_input_tokens"
                ],
                400,
            )
            self.assertEqual(
                report["variants"]["reuse"]["median"][
                    "last_uncached_input_tokens"
                ],
                250,
            )
            self.assertAlmostEqual(
                report["comparison"]["uncached_input_change"],
                -0.375,
            )
            self.assertAlmostEqual(
                report["comparison"][
                    "total_uncached_input_change_diagnostic"
                ],
                1.5,
            )
            markdown = markdown_output.read_text(encoding="utf-8")
            self.assertIn(
                "median `last_uncached_input_tokens` falls by at least 10%",
                markdown,
            )
            self.assertIn("Median total_input_tokens (diagnostic)", markdown)
            self.assertIn("Sample count", markdown)
            self.assertIn("Result: PASS", markdown)

    def test_agent_cache_benchmark_requires_success_and_last_usage(self) -> None:
        script = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "agent-cache-benchmark.py"
        )
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            fresh = base / "fresh.jsonl"
            reuse = base / "reuse.jsonl"
            missing_success = base / "missing-success.jsonl"
            fresh.write_text(
                json.dumps(
                    {
                        "first_turn_latency_ms": 1000,
                        "task_success": True,
                        "last_uncached_input_tokens": 100,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            reuse.write_text(
                json.dumps(
                    {
                        "first_turn_latency_ms": 800,
                        "task_success": True,
                        "last_uncached_input_tokens": 80,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            missing_success.write_text(
                json.dumps(
                    {
                        "first_turn_latency_ms": 1000,
                        "last_uncached_input_tokens": 100,
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            rejected = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--fresh",
                    str(missing_success),
                    "--reuse",
                    str(reuse),
                ],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("task_success", rejected.stderr)

            no_last = base / "no-last.jsonl"
            no_last.write_text(
                "\n".join(
                    json.dumps(record)
                    for record in (
                        {
                            "first_turn_latency_ms": 1000,
                            "task_success": True,
                            "total_input_tokens": 1000,
                            "total_cached_input_tokens": 500,
                            "total_uncached_input_tokens": 500,
                        },
                        {
                            "first_turn_latency_ms": 100,
                            "task_success": True,
                            "total_input_tokens": 2000,
                            "total_cached_input_tokens": 1500,
                            "total_uncached_input_tokens": 500,
                        },
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            report_path = base / "no-last.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--fresh",
                    str(no_last),
                    "--reuse",
                    str(no_last),
                    "--json",
                    str(report_path),
                ],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertFalse(report["comparison"]["last_usage_gate"])
            self.assertFalse(report["comparison"]["meets_gate"])
            self.assertFalse(report["variants"]["fresh"]["last_usage_complete"])
            self.assertIsNone(
                report["variants"]["fresh"]["median_last_uncached_input_tokens"]
            )
            self.assertIsNone(report["variants"]["fresh"]["cache_hit_rate"])

            nested = base / "nested.jsonl"
            nested.write_text(
                "\n".join(
                    json.dumps(record)
                    for record in (
                        {
                            "first_turn_latency_ms": 1000,
                            "task_success": True,
                            "usage": {
                                "total": {
                                    "input": 1000,
                                    "cached": 900,
                                    "uncached": 100,
                                },
                                "last": {
                                    "input": 100,
                                    "cached": 20,
                                    "uncached": 80,
                                },
                            },
                        },
                        {
                            "first_turn_latency_ms": 1000,
                            "task_success": True,
                            "usage": {
                                "total": {
                                    "input": 1000,
                                    "cached": 100,
                                    "uncached": 900,
                                },
                                "last": {
                                    "input": 100,
                                    "cached": 40,
                                    "uncached": 60,
                                },
                            },
                        },
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            nested_report_path = base / "nested.json"
            nested_result = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--fresh",
                    str(nested),
                    "--reuse",
                    str(nested),
                    "--json",
                    str(nested_report_path),
                ],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            self.assertEqual(nested_result.returncode, 0, nested_result.stderr)
            nested_report = json.loads(
                nested_report_path.read_text(encoding="utf-8")
            )
            self.assertEqual(
                nested_report["variants"]["fresh"]["median_last_uncached_input_tokens"],
                70,
            )
            self.assertAlmostEqual(
                nested_report["variants"]["fresh"]["cache_hit_rate"],
                0.3,
            )


if __name__ == "__main__":
    unittest.main()
