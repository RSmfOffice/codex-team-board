from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import ExitStack, nullcontext
from pathlib import Path
from unittest.mock import patch

from codex_team import clusterd
from codex_team.cluster import ClusterError, ClusterManager
from codex_team.config import Settings
from codex_team.dashboard import build_server
from codex_team.db import SCHEMA_VERSION, utc_now
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


class DashboardClusterControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        git(self.repo, "init")
        git(self.repo, "config", "user.email", "dashboard-test@example.invalid")
        git(self.repo, "config", "user.name", "Dashboard Test")
        (self.repo / "README.md").write_text("# Dashboard test\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "initial")
        self.settings = Settings(
            project_root=Path(__file__).resolve().parents[1],
            home=self.base / "team-home",
            db_path=self.base / "team-home" / "team.sqlite",
            logs_dir=self.base / "team-home" / "logs",
        )
        self.team = TeamService(self.settings)
        self.project = self.team.init_project(self.repo)
        git(self.repo, "add", ".codex", "AGENTS.md")
        git(self.repo, "commit", "-m", "initialize team policy")
        self.server = build_server(self.settings, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def post(
        self,
        path: str,
        payload: object,
        *,
        content_type: str = "application/json",
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, object]]:
        headers = {"Content-Type": content_type}
        headers.update(extra_headers or {})
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode("utf-8"))
            finally:
                exc.close()

    def test_write_requests_require_loopback_same_origin_json(self) -> None:
        payload = {
            "project_id": self.project["project_id"],
            "objective": "Rejected unless local JSON",
        }
        status, body = self.post(
            "/api/clusters",
            payload,
            extra_headers={"Host": "attacker.example"},
        )
        self.assertEqual(status, 403)
        self.assertIn("loopback Host", body["error"])

        status, body = self.post(
            "/api/clusters",
            payload,
            extra_headers={"Origin": "https://attacker.example"},
        )
        self.assertEqual(status, 403)
        self.assertIn("same-origin", body["error"])

        status, body = self.post(
            "/api/clusters",
            payload,
            content_type="text/plain",
        )
        self.assertEqual(status, 415)
        self.assertIn("application/json", body["error"])
        with self.team.connection(write=False) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM cluster_goals"
                ).fetchone()[0],
                0,
            )

        with patch(
            "codex_team.dashboard.ensure_daemon",
            return_value={"running": True, "pid": 1234},
        ):
            status, body = self.post(
                "/api/clusters",
                payload,
                extra_headers={"Origin": self.base_url},
            )
        self.assertEqual(status, 200)
        self.assertEqual(body["goal"]["status"], "planning")

    def get(self, path: str) -> dict[str, object]:
        with urllib.request.urlopen(self.base_url + path, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_create_cluster_goal_and_start_daemon(self) -> None:
        daemon = {"running": True, "pid": 1234}
        with patch(
            "codex_team.dashboard.ensure_daemon",
            return_value=daemon,
        ) as start_daemon:
            status, body = self.post(
                "/api/clusters",
                {
                    "project_id": self.project["project_id"],
                    "objective": "Build the release",
                    "autonomy": "supervised",
                    "max_worker_attempts": 4,
                    "max_lead_attempts": 6,
                },
            )

        self.assertEqual(status, 200)
        self.assertEqual(body["daemon"], daemon)
        goal = body["goal"]
        self.assertEqual(goal["project_id"], self.project["project_id"])
        self.assertEqual(goal["objective"], "Build the release")
        self.assertEqual(goal["autonomy"], "supervised")
        self.assertEqual(goal["max_worker_attempts"], 4)
        self.assertEqual(goal["max_lead_attempts"], 6)
        start_daemon.assert_called_once_with(self.settings)

    def test_dashboard_exposes_default_thread_reuse_runtime(self) -> None:
        connection = sqlite3.connect(self.settings.db_path)
        try:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0],
                SCHEMA_VERSION,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0],
                SCHEMA_VERSION,
            )
            connection.execute("PRAGMA user_version = 7")
            connection.execute(
                "UPDATE metadata SET value = '7' WHERE key = 'schema_version'"
            )
            connection.commit()
        finally:
            connection.close()
        with patch.dict(os.environ, {"CODEX_TEAM_AGENT_RUNTIME": ""}):
            health = self.get("/api/health")
            board = self.get("/api/board")

        self.assertEqual(health["schema_version"], SCHEMA_VERSION)
        self.assertEqual(
            health["cluster_runtime"],
            {
                "runtime": "app_server",
                "thread_reuse": True,
                "running": False,
                "config_mismatch": False,
                "version": "0.5.0",
            },
        )
        self.assertEqual(board["cluster_runtime"], health["cluster_runtime"])

    def test_dashboard_exposes_degraded_tick_phase_health(self) -> None:
        daemon = {
            "running": True,
            "config_mismatch": False,
            "expected_identity": {
                "runtime": "app_server",
                "version": "0.5.0",
            },
            "state": {
                "ok": False,
                "tick_id": "TICK-DEGRADED",
                "tick_at": "2026-11-11T07:00:00Z",
                "degraded": True,
                "failed_phase": "settle",
                "failed_phases": ["settle"],
                "degraded_phases": ["settle", "health"],
                "skipped_phases": ["dispatch"],
                "error_code": "settle_phase_failure",
                "error": "database unavailable",
                "phases": [
                    {
                        "name": "settle",
                        "ok": False,
                        "degraded": True,
                        "attempts": 2,
                        "duration_ms": 1.25,
                        "error_code": "settle_phase_failure",
                        "redacted_error": "database unavailable",
                        "retry_suppressed": False,
                    }
                ],
                "retry": {
                    "limit": 4,
                    "used": 1,
                    "exhausted": False,
                },
                "health": {
                    "ok": False,
                    "degraded": True,
                    "failed_phases": ["settle"],
                    "degraded_phases": ["settle", "health"],
                    "skipped_phases": ["dispatch"],
                    "errors": [
                        {
                            "phase": "settle",
                            "error_code": "settle_phase_failure",
                            "redacted_error": "database unavailable",
                        }
                    ],
                },
            },
        }
        with patch(
            "codex_team.dashboard.daemon_status",
            return_value=daemon,
        ):
            health = self.get("/api/health")
            board = self.get("/api/board")

        runtime = health["cluster_runtime"]
        self.assertEqual(board["cluster_runtime"], runtime)
        tick = runtime["tick"]
        self.assertTrue(tick["degraded"])
        self.assertEqual(tick["failed_phase"], "settle")
        self.assertEqual(
            tick["degraded_phases"],
            ["settle", "health"],
        )
        self.assertEqual(tick["skipped_phases"], ["dispatch"])
        self.assertEqual(tick["error_code"], "settle_phase_failure")
        self.assertEqual(tick["phases"][0]["attempts"], 2)
        self.assertEqual(tick["retry"]["used"], 1)
        self.assertTrue(tick["health"]["degraded"])

    def test_tick_only_changes_do_not_break_conditional_304(self) -> None:
        def daemon_state(
            tick_id: str,
            tick_at: str,
            duration_ms: float,
            *,
            degraded: bool = False,
        ) -> dict[str, object]:
            failed_phases = ["settle"] if degraded else []
            error_code = "settle_phase_failure" if degraded else ""
            phase = {
                "name": "settle",
                "ok": not degraded,
                "degraded": degraded,
                "attempts": 1,
                "duration_ms": duration_ms,
                "error_code": error_code,
                "redacted_error": "database unavailable" if degraded else "",
                "retry_suppressed": False,
            }
            health = {
                "ok": not degraded,
                "degraded": degraded,
                "failed_phases": failed_phases,
                "degraded_phases": failed_phases,
                "skipped_phases": ["dispatch"] if degraded else [],
                "errors": (
                    [
                        {
                            "phase": "settle",
                            "source": "settle",
                            "error_code": error_code,
                            "redacted_error": "database unavailable",
                        }
                    ]
                    if degraded
                    else []
                ),
            }
            return {
                "running": True,
                "config_mismatch": False,
                "expected_identity": {
                    "runtime": "app_server",
                    "version": "0.5.0",
                },
                "state": {
                    "ok": not degraded,
                    "tick_id": tick_id,
                    "tick_at": tick_at,
                    "degraded": degraded,
                    "failed_phase": (
                        "settle" if degraded else None
                    ),
                    "failed_phases": failed_phases,
                    "degraded_phases": failed_phases,
                    "skipped_phases": (
                        ["dispatch"] if degraded else []
                    ),
                    "error_code": error_code,
                    "error": (
                        "database unavailable" if degraded else ""
                    ),
                    "phases": [phase],
                    "retry": {
                        "limit": 4,
                        "used": 0,
                        "exhausted": False,
                    },
                    "health": health,
                },
            }

        def conditional_status(
            path: str,
            etag: str,
        ) -> tuple[int, str, dict[str, object]]:
            request = urllib.request.Request(
                self.base_url + path,
                headers={"If-None-Match": etag},
            )
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    return (
                        response.status,
                        response.headers.get("ETag", ""),
                        json.loads(response.read().decode("utf-8")),
                    )
            except urllib.error.HTTPError as exc:
                try:
                    return (
                        exc.code,
                        exc.headers.get("ETag", ""),
                        {},
                    )
                finally:
                    exc.close()

        def response_etag(path: str) -> str:
            with urllib.request.urlopen(
                self.base_url + path,
                timeout=5,
            ) as response:
                return response.headers.get("ETag", "")

        first = daemon_state(
            "TICK-A",
            "2026-11-11T07:00:00Z",
            1.0,
        )
        second = daemon_state(
            "TICK-B",
            "2026-11-11T07:00:05Z",
            9.0,
        )
        semantic = daemon_state(
            "TICK-C",
            "2026-11-11T07:00:10Z",
            2.0,
            degraded=True,
        )
        with patch(
            "codex_team.dashboard.daemon_status",
            return_value=first,
        ):
            summary = self.get("/api/summary")
            board = self.get("/api/board")
            summary_etag = response_etag("/api/summary")
            board_etag = response_etag("/api/board")
        with patch(
            "codex_team.dashboard.daemon_status",
            return_value=second,
        ):
            self.assertEqual(
                conditional_status("/api/summary", summary_etag)[0],
                304,
            )
            self.assertEqual(
                conditional_status("/api/board", board_etag)[0],
                304,
            )
            tick_only_summary = self.get("/api/summary")
            self.assertEqual(
                tick_only_summary["revision"],
                summary["revision"],
            )
            self.assertEqual(
                tick_only_summary["cluster_runtime"]["tick"]["tick_id"],
                "TICK-B",
            )
            self.assertEqual(board["revision"], summary["revision"])

        with patch(
            "codex_team.dashboard.daemon_status",
            return_value=semantic,
        ):
            status, changed_etag, _ = conditional_status(
                "/api/summary",
                summary_etag,
            )
            self.assertEqual(status, 200)
            self.assertNotEqual(changed_etag, summary_etag)
            changed = self.get("/api/summary")
            self.assertTrue(changed["cluster_runtime"]["tick"]["degraded"])

    def test_degraded_tick_round_trips_from_manager_to_dashboard(
        self,
    ) -> None:
        manager = ClusterManager(
            self.settings,
            team=self.team,
            runtime_mode="exec",
            agent_command=["codex"],
        )
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    manager,
                    "_reconcile_jobs",
                    side_effect=RuntimeError(
                        "claim_token=integration-secret"
                    ),
                )
            )
            stack.enter_context(
                patch.object(
                    manager.worktrees,
                    "recover_pending",
                    return_value=[],
                )
            )
            stack.enter_context(
                patch.object(
                    manager,
                    "_apply_agent_reset_requests",
                    return_value={"count": 0},
                )
            )
            stack.enter_context(
                patch.object(
                    manager,
                    "_sync_project_agent_archive",
                    return_value={"count": 0},
                )
            )
            stack.enter_context(
                patch.object(manager, "list_goals", return_value=[])
            )
            stack.enter_context(
                patch.object(manager, "_schedule_goals", return_value=[])
            )
            stack.enter_context(
                patch.object(
                    manager,
                    "_check_worker_health",
                    return_value={"count": 0},
                )
            )
            stack.enter_context(
                patch.object(
                    manager,
                    "_check_goal_stalls",
                    return_value={"count": 0},
                )
            )
            stack.enter_context(patch("codex_team.cluster.time.sleep"))
            stack.enter_context(
                patch.object(Settings, "load", return_value=self.settings)
            )
            stack.enter_context(
                patch.object(
                    clusterd,
                    "ClusterManager",
                    return_value=manager,
                )
            )
            stack.enter_context(
                patch.object(
                    clusterd,
                    "acquire_lock",
                    return_value=nullcontext(),
                )
            )
            exit_code = clusterd.run(once=True)

        health = self.get("/api/health")
        tick = health["cluster_runtime"]["tick"]
        serialized = json.dumps(tick, ensure_ascii=False)
        self.assertEqual(exit_code, 1)
        self.assertTrue(tick["degraded"])
        self.assertEqual(tick["failed_phase"], "settle")
        self.assertEqual(tick["error_code"], "settle_phase_failure")
        self.assertEqual(tick["phases"][0]["attempts"], 2)
        self.assertIn("dispatch", tick["skipped_phases"])
        self.assertNotIn("integration-secret", serialized)
        self.assertIn("[REDACTED]", serialized)

    def test_sync_native_agents_is_idempotent_and_preserves_failed(self) -> None:
        task = self.team.create_task(
            self.repo,
            title="Native sync task",
            description="Bind a discovered native session.",
            acceptance="Native session is visible.",
            kind="read",
            owner_role="team_explorer",
        )
        with self.team.connection() as connection:
            connection.execute(
                """
                INSERT INTO cluster_agents(
                    agent_id, project_id, role, workstream_key, display_name,
                    thread_id, runtime, status, created_at, updated_at
                ) VALUES (?, ?, 'team_worker', 'legacy-failed',
                          'Legacy Failed Worker',
                          '01a1cafe-9999-7222-8333-944455556666',
                          'managed', 'failed', ?, ?)
                """,
                (
                    "A-LEGACY-FAILED",
                    self.project["project_id"],
                    utc_now(),
                    utc_now(),
                ),
            )

        sessions_root = self.base / "codex-sessions"
        sessions_root.mkdir()
        thread_id = "01a1cafe-1111-7222-8333-944455556666"
        session_path = sessions_root / f"rollout-{thread_id}.jsonl"
        session_path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "session_meta",
                            "payload": {
                                "id": thread_id,
                                "cwd": str(self.repo),
                                "model_provider": "deepseek",
                                "source": {
                                    "subagent": {
                                        "thread_spawn": {
                                            "parent_thread_id": "parent-123",
                                            "agent_path": "/root/cache_prefix_review",
                                            "agent_nickname": "Hume",
                                            "agent_role": None,
                                        }
                                    }
                                },
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
                                        "text": f"Review {task['task_id']}",
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
        unknown_thread_id = "01a1cafe-2222-7222-8333-944455556666"
        (sessions_root / f"rollout-{unknown_thread_id}.jsonl").write_text(
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {
                        "id": unknown_thread_id,
                        "cwd": str(self.base / "unknown-project"),
                        "source": {
                            "subagent": {
                                "thread_spawn": {
                                    "parent_thread_id": "parent-456",
                                    "agent_path": "/root/unknown",
                                    "agent_role": "team_explorer",
                                }
                            }
                        },
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (sessions_root / "rollout-main-thread.jsonl").write_text(
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {
                        "id": "01a1cafe-3333-7222-8333-944455556666",
                        "cwd": str(self.repo),
                        "source": "vscode",
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        with patch.dict(
            os.environ,
            {"CODEX_NATIVE_SESSIONS_ROOT": str(sessions_root)},
        ):
            status, first = self.post(
                "/api/agents/sync-native",
                {"limit": 100},
            )
            self.assertEqual(status, 200)
            self.assertEqual(first["registered_count"], 1)
            self.assertEqual(first["registered"][0]["thread_id"], thread_id)
            self.assertIn(
                unknown_thread_id,
                first["skipped_unknown_project"],
            )
            self.assertEqual(
                first["registered"][0]["current_task_id"],
                task["task_id"],
            )

            board = self.get("/api/board")
            agent_ids = {
                agent["agent_id"] for agent in board["cluster_agents"]
            }
            self.assertIn("A-LEGACY-FAILED", agent_ids)
            native = next(
                agent
                for agent in board["cluster_agents"]
                if agent["thread_id"] == thread_id
            )
            self.assertEqual(native["runtime"], "native")
            self.assertEqual(native["role"], "team_reviewer")
            self.assertEqual(
                native["identity_source"],
                "agent_path_inference",
            )
            self.assertEqual(native["identity_confidence"], "low")
            self.assertEqual(native["lifecycle"], "active")
            self.assertFalse(native["session_stale"])
            self.assertEqual(
                native["assignment_source"],
                "unique_session_candidate",
            )
            self.assertEqual(
                native["task_candidates"],
                [task["task_id"]],
            )
            self.assertFalse(native["task_assignment_ambiguous"])
            self.assertEqual(native["prompt_status"], "delivered")
            self.assertEqual(native["current_task_id"], task["task_id"])
            self.assertFalse(native["usage_available"])
            self.assertIsNone(native["last_input_tokens"])
            self.assertIsNone(native["last_cached_input_tokens"])
            self.assertIsNone(native["last_uncached_input_tokens"])
            self.assertIsNone(native["context_tokens"])
            self.assertIsNone(native["turn_count"])
            self.assertEqual(
                native["native_usage_status"],
                "unavailable",
            )

            with session_path.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "token_count",
                                "info": {
                                    "total_token_usage": {
                                        "input_tokens": 2000,
                                        "cached_input_tokens": 1600,
                                        "output_tokens": 100,
                                    },
                                    "last_token_usage": {
                                        "input_tokens": 1000,
                                        "cached_input_tokens": 800,
                                        "output_tokens": 50,
                                    },
                                    "model_context_window": 996147,
                                },
                            },
                        }
                    )
                    + "\n"
                )
            os.utime(session_path, None)

            status, second = self.post(
                "/api/agents/sync-native",
                {"limit": 100},
            )
            self.assertEqual(status, 200)
            self.assertEqual(second["registered_count"], 0)
            self.assertEqual(second["refreshed_count"], 1)
            usage_board = self.get("/api/board")
            usage_agent = next(
                agent
                for agent in usage_board["cluster_agents"]
                if agent["thread_id"] == thread_id
            )
            self.assertTrue(usage_agent["usage_available"])
            self.assertEqual(usage_agent["last_input_tokens"], 1000)
            self.assertEqual(usage_agent["last_cached_input_tokens"], 800)
            self.assertEqual(usage_agent["last_uncached_input_tokens"], 200)
            self.assertEqual(usage_agent["context_tokens"], 1000)
            self.assertEqual(usage_agent["model_context_window"], 996147)
            self.assertEqual(
                usage_agent["native_usage_status"],
                "partial",
            )
            self.assertIsNone(usage_agent["turn_count"])
            self.assertIn(
                "turn_count",
                usage_agent["native_usage_missing_fields"],
            )
            self.assertAlmostEqual(usage_agent["cache_hit_rate"], 0.8)

            status, third = self.post(
                "/api/agents/sync-native",
                {"limit": 100},
            )
            self.assertEqual(status, 200)
            self.assertEqual(third["registered_count"], 0)
            self.assertEqual(third["refreshed_count"], 0)
            self.assertIn(thread_id, third["skipped_existing"])
            after = self.get("/api/board")
            self.assertEqual(
                len(after["cluster_agents"]),
                len(board["cluster_agents"]),
            )
            self.assertIn(
                "A-LEGACY-FAILED",
                {
                    agent["agent_id"]
                    for agent in after["cluster_agents"]
                },
            )

        unknown_thread_id = "01a1cafe-5555-7222-8333-944455556666"
        self.team.agent_pool.register_native_thread(
            str(self.project["project_id"]),
            "team_explorer",
            "native:unknown-usage",
            unknown_thread_id,
            "native:unknown-usage",
        )
        unknown_board = self.get("/api/board")
        unknown_usage_agent = next(
            agent
            for agent in unknown_board["cluster_agents"]
            if agent["thread_id"] == unknown_thread_id
        )
        self.assertFalse(unknown_usage_agent["usage_available"])
        self.assertIsNone(unknown_usage_agent["last_input_tokens"])
        self.assertIsNone(unknown_usage_agent["last_cached_input_tokens"])
        self.assertIsNone(unknown_usage_agent["last_uncached_input_tokens"])
        self.assertIsNone(unknown_usage_agent["context_tokens"])

    def test_sync_native_refreshes_usage_on_same_second_mtime_change(
        self,
    ) -> None:
        sessions_root = self.base / "same-second-native-sessions"
        sessions_root.mkdir()
        thread_id = "01a1cafe-8787-7222-8333-944455556666"
        session_path = sessions_root / f"rollout-{thread_id}.jsonl"

        def write_usage(input_tokens: int) -> None:
            session_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "session_meta",
                                "payload": {
                                    "id": thread_id,
                                    "cwd": str(self.repo),
                                    "source": {
                                        "subagent": {
                                            "thread_spawn": {
                                                "parent_thread_id": "parent-same-second",
                                                "agent_role": "team_explorer",
                                            }
                                        }
                                    },
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "event_msg",
                                "payload": {
                                    "type": "token_count",
                                    "info": {
                                        "total_token_usage": {
                                            "input_tokens": input_tokens,
                                            "cached_input_tokens": int(
                                                input_tokens * 0.8
                                            ),
                                            "output_tokens": 50,
                                        },
                                        "last_token_usage": {
                                            "input_tokens": input_tokens,
                                            "cached_input_tokens": int(
                                                input_tokens * 0.8
                                            ),
                                            "output_tokens": 50,
                                        },
                                        "model_context_window": 996147,
                                    },
                                },
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

        same_second = (
            time.time_ns() // 1_000_000_000
        ) * 1_000_000_000 + 500_000_000
        write_usage(100)
        os.utime(session_path, ns=(same_second, same_second))
        with patch.dict(
            os.environ,
            {"CODEX_NATIVE_SESSIONS_ROOT": str(sessions_root)},
        ):
            status, first = self.post(
                "/api/agents/sync-native",
                {"limit": 100},
            )
            self.assertEqual(status, 200)
            self.assertEqual(first["registered_count"], 1)
            first_agent = next(
                agent
                for agent in self.get("/api/board")["cluster_agents"]
                if agent["thread_id"] == thread_id
            )
            self.assertEqual(first_agent["last_input_tokens"], 100)

            write_usage(200)
            os.utime(
                session_path,
                ns=(same_second, same_second + 1_000),
            )
            status, second = self.post(
                "/api/agents/sync-native",
                {"limit": 100},
            )
            self.assertEqual(status, 200)
            self.assertEqual(second["refreshed_count"], 1)
            refreshed_agent = next(
                agent
                for agent in self.get("/api/board")["cluster_agents"]
                if agent["thread_id"] == thread_id
            )
            self.assertEqual(refreshed_agent["last_input_tokens"], 200)
            self.assertEqual(refreshed_agent["last_cached_input_tokens"], 160)

    def test_sync_refreshes_registered_native_outside_window(self) -> None:
        sessions_root = self.base / "old-native-sessions"
        sessions_root.mkdir()
        thread_id = "01a1cafe-6666-7222-8333-944455556666"
        session_path = sessions_root / f"rollout-{thread_id}.jsonl"
        session_path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "session_meta",
                            "payload": {
                                "id": thread_id,
                                "cwd": str(self.repo),
                                "source": {
                                    "subagent": {
                                        "thread_spawn": {
                                            "parent_thread_id": "parent-old",
                                            "agent_path": "/root/old-reviewer",
                                        }
                                    }
                                },
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "token_count",
                                "info": {
                                    "total_token_usage": {
                                        "input_tokens": 2000,
                                        "cached_input_tokens": 1600,
                                        "output_tokens": 100,
                                    },
                                    "last_token_usage": {
                                        "input_tokens": 1000,
                                        "cached_input_tokens": 800,
                                        "output_tokens": 50,
                                    },
                                    "model_context_window": 996147,
                                },
                            },
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        old_timestamp = time.time() - 2 * 3600
        os.utime(session_path, (old_timestamp, old_timestamp))
        self.team.agent_pool.register_native_thread(
            str(self.project["project_id"]),
            "team_worker",
            "native:parent-old",
            thread_id,
            f"native:{thread_id}",
        )

        with patch.dict(
            os.environ,
            {
                "CODEX_NATIVE_SESSIONS_ROOT": str(sessions_root),
                "CODEX_TEAM_NATIVE_DISCOVERY_MAX_AGE_HOURS": "1",
            },
        ):
            status, first = self.post(
                "/api/agents/sync-native",
                {"limit": 100},
            )
            self.assertEqual(status, 200)
            self.assertEqual(first["discovered"], 0)
            self.assertEqual(first["registered_count"], 0)
            self.assertEqual(first["refreshed_count"], 1)
            board = self.get("/api/board")
            agent = next(
                item
                for item in board["cluster_agents"]
                if item["thread_id"] == thread_id
            )
            self.assertTrue(agent["usage_available"])
            self.assertEqual(agent["context_tokens"], 1000)
            self.assertEqual(agent["model_context_window"], 996147)
            self.assertEqual(agent["role"], "team_worker")
            self.assertEqual(agent["identity_source"], "registry")
            self.assertEqual(agent["identity_confidence"], "medium")

            status, second = self.post(
                "/api/agents/sync-native",
                {"limit": 100},
            )
            self.assertEqual(status, 200)
            self.assertEqual(second["refreshed_count"], 0)

    def test_native_sync_preserves_ambiguous_candidates_and_stales_usage(
        self,
    ) -> None:
        first_task = self.team.create_task(
            self.repo,
            title="Native candidate one",
            description="Referenced from a native prompt.",
            acceptance="Remains a candidate.",
            kind="read",
            owner_role="team_explorer",
        )
        second_task = self.team.create_task(
            self.repo,
            title="Native candidate two",
            description="Also referenced from the same prompt.",
            acceptance="Remains a candidate.",
            kind="read",
            owner_role="team_explorer",
        )
        sessions_root = self.base / "ambiguous-native-sessions"
        sessions_root.mkdir()
        thread_id = "01a1cafe-7777-7222-8333-944455556666"
        session_path = sessions_root / f"rollout-{thread_id}.jsonl"
        session_path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "session_meta",
                            "payload": {
                                "id": thread_id,
                                "cwd": str(self.repo),
                                "source": {
                                    "subagent": {
                                        "thread_spawn": {
                                            "agent_path": "/root/unclassified"
                                        }
                                    }
                                },
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
                                    f"Compare {first_task['task_id']} with "
                                    f"{second_task['task_id']}"
                                ),
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "token_count",
                                "info": {
                                    "last_token_usage": {
                                        "input_tokens": 100,
                                        "cached_input_tokens": 80,
                                        "output_tokens": 5,
                                    },
                                    "total_token_usage": {
                                        "input_tokens": 200,
                                        "cached_input_tokens": 160,
                                        "output_tokens": 10,
                                    },
                                    "model_context_window": 1000,
                                },
                            },
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        with patch.dict(
            os.environ,
            {"CODEX_NATIVE_SESSIONS_ROOT": str(sessions_root)},
        ):
            status, first = self.post(
                "/api/agents/sync-native",
                {"limit": 100},
            )
            self.assertEqual(status, 200)
            self.assertEqual(first["registered_count"], 1)
            board = self.get("/api/board")
            agent = next(
                item
                for item in board["cluster_agents"]
                if item["thread_id"] == thread_id
            )
            self.assertEqual(agent["role"], "unknown")
            self.assertEqual(agent["identity_source"], "missing")
            self.assertIsNone(agent["current_task_id"])
            self.assertTrue(agent["task_assignment_ambiguous"])
            self.assertEqual(
                agent["task_candidates"],
                sorted(
                    [
                        first_task["task_id"],
                        second_task["task_id"],
                    ]
                ),
            )
            self.assertEqual(agent["assignment_source"], "ambiguous")
            self.assertEqual(agent["prompt_status"], "unknown")
            self.assertEqual(agent["native_usage_status"], "partial")
            with self.assertRaisesRegex(
                TeamError,
                "prompt has not been verified",
            ):
                self.team.claim_task(
                    first_task["task_id"],
                    actor=f"dashboard:{thread_id}",
                )

            session_path.unlink()
            status, second = self.post(
                "/api/agents/sync-native",
                {"limit": 100},
            )
            self.assertEqual(status, 200)
            self.assertEqual(second["refreshed_count"], 1)
            stale_board = self.get("/api/board")
            stale = next(
                item
                for item in stale_board["cluster_agents"]
                if item["thread_id"] == thread_id
            )
            self.assertTrue(stale["session_stale"])
            self.assertEqual(
                stale["native_usage_status"],
                "stale",
            )
            self.assertTrue(stale["usage_stale"])
            self.assertTrue(stale["usage_available"])
            self.assertEqual(stale["last_input_tokens"], 100)
            self.assertEqual(
                stale["native_session_path"],
                str(session_path),
            )

    def test_control_cluster_goal_pause_resume_and_cancel(self) -> None:
        cluster = ClusterManager(self.settings, team=self.team)
        goal = cluster.create_goal(
            self.repo,
            objective="Control the release",
            autonomy="auto",
        )
        daemon = {"running": True, "pid": 4321}
        with (
            patch(
                "codex_team.dashboard.daemon_status",
                return_value=daemon,
            ) as current_daemon,
            patch(
                "codex_team.dashboard.ensure_daemon",
                return_value=daemon,
            ) as start_daemon,
        ):
            status, paused = self.post(
                "/api/clusters/control",
                {"goal_id": goal["goal_id"], "action": "pause"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(paused["goal"]["status"], "paused")
            self.assertEqual(paused["daemon"], daemon)
            start_daemon.assert_not_called()

            status, resumed = self.post(
                "/api/clusters/control",
                {"goal_id": goal["goal_id"], "action": "resume"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(resumed["goal"]["status"], "active")
            self.assertEqual(resumed["daemon"], daemon)
            start_daemon.assert_called_once_with(self.settings)
            board = self.get("/api/board")
            board_goal = next(
                item
                for item in board["cluster_goals"]
                if item["goal_id"] == goal["goal_id"]
            )
            self.assertEqual(board_goal["status"], "active")

            task = self.team.create_task(
                self.repo,
                title="Canceled with goal",
                description="Must be blocked when the goal is canceled.",
                acceptance="blocked with goal cancellation reason",
                kind="read",
                owner_role="team_explorer",
                goal_id=goal["goal_id"],
            )
            done_task = self.team.create_task(
                self.repo,
                title="Goal cascade archive",
                description="Done task archived with its goal.",
                acceptance="cascade through dashboard API",
                kind="read",
                owner_role="team_explorer",
                goal_id=goal["goal_id"],
            )
            with self.team.connection() as connection:
                connection.execute(
                    """
                    UPDATE tasks
                    SET status = 'done', completed_at = ?, updated_at = ?,
                        version = version + 1
                    WHERE task_id = ?
                    """,
                    (utc_now(), utc_now(), done_task["task_id"]),
                )
            self.team.claim_task(task["task_id"], actor="worker")
            with self.team.connection() as connection:
                connection.execute(
                    """
                    INSERT INTO cluster_jobs(
                        job_id, goal_id, task_id, kind, role, status,
                        log_path, output_path, started_at
                    ) VALUES (
                        'J-CANCEL-TEST', ?, ?, 'worker', 'team_explorer', 'running',
                        ?, ?, ?
                    )
                    """,
                    (
                        goal["goal_id"],
                        task["task_id"],
                        str(self.base / "cancel.log"),
                        str(self.base / "cancel.out"),
                        "2026-11-07T10:00:00Z",
                    ),
                )
            status, canceled = self.post(
                "/api/clusters/control",
                {"goal_id": goal["goal_id"], "action": "cancel"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(canceled["goal"]["status"], "failed")
            self.assertIn("cancel requested", canceled["goal"]["last_error"])
            current_daemon.assert_called()
            canceled_task = self.team.get_task(task["task_id"])
            self.assertEqual(canceled_task["status"], "blocked")
            self.assertIn("goal canceled", canceled_task["blocker"])
            with self.team.connection(write=False) as connection:
                job = connection.execute(
                    "SELECT status, error FROM cluster_jobs WHERE job_id = 'J-CANCEL-TEST'"
                ).fetchone()
            self.assertEqual(job["status"], "interrupted")
            self.assertIn("cancel requested", job["error"])
            status, invalid_resume = self.post(
                "/api/clusters/control",
                {"goal_id": goal["goal_id"], "action": "resume"},
            )
            self.assertEqual(status, 409)
            self.assertIn("invalid goal transition", invalid_resume["error"])
            status, archived = self.post(
                "/api/clusters/archive",
                {"goal_id": goal["goal_id"], "archived": True},
            )
            self.assertEqual(status, 200)
            self.assertTrue(archived["goal"]["archived_at"])
            archived_task = self.team.get_task(done_task["task_id"])
            self.assertIsNotNone(archived_task["archived_at"])
            self.assertEqual(archived_task["archived_by_goal"], 1)
            active_board = self.get("/api/board")
            visible_board = self.get("/api/board?include_archived=1")
            self.assertNotIn(
                goal["goal_id"],
                {item["goal_id"] for item in active_board["cluster_goals"]},
            )
            self.assertIn(
                goal["goal_id"],
                {item["goal_id"] for item in visible_board["cluster_goals"]},
            )
            status, restored = self.post(
                "/api/clusters/archive",
                {"goal_id": goal["goal_id"], "archived": False},
            )
            self.assertEqual(status, 200)
            self.assertIsNone(restored["goal"]["archived_at"])
            restored_task = self.team.get_task(done_task["task_id"])
            self.assertIsNone(restored_task["archived_at"])
            self.assertEqual(restored_task["archived_by_goal"], 0)

    def test_daemon_start_failure_marks_goal_failed(self) -> None:
        with patch(
            "codex_team.dashboard.ensure_daemon",
            side_effect=ClusterError("daemon unavailable"),
        ):
            status, body = self.post(
                "/api/clusters",
                {
                    "project_id": self.project["project_id"],
                    "objective": "Daemon failure rollback",
                },
            )
        self.assertEqual(status, 409)
        self.assertIn("failed to start", body["error"])
        board = self.get("/api/board")
        failed = next(
            goal
            for goal in board["cluster_goals"]
            if goal["objective"] == "Daemon failure rollback"
        )
        self.assertEqual(failed["status"], "failed")
        self.assertIn("daemon start failed", failed["last_error"])

    def test_archived_project_is_rejected_before_starting_daemon(self) -> None:
        self.team.set_project_archived(self.project["project_id"], True)
        with patch("codex_team.dashboard.ensure_daemon") as start_daemon:
            status, body = self.post(
                "/api/clusters",
                {
                    "project_id": self.project["project_id"],
                    "objective": "Must not start",
                },
            )

        self.assertEqual(status, 409)
        self.assertIn("archived", body["error"])
        start_daemon.assert_not_called()

    def test_invalid_cluster_requests_return_json_errors(self) -> None:
        requests = [
            (
                "/api/clusters",
                {"project_id": self.project["project_id"]},
            ),
            (
                "/api/clusters",
                {
                    "project_id": self.project["project_id"],
                    "objective": "Invalid autonomy",
                    "autonomy": "manual",
                },
            ),
            (
                "/api/clusters",
                {
                    "project_id": self.project["project_id"],
                    "objective": "Invalid autonomy type",
                    "autonomy": [],
                },
            ),
            (
                "/api/clusters",
                {
                    "project_id": self.project["project_id"],
                    "objective": "Invalid attempts",
                    "max_worker_attempts": 0,
                },
            ),
            (
                "/api/clusters/control",
                {"goal_id": "G-MISSING", "action": "restart"},
            ),
        ]
        with patch("codex_team.dashboard.ensure_daemon") as start_daemon:
            for path, payload in requests:
                with self.subTest(path=path, payload=payload):
                    status, body = self.post(path, payload)
                    self.assertGreaterEqual(status, 400)
                    self.assertLess(status, 500)
                    self.assertIn("error", body)
            start_daemon.assert_not_called()

    def test_reset_agent_rejects_busy_and_records_idle_request(self) -> None:
        now = "2026-11-07T11:30:00Z"
        with self.team.connection() as connection:
            for agent_id, status, archived_at in (
                ("A-BUSY", "busy", None),
                ("A-IDLE", "idle", None),
                ("A-SUPERSEDED", "superseded", None),
                ("A-ARCHIVED", "stored", now),
            ):
                connection.execute(
                    """
                    INSERT INTO cluster_agents(
                        agent_id, project_id, role, workstream_key,
                        display_name, thread_id, status, archived_at,
                        created_at, updated_at
                    ) VALUES (?, ?, 'team_worker', 'reset-test', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        agent_id,
                        self.project["project_id"],
                        f"Agent {agent_id}",
                        f"thread-{agent_id.lower()}",
                        status,
                        archived_at,
                        now,
                        now,
                    ),
                )

        with (
            patch(
                "codex_team.dashboard.ensure_daemon",
                side_effect=AssertionError("reset must not start a daemon"),
            ) as start_daemon,
            patch(
                "codex_team.dashboard.daemon_status",
                side_effect=AssertionError("reset must not inspect a daemon"),
            ) as read_daemon,
        ):
            status, busy = self.post(
                "/api/agents/reset",
                {"agent_id": "A-BUSY", "reason": "must be rejected"},
            )
            status_ok, reset = self.post(
                "/api/agents/reset",
                {"agent_id": "A-IDLE", "reason": "manual diagnostics reset"},
            )
            status_superseded, superseded = self.post(
                "/api/agents/reset",
                {"agent_id": "A-SUPERSEDED", "reason": "must be rejected"},
            )
            status_archived, archived = self.post(
                "/api/agents/reset",
                {"agent_id": "A-ARCHIVED", "reason": "must be rejected"},
            )

        self.assertEqual(status, 409)
        self.assertIn("busy", busy["error"])
        self.assertEqual(status_superseded, 409)
        self.assertIn("superseded", superseded["error"])
        self.assertEqual(status_archived, 409)
        self.assertIn("archived", archived["error"])
        self.assertEqual(status_ok, 200)
        self.assertEqual(reset["agent"]["agent_id"], "A-IDLE")
        self.assertEqual(reset["agent"]["status"], "idle")
        self.assertIsNotNone(reset["agent"]["reset_requested_at"])
        self.assertEqual(
            reset["agent"]["reset_reason"],
            "manual diagnostics reset",
        )
        start_daemon.assert_not_called()
        read_daemon.assert_not_called()

        with self.team.connection(write=False) as connection:
            agents = {
                row["agent_id"]: dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM cluster_agents
                    WHERE agent_id IN (
                        'A-BUSY', 'A-IDLE', 'A-SUPERSEDED', 'A-ARCHIVED'
                    )
                    """
                )
            }
            events = connection.execute(
                """
                SELECT agent_id, event_type, actor, data_json
                FROM cluster_agent_events
                ORDER BY event_id
                """
            ).fetchall()
        self.assertIsNone(agents["A-BUSY"]["reset_requested_at"])
        self.assertEqual(agents["A-BUSY"]["reset_reason"], "")
        self.assertIsNone(agents["A-SUPERSEDED"]["reset_requested_at"])
        self.assertIsNone(agents["A-ARCHIVED"]["reset_requested_at"])
        self.assertIsNotNone(agents["A-IDLE"]["reset_requested_at"])
        self.assertEqual(
            agents["A-IDLE"]["reset_reason"],
            "manual diagnostics reset",
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["agent_id"], "A-IDLE")
        self.assertEqual(events[0]["event_type"], "reset_requested")
        self.assertEqual(events[0]["actor"], "human")
        self.assertEqual(
            json.loads(events[0]["data_json"])["reason"],
            "manual diagnostics reset",
        )

    def test_resume_daemon_failure_keeps_goal_paused(self) -> None:
        cluster = ClusterManager(self.settings, team=self.team)
        goal = cluster.create_goal(
            self.repo,
            objective="Resume only after daemon is healthy",
        )
        cluster.set_goal_status(
            goal["goal_id"],
            "paused",
            reason="pause before resume failure",
        )
        with patch(
            "codex_team.dashboard.ensure_daemon",
            side_effect=RuntimeError("daemon start failed"),
        ) as start_daemon:
            status, body = self.post(
                "/api/clusters/control",
                {"goal_id": goal["goal_id"], "action": "resume"},
            )
        self.assertEqual(status, 409)
        self.assertIn("failed to start", body["error"])
        start_daemon.assert_called_once_with(self.settings)
        self.assertEqual(
            cluster.get_goal(goal["goal_id"])["status"],
            "paused",
        )

    def test_malformed_json_returns_json_error(self) -> None:
        request = urllib.request.Request(
            self.base_url + "/api/clusters",
            data=b"{",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        error = caught.exception
        self.assertEqual(error.code, 400)
        body = json.loads(error.read().decode("utf-8"))
        error.close()
        self.assertIn("error", body)


if __name__ == "__main__":
    unittest.main()
