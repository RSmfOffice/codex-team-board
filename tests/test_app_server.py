from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Callable, Mapping
from unittest.mock import patch

from codex_team.app_server import (
    AppServerDisconnectedError,
    AppServerEvents,
    AppServerRPCError,
    AppServerRuntime,
    AppServerTimeoutError,
    TokenUsage,
    map_token_usage,
)


FAKE_SERVER = Path(__file__).with_name("fake_app_server.py")


def wait_for(predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met before timeout")


class EventRecorder(AppServerEvents):
    def __init__(self) -> None:
        self.thread_started: list[tuple[str, Mapping[str, Any]]] = []
        self.thread_statuses: list[tuple[str, Mapping[str, Any]]] = []
        self.thread_closed: list[str] = []
        self.turn_started: list[tuple[str, str, Mapping[str, Any]]] = []
        self.turn_completed: list[tuple[str, str, Mapping[str, Any]]] = []
        self.token_usage: list[tuple[str, str, TokenUsage]] = []
        self.warnings: list[tuple[str, str | None]] = []
        self.errors: list[
            tuple[str, str, Mapping[str, Any], bool]
        ] = []

    def on_thread_started(
        self,
        thread_id: str,
        thread: Mapping[str, Any],
    ) -> None:
        self.thread_started.append((thread_id, thread))

    def on_thread_status_changed(
        self,
        thread_id: str,
        status: Mapping[str, Any],
    ) -> None:
        self.thread_statuses.append((thread_id, status))

    def on_thread_closed(self, thread_id: str) -> None:
        self.thread_closed.append(thread_id)

    def on_turn_started(
        self,
        thread_id: str,
        turn_id: str,
        turn: Mapping[str, Any],
    ) -> None:
        self.turn_started.append((thread_id, turn_id, turn))

    def on_turn_completed(
        self,
        thread_id: str,
        turn_id: str,
        turn: Mapping[str, Any],
    ) -> None:
        self.turn_completed.append((thread_id, turn_id, turn))

    def on_token_usage_updated(
        self,
        thread_id: str,
        turn_id: str,
        usage: TokenUsage,
    ) -> None:
        self.token_usage.append((thread_id, turn_id, usage))

    def on_warning(self, message: str, thread_id: str | None) -> None:
        self.warnings.append((message, thread_id))

    def on_error(
        self,
        thread_id: str,
        turn_id: str,
        error: Mapping[str, Any],
        will_retry: bool,
    ) -> None:
        self.errors.append((thread_id, turn_id, error, will_retry))


class AppServerRuntimeTests(unittest.TestCase):
    def _temporary_base(self) -> Path:
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        return Path(temporary)

    def _runtime(
        self,
        base: Path,
        *,
        events: AppServerEvents | None = None,
        request_timeout: float = 1.0,
        extra_env: Mapping[str, str] | None = None,
    ) -> AppServerRuntime:
        env = {
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "FAKE_APP_SERVER_TRACE": str(base / "trace.jsonl"),
        }
        if extra_env:
            env.update(extra_env)
        runtime = AppServerRuntime(
            [sys.executable, str(FAKE_SERVER)],
            request_timeout=request_timeout,
            startup_timeout=2.0,
            stop_timeout=2.0,
            stderr_log_path=base / "app-server.stderr.log",
            event_handler=events,
            env=env,
        )
        self.addCleanup(runtime.stop)
        return runtime

    def test_default_command_is_private_stdio_app_server(self) -> None:
        with patch.dict(
            os.environ,
            {"CODEX_TEAM_CODEX_COMMAND": ""},
            clear=False,
        ):
            with patch(
                "codex_team.app_server.shutil.which",
                side_effect=lambda name: (
                    r"C:\Tools\codex.exe" if name == "codex.exe" else None
                ),
            ):
                runtime = AppServerRuntime()
        self.assertEqual(
            runtime.command,
            [
                r"C:\Tools\codex.exe",
                "app-server",
                "--listen",
                "stdio://",
            ],
        )

    def test_jsonl_flow_maps_events_and_token_usage(self) -> None:
        base = self._temporary_base()
        events = EventRecorder()
        runtime = self._runtime(
            base,
            events=events,
            extra_env={"FAKE_APP_SERVER_STDERR": "fake stderr marker"},
        )
        workspace = base / "workspace"
        workspace.mkdir()

        thread_id = runtime.start_thread(
            workspace,
            developer_instructions="Follow the task contract.",
            model="test-model",
            model_provider="test-provider",
            service_name="codex-team-test",
        )
        self.assertEqual(thread_id, "thread-1")
        wait_for(lambda: bool(events.thread_started))
        self.assertEqual(events.thread_started[0][0], thread_id)

        self.assertEqual(runtime.list_loaded_threads(), ["thread-1"])
        resumed_id = runtime.resume_thread("thread-resumed")
        self.assertEqual(resumed_id, "thread-resumed")
        wait_for(
            lambda: any(
                item[0] == "thread-resumed" for item in events.thread_started
            )
        )

        turn = runtime.start_turn(
            "thread-resumed",
            "Run the tests",
            cwd=workspace,
            sandbox_mode="workspace-write",
            model="turn-model",
            effort="high",
        )
        self.assertEqual(turn["turn_id"], "turn-2")
        self.assertEqual(turn["status"], "inProgress")
        wait_for(
            lambda: any(
                item[1] == turn["turn_id"] for item in events.turn_completed
            )
        )
        wait_for(
            lambda: any(
                item[1] == turn["turn_id"] for item in events.token_usage
            )
        )
        usage = next(
            item[2]
            for item in events.token_usage
            if item[1] == turn["turn_id"]
        )
        self.assertEqual(
            usage,
            {
                "total": {
                    "input": 120,
                    "cached": 20,
                    "uncached": 100,
                    "output": 30,
                    "context": 150,
                },
                "last": {
                    "input": 50,
                    "cached": 5,
                    "uncached": 45,
                    "output": 10,
                    "context": 60,
                },
                "modelContextWindow": 200,
            },
        )
        self.assertEqual(events.warnings[0], ("fake warning", "thread-resumed"))
        self.assertFalse(events.errors[0][3])
        self.assertEqual(
            events.errors[0][2]["message"],
            "fake nonfatal error",
        )
        self.assertTrue(
            any(
                thread_id == "thread-resumed"
                and status["type"] == "active"
                for thread_id, status in events.thread_statuses
            )
        )

        overflow_turn = runtime.start_turn(
            "thread-resumed",
            "usage-overflow",
        )
        wait_for(
            lambda: any(
                item[1] == overflow_turn["turn_id"]
                for item in events.token_usage
            )
        )
        overflow_usage = next(
            item[2]
            for item in events.token_usage
            if item[1] == overflow_turn["turn_id"]
        )
        self.assertEqual(overflow_usage["total"]["uncached"], 0)
        self.assertEqual(overflow_usage["last"]["uncached"], 0)

        runtime.interrupt_turn("thread-resumed", turn["turn_id"])
        wait_for(
            lambda: any(
                item[2].get("status") == "interrupted"
                for item in events.turn_completed
            )
        )
        self.assertEqual(
            runtime.unsubscribe_thread("thread-resumed"),
            "unsubscribed",
        )
        wait_for(lambda: "thread-resumed" in events.thread_closed)
        self.assertEqual(runtime.list_loaded_threads(), ["thread-1"])

        messages = [
            json.loads(line)
            for line in (base / "trace.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(messages[0]["method"], "initialize")
        self.assertNotIn("jsonrpc", messages[0])
        self.assertEqual(messages[1], {"method": "initialized", "params": {}})
        start_request = next(
            message
            for message in messages
            if message.get("method") == "thread/start"
        )
        self.assertEqual(
            start_request["params"],
            {
                "cwd": str(workspace),
                "developerInstructions": "Follow the task contract.",
                "model": "test-model",
                "modelProvider": "test-provider",
                "serviceName": "codex-team-test",
            },
        )
        turn_request = next(
            message
            for message in messages
            if message.get("method") == "turn/start"
        )
        self.assertEqual(
            turn_request["params"],
            {
                "threadId": "thread-resumed",
                "input": [{"type": "text", "text": "Run the tests"}],
                "cwd": str(workspace),
                "sandboxPolicy": {
                    "type": "workspaceWrite",
                    "writableRoots": [str(workspace)],
                },
                "model": "turn-model",
                "effort": "high",
            },
        )

        wait_for(
            lambda: "fake stderr marker"
            in (base / "app-server.stderr.log").read_text(
                encoding="utf-8",
                errors="replace",
            )
            if (base / "app-server.stderr.log").exists()
            else False
        )
        self.assertIn("fake stderr marker", runtime.stderr_lines)
        self.assertTrue(runtime.health()["healthy"])
        runtime.stop()
        self.assertFalse(runtime.health()["running"])

    def test_request_timeout_and_jsonrpc_error_are_actionable(self) -> None:
        base = self._temporary_base()
        runtime = self._runtime(base, request_timeout=0.15)
        with self.assertRaises(AppServerTimeoutError):
            runtime.resume_thread("hang")
        with self.assertRaises(AppServerRPCError) as raised:
            runtime.resume_thread("missing")
        self.assertEqual(raised.exception.code, -32602)
        self.assertEqual(raised.exception.data, {"type": "notFound"})
        self.assertTrue(runtime.health()["healthy"])

    def test_stderr_log_redacts_credentials(self) -> None:
        base = self._temporary_base()
        secret = "claim-secret-value"
        api_secret = "api-secret-value"
        runtime = self._runtime(
            base,
            extra_env={
                "FAKE_APP_SERVER_STDERR": (
                    f"diagnostic marker claim_token={secret} "
                    f"OPENAI_API_KEY={api_secret}"
                ),
            },
        )
        runtime.start()
        log_path = base / "app-server.stderr.log"
        wait_for(
            lambda: log_path.exists()
            and "diagnostic marker" in log_path.read_text(
                encoding="utf-8",
                errors="replace",
            )
        )
        logged = log_path.read_text(encoding="utf-8", errors="replace")
        self.assertIn("[REDACTED]", logged)
        self.assertNotIn(secret, logged)
        self.assertNotIn(api_secret, logged)

    def test_concurrent_requests_are_correlated_by_jsonrpc_id(self) -> None:
        base = self._temporary_base()
        runtime = self._runtime(base, request_timeout=1.0)
        runtime.start()
        barrier = threading.Barrier(3)
        results: dict[str, str] = {}
        failures: list[BaseException] = []

        def resume(thread_id: str) -> None:
            try:
                barrier.wait(timeout=2.0)
                results[thread_id] = runtime.resume_thread(thread_id)
            except BaseException as exc:
                failures.append(exc)

        slow = threading.Thread(target=resume, args=("slow-thread",))
        fast = threading.Thread(target=resume, args=("fast-thread",))
        slow.start()
        fast.start()
        barrier.wait(timeout=2.0)
        slow.join(timeout=2.0)
        fast.join(timeout=2.0)
        self.assertEqual(failures, [])
        self.assertEqual(
            results,
            {
                "slow-thread": "slow-thread",
                "fast-thread": "fast-thread",
            },
        )

    def test_disconnect_restart_resume_and_stop(self) -> None:
        base = self._temporary_base()
        crash_marker = base / "crashed-once"
        runtime = self._runtime(
            base,
            request_timeout=1.0,
            extra_env={"FAKE_APP_SERVER_CRASH_ONCE": str(crash_marker)},
        )
        runtime.start_thread(base)
        first_generation = runtime.generation
        with self.assertRaises(AppServerDisconnectedError):
            runtime.resume_thread("crash")
        wait_for(lambda: not runtime.health()["running"])

        health = runtime.restart()
        self.assertTrue(health["healthy"])
        self.assertEqual(health["restart_count"], 1)
        self.assertGreater(health["generation"], first_generation)
        self.assertEqual(runtime.resume_thread("crash"), "crash")
        self.assertIn("crash", runtime.list_loaded_threads())
        runtime.stop()
        self.assertFalse(runtime.health()["running"])
        runtime.stop()

    def test_token_mapping_is_exact_and_clamps_uncached(self) -> None:
        usage = map_token_usage(
            {
                "total": {
                    "inputTokens": 3,
                    "cachedInputTokens": 8,
                    "outputTokens": 2,
                },
                "last": {"totalTokens": 5},
                "modelContextWindow": None,
            }
        )
        self.assertEqual(
            usage,
            {
                "total": {
                    "input": 3,
                    "cached": 3,
                    "uncached": 0,
                    "output": 2,
                    "context": 0,
                },
                "last": {
                    "input": 0,
                    "cached": 0,
                    "uncached": 0,
                    "output": 0,
                    "context": 5,
                },
                "modelContextWindow": None,
            },
        )
        missing_last = map_token_usage(
            {
                "total": {
                    "inputTokens": 12,
                    "cachedInputTokens": 4,
                    "outputTokens": 3,
                    "totalTokens": 15,
                }
            }
        )
        self.assertIsNone(missing_last["last"])
        self.assertEqual(missing_last["total"]["uncached"], 8)


if __name__ == "__main__":
    unittest.main()
