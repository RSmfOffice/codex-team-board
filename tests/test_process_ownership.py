from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from codex_team.cluster import (
    ClusterError,
    ClusterManager,
    daemon_paths,
    daemon_status,
    ensure_daemon,
    stop_daemon,
)
from codex_team.clusterd import acquire_lock as acquire_clusterd_lock
from codex_team.config import Settings
from codex_team.db import utc_now
from codex_team.processes import (
    ProcessJob,
    current_process_identity,
    pid_is_running,
    process_identity,
    process_identity_matches,
)
from codex_team.service import TeamService


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
    subprocess.run(
        ["git", "-C", str(repo), "init", "-b", "main"],
        capture_output=True,
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "config",
            "user.email",
            "ownership-test@example.invalid",
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Ownership Test"],
        check=True,
    )
    return repo


class ProcessIdentityTests(unittest.TestCase):
    def test_identity_requires_pid_creation_time_and_executable(self) -> None:
        identity = current_process_identity()
        self.assertTrue(identity)
        self.assertTrue(process_identity_matches(identity))

        for field, value in (
            ("pid", identity["pid"] + 1),
            (
                "creation_time",
                f"{identity['creation_time']}-forged",
            ),
            ("executable", str(identity["executable"]) + ".forged"),
        ):
            forged = dict(identity)
            forged[field] = value
            with self.subTest(field=field):
                self.assertFalse(process_identity_matches(forged))
        empty_executable = dict(identity)
        empty_executable["executable"] = ""
        self.assertFalse(process_identity_matches(empty_executable))

    def test_daemon_status_rejects_forged_pid_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = _settings(Path(temporary))
            lock_path, _, _ = daemon_paths(settings)
            lock_path.parent.mkdir(parents=True)
            identity = current_process_identity()
            identity["creation_time"] = f"{identity['creation_time']}-forged"
            lock_path.write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "process_identity": identity,
                        "started_at": utc_now(),
                    }
                ),
                encoding="utf-8",
            )

            status = daemon_status(settings)
            self.assertFalse(status["running"])
            self.assertFalse(status["process_identity_verified"])
            self.assertTrue(status["unverified_live_process"])
            with patch("codex_team.cluster.subprocess.run") as run:
                with self.assertRaisesRegex(
                    ClusterError,
                    "unverified live process",
                ):
                    stop_daemon(settings)
            run.assert_not_called()
            self.assertTrue(lock_path.exists())

    def test_daemon_status_requires_lock_pid_to_match_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = _settings(Path(temporary))
            lock_path, _, _ = daemon_paths(settings)
            lock_path.parent.mkdir(parents=True)
            exited = subprocess.Popen([sys.executable, "-c", "pass"])
            exited.wait(timeout=5)
            lock_path.write_text(
                json.dumps(
                    {
                        "pid": exited.pid,
                        "process_identity": current_process_identity(),
                        "started_at": utc_now(),
                    }
                ),
                encoding="utf-8",
            )

            status = daemon_status(settings)
            self.assertFalse(status["running"])
            self.assertFalse(status["process_identity_verified"])
            with patch("codex_team.cluster.subprocess.run") as run:
                stopped = stop_daemon(settings)
            run.assert_not_called()
            self.assertIsNone(stopped["stopped_pid"])

    def test_daemon_lock_rejects_forged_live_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = _settings(Path(temporary))
            lock_path, _, _ = daemon_paths(settings)
            lock_path.parent.mkdir(parents=True)
            identity = current_process_identity()
            identity["executable"] = str(identity["executable"]) + ".forged"
            lock_path.write_text(
                json.dumps({"pid": os.getpid(), "process_identity": identity}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "unverified live process",
            ):
                with acquire_clusterd_lock(settings):
                    self.fail("forged live lock must not be replaced")
            self.assertTrue(lock_path.exists())
            with self.assertRaisesRegex(
                ClusterError,
                "unverified live process",
            ):
                stop_daemon(settings)
            self.assertTrue(lock_path.exists())

    def test_daemon_lock_rejects_legacy_live_pid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = _settings(Path(temporary))
            lock_path, _, _ = daemon_paths(settings)
            lock_path.parent.mkdir(parents=True)
            lock_path.write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "started_at": utc_now(),
                    }
                ),
                encoding="utf-8",
            )

            status = daemon_status(settings)
            self.assertFalse(status["running"])
            self.assertTrue(status["unverified_live_process"])
            with self.assertRaisesRegex(
                RuntimeError,
                "stop the legacy daemon",
            ):
                with acquire_clusterd_lock(settings):
                    self.fail("legacy live lock must not be replaced")
            self.assertTrue(lock_path.exists())
            with self.assertRaisesRegex(
                ClusterError,
                "stop the legacy daemon",
            ):
                stop_daemon(settings)
            self.assertTrue(lock_path.exists())
            with patch("codex_team.cluster.subprocess.Popen") as popen:
                with self.assertRaisesRegex(
                    ClusterError,
                    "stop the legacy daemon",
                ):
                    ensure_daemon(settings)
            popen.assert_not_called()
            self.assertTrue(lock_path.exists())

    def test_daemon_lock_rejects_corrupt_unverifiable_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = _settings(Path(temporary))
            lock_path, _, _ = daemon_paths(settings)
            lock_path.parent.mkdir(parents=True)
            lock_path.write_text("{", encoding="utf-8")

            status = daemon_status(settings)
            self.assertFalse(status["running"])
            self.assertFalse(status["lock_state_valid"])
            with self.assertRaisesRegex(RuntimeError, "lock is corrupt"):
                with acquire_clusterd_lock(settings):
                    self.fail("corrupt lock must not be replaced")
            with self.assertRaisesRegex(ClusterError, "lock is corrupt"):
                stop_daemon(settings)
            with patch("codex_team.cluster.subprocess.Popen") as popen:
                with self.assertRaisesRegex(ClusterError, "lock is corrupt"):
                    ensure_daemon(settings)
            popen.assert_not_called()
            self.assertTrue(lock_path.exists())

    def test_unverified_persisted_identity_is_not_running(self) -> None:
        identity = current_process_identity()
        identity["instance_id"] = "0" * 64
        identity["creation_time"] = f"{identity['creation_time']}-old"
        self.assertFalse(process_identity_matches(identity))


@unittest.skipUnless(os.name == "nt", "Windows Job Objects only")
class WindowsJobObjectTests(unittest.TestCase):
    def test_daemon_stop_terminates_verified_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = _settings(Path(temporary))
            lock_path, _, _ = daemon_paths(settings)
            lock_path.parent.mkdir(parents=True)
            process = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            identity = process_identity(process.pid)
            self.assertTrue(identity)
            lock_path.write_text(
                json.dumps(
                    {
                        "pid": process.pid,
                        "process_identity": identity,
                        "started_at": utc_now(),
                    }
                ),
                encoding="utf-8",
            )
            status = daemon_status(settings)
            self.assertTrue(status["running"])
            stopped = stop_daemon(settings)
            self.assertEqual(stopped["stopped_pid"], process.pid)
            process.wait(timeout=5)
            self.assertFalse(pid_is_running(process.pid))

    def test_daemon_stop_does_not_unlink_while_verified_pid_lives(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = _settings(Path(temporary))
            lock_path, _, _ = daemon_paths(settings)
            lock_path.parent.mkdir(parents=True)
            process = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            identity = process_identity(process.pid)
            self.assertTrue(identity)
            lock_path.write_text(
                json.dumps(
                    {
                        "pid": process.pid,
                        "process_identity": identity,
                        "started_at": utc_now(),
                    }
                ),
                encoding="utf-8",
            )
            try:
                with patch(
                    "codex_team.cluster.subprocess.run",
                    return_value=subprocess.CompletedProcess(
                        [],
                        0,
                        stdout="",
                        stderr="",
                    ),
                ), patch(
                    "codex_team.cluster._wait_for_pid_exit",
                    return_value=False,
                ):
                    with self.assertRaisesRegex(
                        ClusterError,
                        "still running after stop",
                    ):
                        stop_daemon(settings)
                self.assertTrue(lock_path.exists())
                self.assertTrue(pid_is_running(process.pid))
            finally:
                process.kill()
                process.wait(timeout=5)

    def test_job_object_close_terminates_descendant_tree(self) -> None:
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import subprocess, sys, time; "
                    "child = subprocess.Popen([sys.executable, '-c', "
                    "'import time; time.sleep(60)']); "
                    "print(child.pid, flush=True); child.wait()"
                ),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        job = ProcessJob.attach(process)
        self.assertIsNotNone(job)
        try:
            assert process.stdout is not None
            child_pid = int(process.stdout.readline().strip())
            self.assertTrue(pid_is_running(child_pid))
            self.assertTrue(process_identity(child_pid))
        finally:
            assert job is not None
            job.close()
        process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and pid_is_running(child_pid):
            time.sleep(0.02)
        self.assertFalse(pid_is_running(child_pid))

    def test_exec_timeout_uses_owned_job_and_persists_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            manager = ClusterManager(
                settings,
                team=team,
                runtime_mode="exec",
            )
            try:
                repo = _repo(base, "exec-timeout")
                goal = manager.create_goal(repo, objective="Own exec process")
                task = team.create_task(
                    repo,
                    title="Timeout owned process",
                    description="Persist identity before timeout.",
                    acceptance="Only the owned Job Object is terminated.",
                    kind="read",
                    owner_role="team_explorer",
                    goal_id=goal["goal_id"],
                )
                with patch.object(
                    manager,
                    "_build_command",
                    return_value=[
                        sys.executable,
                        "-c",
                        "import time; time.sleep(60)",
                    ],
                ):
                    dispatched = manager._dispatch_worker(
                        manager.get_goal(goal["goal_id"]),
                        team.get_task(task["task_id"]),
                    )

                with team.connection(write=False) as connection:
                    persisted = connection.execute(
                        """
                        SELECT pid, process_creation_time,
                               process_executable, process_instance_id
                        FROM cluster_jobs WHERE job_id = ?
                        """,
                        (dispatched["job_id"],),
                    ).fetchone()
                self.assertTrue(persisted["pid"])
                self.assertTrue(persisted["process_creation_time"])
                self.assertTrue(persisted["process_executable"])
                self.assertTrue(persisted["process_instance_id"])
                self.assertIn(dispatched["job_id"], manager.process_jobs)

                manager.worker_timeout_seconds = -1
                with patch("codex_team.cluster.subprocess.run") as run:
                    manager._reconcile_jobs()
                run.assert_not_called()
                job = next(
                    item
                    for item in manager.get_goal(goal["goal_id"])["jobs"]
                    if item["job_id"] == dispatched["job_id"]
                )
                self.assertEqual(job["status"], "failed")
                self.assertEqual(job["outcome"], "failed")
                self.assertIsNotNone(job["settled_at"])
                self.assertFalse(pid_is_running(int(persisted["pid"])))
            finally:
                manager.close()

    def test_exec_start_failure_settles_job_and_kills_launched_process(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            manager = ClusterManager(
                settings,
                team=team,
                runtime_mode="exec",
            )
            try:
                repo = _repo(base, "exec-start-failure")
                goal = manager.create_goal(
                    repo,
                    objective="Settle failed ownership assignment",
                )
                task = team.create_task(
                    repo,
                    title="Fail Job Object assignment",
                    description="The launched process must not remain orphaned.",
                    acceptance="The job is failed and settled.",
                    kind="read",
                    owner_role="team_explorer",
                    goal_id=goal["goal_id"],
                )
                with patch.object(
                    manager,
                    "_build_command",
                    return_value=[
                        sys.executable,
                        "-c",
                        "import time; time.sleep(60)",
                    ],
                ), patch(
                    "codex_team.cluster.ProcessJob.attach",
                    side_effect=OSError("assignment failed"),
                ):
                    with self.assertRaisesRegex(
                        Exception,
                        "assignment failed",
                    ):
                        manager._dispatch_worker(
                            manager.get_goal(goal["goal_id"]),
                            team.get_task(task["task_id"]),
                        )
                jobs = manager.get_goal(goal["goal_id"])["jobs"]
                failed = next(
                    job for job in jobs if job["status"] == "failed"
                )
                self.assertEqual(failed["outcome"], "failed")
                self.assertIsNotNone(failed["settled_at"])
                self.assertIn("assignment failed", failed["error"])
            finally:
                manager.close()

    def test_reconcile_terminates_verified_ownerless_job_and_marks_lost(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            manager = ClusterManager(
                settings,
                team=team,
                runtime_mode="exec",
            )
            process = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                repo = _repo(base, "ownerless-live")
                goal = manager.create_goal(
                    repo,
                    objective="Settle an ownerless live exec job",
                )
                task = team.create_task(
                    repo,
                    title="Ownerless live job",
                    description="The job has identity but no local Popen.",
                    acceptance="It is terminated once and settled lost.",
                    kind="read",
                    owner_role="team_explorer",
                    goal_id=goal["goal_id"],
                )
                identity = process_identity(process.pid)
                self.assertTrue(identity)
                with team.connection() as connection:
                    connection.execute(
                        """
                        INSERT INTO cluster_jobs(
                            job_id, goal_id, task_id, kind, role, status,
                            pid, process_creation_time, process_executable,
                            process_instance_id, attempt, log_path,
                            output_path, started_at
                        ) VALUES (
                            'J-OWNERLESS', ?, ?, 'worker', 'team_explorer',
                            'running', ?, ?, ?, ?, 1, ?, ?, ?
                        )
                        """,
                        (
                            goal["goal_id"],
                            task["task_id"],
                            identity["pid"],
                            identity["creation_time"],
                            identity["executable"],
                            identity["instance_id"],
                            str(base / "ownerless.log"),
                            str(base / "ownerless.out"),
                            utc_now(),
                        ),
                    )

                real_run = subprocess.run
                commands: list[list[str]] = []

                def recording_run(command, *args, **kwargs):
                    commands.append(list(command))
                    return real_run(command, *args, **kwargs)

                with patch(
                    "codex_team.cluster.subprocess.run",
                    side_effect=recording_run,
                ):
                    manager._reconcile_jobs()

                process.wait(timeout=5)
                self.assertIn(
                    [
                        "taskkill",
                        "/PID",
                        str(identity["pid"]),
                        "/T",
                        "/F",
                    ],
                    commands,
                )
                lost = next(
                    job
                    for job in manager.get_goal(goal["goal_id"])["jobs"]
                    if job["job_id"] == "J-OWNERLESS"
                )
                self.assertEqual(lost["status"], "lost")
                self.assertEqual(lost["outcome"], "lost")
                self.assertIsNotNone(lost["settled_at"])
                self.assertEqual(
                    team.get_task(task["task_id"])["status"],
                    "ready",
                )
                self.assertFalse(pid_is_running(identity["pid"]))
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                manager.close()

    def test_exec_db_finalize_failure_kills_process_and_settles_failed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            manager = ClusterManager(
                settings,
                team=team,
                runtime_mode="exec",
            )
            repo = _repo(base, "exec-db-finalize-failure")
            goal = manager.create_goal(
                repo,
                objective="Clean up after a DB finalize failure",
            )
            task = team.create_task(
                repo,
                title="Fail running-row finalization",
                description="The launched process must not survive.",
                acceptance="The process is killed and the job is failed.",
                kind="read",
                owner_role="team_explorer",
                goal_id=goal["goal_id"],
            )
            real_connection = team.connection
            failed_once = False

            @contextmanager
            def failing_connection(*args, **kwargs):
                nonlocal failed_once
                with real_connection(*args, **kwargs) as connection:
                    class Proxy:
                        def execute(self, sql, parameters=()):
                            nonlocal failed_once
                            if (
                                not failed_once
                                and "SET status = 'running'" in sql
                            ):
                                failed_once = True
                                raise RuntimeError("database finalize failed")
                            return connection.execute(sql, parameters)

                        def __getattr__(self, name):
                            return getattr(connection, name)

                    yield Proxy()

            real_popen = subprocess.Popen
            launched: list[subprocess.Popen] = []
            exec_command = [
                sys.executable,
                "-c",
                "import time; time.sleep(60)",
            ]

            def recording_popen(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                if args and list(args[0]) == exec_command:
                    launched.append(process)
                return process

            try:
                with patch.object(
                    manager,
                    "_build_command",
                    return_value=exec_command,
                ), patch.object(
                    team,
                    "connection",
                    side_effect=failing_connection,
                ), patch(
                    "codex_team.cluster.subprocess.Popen",
                    side_effect=recording_popen,
                ):
                    with self.assertRaisesRegex(
                        ClusterError,
                        "database finalize failed",
                    ):
                        manager._dispatch_worker(
                            manager.get_goal(goal["goal_id"]),
                            team.get_task(task["task_id"]),
                        )

                self.assertEqual(len(launched), 1)
                self.assertFalse(pid_is_running(launched[0].pid))
                self.assertFalse(manager.processes)
                self.assertFalse(manager.process_jobs)
                self.assertFalse(manager.log_threads)
                with team.connection(write=False) as connection:
                    failed = connection.execute(
                        """
                        SELECT status, outcome, error, settled_at
                        FROM cluster_jobs
                        WHERE goal_id = ?
                        ORDER BY rowid DESC
                        LIMIT 1
                        """,
                        (goal["goal_id"],),
                    ).fetchone()
                self.assertIsNotNone(failed)
                self.assertEqual(failed["status"], "failed")
                self.assertEqual(failed["outcome"], "failed")
                self.assertIn("database finalize failed", failed["error"])
                self.assertIsNotNone(failed["settled_at"])
            finally:
                for process in launched:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
                manager.close()

    def test_exec_daemon_restart_closes_tree_and_settles_job_lost(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            manager = ClusterManager(
                settings,
                team=team,
                runtime_mode="exec",
            )
            repo = _repo(base, "exec-daemon-restart")
            goal = manager.create_goal(repo, objective="Lose exec on restart")
            task = team.create_task(
                repo,
                title="Restart owned process",
                description="Closing the daemon must close its Job Object.",
                acceptance="The job is settled lost and the task recovers.",
                kind="read",
                owner_role="team_explorer",
                goal_id=goal["goal_id"],
            )
            with patch.object(
                manager,
                "_build_command",
                return_value=[
                    sys.executable,
                    "-c",
                    "import time; time.sleep(60)",
                ],
            ):
                dispatched = manager._dispatch_worker(
                    manager.get_goal(goal["goal_id"]),
                    team.get_task(task["task_id"]),
                )
            with team.connection(write=False) as connection:
                pid = connection.execute(
                    "SELECT pid FROM cluster_jobs WHERE job_id = ?",
                    (dispatched["job_id"],),
                ).fetchone()["pid"]
            manager.close()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and pid_is_running(int(pid)):
                time.sleep(0.02)
            self.assertFalse(pid_is_running(int(pid)))

            restarted = ClusterManager(
                settings,
                team=team,
                runtime_mode="exec",
            )
            try:
                restarted._reconcile_jobs()
                lost = next(
                    job
                    for job in restarted.get_goal(goal["goal_id"])["jobs"]
                    if job["job_id"] == dispatched["job_id"]
                )
                self.assertEqual(lost["status"], "lost")
                self.assertEqual(lost["outcome"], "lost")
                self.assertIsNotNone(lost["settled_at"])
                self.assertEqual(
                    team.get_task(task["task_id"])["status"],
                    "ready",
                )
            finally:
                restarted.close()

    def test_exec_cancel_terminates_owned_job_without_taskkill(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            manager = ClusterManager(
                settings,
                team=team,
                runtime_mode="exec",
            )
            try:
                repo = _repo(base, "exec-cancel")
                goal = manager.create_goal(repo, objective="Cancel owned process")
                task = team.create_task(
                    repo,
                    title="Cancel owned process",
                    description="The active process is owned by a Job Object.",
                    acceptance="Cancel closes the Job Object.",
                    kind="read",
                    owner_role="team_explorer",
                    goal_id=goal["goal_id"],
                )
                with patch.object(
                    manager,
                    "_build_command",
                    return_value=[
                        sys.executable,
                        "-c",
                        "import time; time.sleep(60)",
                    ],
                ):
                    dispatched = manager._dispatch_worker(
                        manager.get_goal(goal["goal_id"]),
                        team.get_task(task["task_id"]),
                    )
                with team.connection(write=False) as connection:
                    pid = connection.execute(
                        "SELECT pid FROM cluster_jobs WHERE job_id = ?",
                        (dispatched["job_id"],),
                    ).fetchone()["pid"]

                real_run = subprocess.run

                def guarded_run(command, *args, **kwargs):
                    if command and command[0] == "taskkill":
                        self.fail("owned cancel attempted taskkill")
                    return real_run(command, *args, **kwargs)

                with patch(
                    "codex_team.cluster.subprocess.run",
                    side_effect=guarded_run,
                ):
                    canceled = manager.cancel_goal(
                        goal["goal_id"],
                        reason="owned cancel",
                    )
                job = next(
                    item
                    for item in canceled["jobs"]
                    if item["job_id"] == dispatched["job_id"]
                )
                self.assertEqual(job["status"], "interrupted")
                self.assertFalse(pid_is_running(int(pid)))
            finally:
                manager.close()

    def test_cancel_does_not_taskkill_unverified_persisted_pid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = _settings(base)
            team = TeamService(settings)
            manager = ClusterManager(
                settings,
                team=team,
                runtime_mode="exec",
            )
            try:
                repo = _repo(base, "cancel-forged")
                goal = manager.create_goal(repo, objective="Cancel safely")
                task = team.create_task(
                    repo,
                    title="Forged active job",
                    description="The PID belongs to this test process.",
                    acceptance="Cancel must not taskkill an unverified PID.",
                    kind="read",
                    owner_role="team_explorer",
                    goal_id=goal["goal_id"],
                )
                identity = current_process_identity()
                identity["creation_time"] = f"{identity['creation_time']}-old"
                with team.connection() as connection:
                    connection.execute(
                        """
                        INSERT INTO cluster_jobs(
                            job_id, goal_id, task_id, kind, role, status,
                            pid, process_creation_time, process_executable,
                            process_instance_id, attempt, log_path,
                            output_path, started_at
                        ) VALUES (
                            'J-FORGED', ?, ?, 'worker', 'team_explorer',
                            'running', ?, ?, ?, ?, 1, ?, ?, ?
                        )
                        """,
                        (
                            goal["goal_id"],
                            task["task_id"],
                            identity["pid"],
                            identity["creation_time"],
                            identity["executable"],
                            identity["instance_id"],
                            str(base / "forged.log"),
                            str(base / "forged.out"),
                            utc_now(),
                        ),
                    )
                real_run = subprocess.run

                def guarded_run(command, *args, **kwargs):
                    if command and command[0] == "taskkill":
                        self.fail("cancel attempted taskkill for a forged PID")
                    return real_run(command, *args, **kwargs)

                with patch(
                    "codex_team.cluster.subprocess.run",
                    side_effect=guarded_run,
                ):
                    canceled = manager.cancel_goal(
                        goal["goal_id"],
                        reason="forged pid test",
                    )
                forged = next(
                    job
                    for job in canceled["jobs"]
                    if job["job_id"] == "J-FORGED"
                )
                self.assertEqual(forged["status"], "interrupted")
                self.assertTrue(pid_is_running(os.getpid()))
            finally:
                manager.close()


if __name__ == "__main__":
    unittest.main()
