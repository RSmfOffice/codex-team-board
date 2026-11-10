from __future__ import annotations

import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from typing import Any
from unittest.mock import patch

from codex_team.agent_pool import (
    CURRENT_AGENT_PROFILE,
    AgentPool,
    AgentPoolError,
)
from codex_team.db import SCHEMA_VERSION, connect, utc_now


class AgentPoolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary.name) / "team.sqlite"
        now = utc_now()
        with closing(connect(self.db_path)) as connection:
            for project_id, name in (("P1", "Project One"), ("P2", "Project Two")):
                connection.execute(
                    """
                    INSERT INTO projects(
                        project_id, name, root_path, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (project_id, name, str(Path(self.temporary.name) / project_id), now, now),
                )
            connection.commit()
        self.pool = AgentPool(self.db_path)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def register(
        self,
        *,
        project_id: str = "P1",
        role: str = "team_explorer",
        workstream_key: str = "main",
        thread_id: str | None = None,
        model: str = "model-a",
        model_provider: str = "provider-a",
        display_name: str | None = None,
        profile: str | None = None,
    ) -> dict[str, Any]:
        return self.pool.register_thread(
            project_id,
            role,
            workstream_key,
            thread_id or f"thread-{role}-{workstream_key}",
            model,
            model_provider,
            display_name or f"{role} {workstream_key}",
            profile=profile,
        )

    def seed_runtime_references(self, project_id: str = "P1") -> None:
        now = utc_now()
        with closing(connect(self.db_path)) as connection:
            connection.execute(
                """
                INSERT INTO cluster_goals(
                    goal_id, project_id, objective, status, autonomy,
                    workstream_key, created_at, updated_at
                ) VALUES ('G1', ?, 'Agent lifecycle test', 'active', 'auto',
                          'main', ?, ?)
                """,
                (project_id, now, now),
            )
            connection.commit()
            connection.execute(
                """
                INSERT INTO tasks(
                    task_id, project_id, goal_id, title, description, acceptance,
                    kind, status, priority, owner_role, continuity_mode,
                    created_at, updated_at
                ) VALUES ('T1', ?, 'G1', 'Test task', 'Test agent pool',
                          'Lifecycle is persisted', 'read', 'ready', 1,
                          'team_explorer', 'auto', ?, ?)
                """,
                (project_id, now, now),
            )
            connection.execute(
                """
                INSERT INTO cluster_jobs(
                    job_id, goal_id, task_id, kind, role, status, attempt,
                    log_path, output_path, started_at, finished_at
                ) VALUES ('J1', 'G1', 'T1', 'worker', 'team_explorer',
                          'completed', 1, 'job.log', 'job.md', ?, ?)
                """,
                (now, now),
            )
            connection.commit()

    def agent_events(self, agent_id: str) -> list[dict[str, Any]]:
        with closing(connect(self.db_path)) as connection:
            rows = connection.execute(
                """
                SELECT * FROM cluster_agent_events
                WHERE agent_id = ? ORDER BY event_id
                """,
                (agent_id,),
            ).fetchall()
        return [
            {
                **dict(row),
                "data": json.loads(row["data_json"]),
            }
            for row in rows
        ]

    def test_selection_matrix_and_identity_scope(self) -> None:
        worker = self.register(role="team_worker")
        self.assertIsNone(
            self.pool.select_agent(
                "P1", "team_worker", "main", "auto", "write", "model-a", "provider-a"
            )
        )
        self.assertIsNone(
            self.pool.select_agent(
                "P1", "team_worker", "main", "fresh", "write", "model-a", "provider-a"
            )
        )
        selected_worker = self.pool.select_agent(
            "P1", "team_worker", "main", "reuse", "write", "model-a", "provider-a"
        )
        self.assertEqual(selected_worker["agent_id"], worker["agent_id"])

        explorer = self.register(role="team_explorer", thread_id="explorer-auto")
        selected_explorer = self.pool.select_agent(
            "P1",
            "team_explorer",
            "main",
            "auto",
            "read",
            "model-a",
            "provider-a",
        )
        self.assertEqual(selected_explorer["agent_id"], explorer["agent_id"])
        self.pool.release(
            selected_explorer["agent_id"],
            {
                "input": 1,
                "cached": 1,
                "context": 1,
                "modelContextWindow": 100,
            },
        )
        selected_explorer = self.pool.select_agent(
            "P1", "team_explorer", "main", "reuse", "read", "model-a", "provider-a"
        )
        self.assertEqual(selected_explorer["agent_id"], explorer["agent_id"])

        reviewer = self.register(role="team_reviewer", thread_id="reviewer-auto")
        selected_reviewer = self.pool.select_agent(
            "P1",
            "team_reviewer",
            "main",
            "auto",
            "review",
            "model-a",
            "provider-a",
        )
        self.assertEqual(selected_reviewer["agent_id"], reviewer["agent_id"])
        self.pool.release(
            selected_reviewer["agent_id"],
            {
                "input": 1,
                "cached": 1,
                "context": 1,
                "modelContextWindow": 100,
            },
        )
        selected_reviewer = self.pool.select_agent(
            "P1", "team_reviewer", "main", "reuse", "review", "model-a", "provider-a"
        )
        self.assertEqual(selected_reviewer["agent_id"], reviewer["agent_id"])

        lead = self.register(role="lead", thread_id="lead-auto")
        self.assertIsNone(
            self.pool.select_agent(
                "P1", "lead", "main", "auto", "read", "model-a", "provider-a"
            )
        )
        now = utc_now()
        with closing(connect(self.db_path)) as connection:
            connection.execute(
                """
                INSERT INTO cluster_goals(
                    goal_id, project_id, objective, status, autonomy,
                    workstream_key, continuity_reason, created_at, updated_at
                ) VALUES ('G-CONT', 'P1', 'Continue work', 'planning', 'auto',
                          'main', 'related read-only follow-up', ?, ?)
                """,
                (now, now),
            )
            connection.commit()
        selected_lead = self.pool.select_agent(
            "P1", "lead", "main", "auto", "read", "model-a", "provider-a"
        )
        self.assertEqual(selected_lead["agent_id"], lead["agent_id"])

        scoped = self.register(
            role="team_explorer",
            workstream_key="other",
            thread_id="scoped-explorer",
        )
        for arguments in (
            ("P2", "team_explorer", "other", "model-a", "provider-a"),
            ("P1", "team_reviewer", "other", "model-a", "provider-a"),
            ("P1", "team_explorer", "main", "model-a", "provider-a"),
            ("P1", "team_explorer", "other", "model-b", "provider-a"),
            ("P1", "team_explorer", "other", "model-a", "provider-b"),
        ):
            self.assertIsNone(
                self.pool.select_agent(
                    arguments[0],
                    arguments[1],
                    arguments[2],
                    "reuse",
                    "read",
                    arguments[3],
                    arguments[4],
                )
            )
        selected_scoped = self.pool.select_agent(
            "P1", "team_explorer", "other", "reuse", "read", "model-a", "provider-a"
        )
        self.assertEqual(selected_scoped["agent_id"], scoped["agent_id"])

    def test_agent_profile_migration_is_idempotent(self) -> None:
        path = Path(self.temporary.name) / "profile-migration.sqlite"
        now = utc_now()
        with closing(connect(path)) as connection:
            self.assertGreaterEqual(SCHEMA_VERSION, 18)
            connection.execute("ALTER TABLE cluster_agents DROP COLUMN profile")
            connection.execute(
                "DELETE FROM schema_migrations WHERE version = ?",
                (SCHEMA_VERSION,),
            )
            connection.execute("PRAGMA user_version = 13")
            connection.execute(
                """
                INSERT INTO projects(
                    project_id, name, root_path, created_at, updated_at
                ) VALUES ('P-PROFILE', 'Profile', ?, ?, ?)
                """,
                (str(path.parent / "profile-project"), now, now),
            )
            connection.execute(
                """
                INSERT INTO cluster_agents(
                    agent_id, project_id, role, workstream_key, display_name,
                    thread_id, status, created_at, updated_at
                ) VALUES (
                    'A-LEGACY', 'P-PROFILE', 'lead', 'main', 'Legacy Lead',
                    'legacy-thread', 'idle', ?, ?
                )
                """,
                (now, now),
            )
            connection.commit()

        for _ in range(2):
            with closing(connect(path)) as upgraded:
                row = upgraded.execute(
                    """
                    SELECT profile FROM cluster_agents
                    WHERE agent_id = 'A-LEGACY'
                    """
                ).fetchone()
                self.assertEqual(row["profile"], "legacy")
                self.assertEqual(
                    upgraded.execute("PRAGMA user_version").fetchone()[0],
                    SCHEMA_VERSION,
                )
                self.assertEqual(
                    upgraded.execute(
                        """
                        SELECT COUNT(*) FROM schema_migrations
                        WHERE version = ?
                        """,
                        (SCHEMA_VERSION,),
                    ).fetchone()[0],
                    1,
                )

    def test_selection_requires_current_profile_for_explicit_reuse(self) -> None:
        legacy = self.register(thread_id="legacy-profile")
        self.assertEqual(legacy["profile"], CURRENT_AGENT_PROFILE)
        with closing(connect(self.db_path)) as connection:
            connection.execute(
                """
                UPDATE cluster_agents SET profile = 'legacy'
                WHERE agent_id = ?
                """,
                (legacy["agent_id"],),
            )
            connection.commit()

        self.assertIsNone(
            self.pool.select_agent(
                "P1",
                "team_explorer",
                "main",
                "reuse",
                "read",
                "model-a",
                "provider-a",
            )
        )

        current = self.register(thread_id="current-profile")
        selected = self.pool.select_agent(
            "P1",
            "team_explorer",
            "main",
            "reuse",
            "read",
            "model-a",
            "provider-a",
        )
        self.assertEqual(selected["agent_id"], current["agent_id"])
        self.assertEqual(selected["profile"], CURRENT_AGENT_PROFILE)

    def test_null_profile_is_migrated_and_not_reused(self) -> None:
        agent = self.register(thread_id="null-profile")
        with closing(connect(self.db_path)) as connection:
            connection.execute("ALTER TABLE cluster_agents DROP COLUMN profile")
            connection.execute("ALTER TABLE cluster_agents ADD COLUMN profile TEXT")
            connection.execute(
                """
                UPDATE cluster_agents SET profile = NULL
                WHERE agent_id = ?
                """,
                (agent["agent_id"],),
            )
            connection.commit()

        self.assertIsNone(
            self.pool.select_agent(
                "P1",
                "team_explorer",
                "main",
                "reuse",
                "read",
                "model-a",
                "provider-a",
            )
        )
        with closing(connect(self.db_path)) as connection:
            row = connection.execute(
                """
                SELECT profile FROM cluster_agents
                WHERE agent_id = ?
                """,
                (agent["agent_id"],),
            ).fetchone()
        self.assertEqual(row["profile"], "legacy")

    def test_selects_most_recently_used_idle_agent_then_falls_back(self) -> None:
        older = self.register(thread_id="older")
        newer = self.register(thread_id="newer")
        with closing(connect(self.db_path)) as connection:
            connection.execute(
                "UPDATE cluster_agents SET last_used_at = ? WHERE agent_id = ?",
                ("2026-01-01T00:00:00Z", older["agent_id"]),
            )
            connection.execute(
                "UPDATE cluster_agents SET last_used_at = ? WHERE agent_id = ?",
                ("2026-02-01T00:00:00Z", newer["agent_id"]),
            )
            connection.commit()
        first = self.pool.select_agent(
            "P1", "team_explorer", "main", "reuse", "read", "model-a", "provider-a"
        )
        self.assertEqual(first["agent_id"], newer["agent_id"])
        second = self.pool.select_agent(
            "P1", "team_explorer", "main", "reuse", "read", "model-a", "provider-a"
        )
        self.assertEqual(second["agent_id"], older["agent_id"])
        self.pool.release(first["agent_id"], {"input": 1, "cached": 0})

    def test_context_threshold_forces_a_fresh_thread(self) -> None:
        agent = self.register(thread_id="high-context")
        with closing(connect(self.db_path)) as connection:
            connection.execute(
                """
                UPDATE cluster_agents
                SET context_tokens = 90, model_context_window = 100,
                    turn_count = 1
                WHERE agent_id = ?
                """,
                (agent["agent_id"],),
            )
            connection.commit()
        with patch.dict(
            os.environ,
            {"CODEX_TEAM_AGENT_REUSE_CONTEXT_RATIO": "0.85"},
        ):
            self.assertIsNone(
                self.pool.select_agent(
                    "P1",
                    "team_explorer",
                    "main",
                    "reuse",
                    "read",
                    "model-a",
                    "provider-a",
                )
            )
        with patch.dict(
            os.environ,
            {"CODEX_TEAM_AGENT_REUSE_CONTEXT_RATIO": "0.95"},
        ):
            selected = self.pool.select_agent(
                "P1",
                "team_explorer",
                "main",
                "reuse",
                "read",
                "model-a",
                "provider-a",
            )
        self.assertEqual(selected["agent_id"], agent["agent_id"])

    def test_exact_cache_profile_is_required_for_reuse(self) -> None:
        agent = self.register(
            role="team_explorer",
            thread_id="profile-stable",
            profile="cache;v1",
        )
        self.assertIsNone(
            self.pool.select_agent(
                "P1",
                "team_explorer",
                "main",
                "reuse",
                "read",
                "model-a",
                "provider-a",
                profile="cache;v2",
            )
        )
        selected = self.pool.select_agent(
            "P1",
            "team_explorer",
            "main",
            "reuse",
            "read",
            "model-a",
            "provider-a",
            profile="cache;v1",
        )
        self.assertEqual(selected["agent_id"], agent["agent_id"])

    def test_concurrent_selection_reserves_only_one_agent(self) -> None:
        agent = self.register(thread_id="concurrent")

        def select() -> dict[str, Any] | None:
            return self.pool.select_agent(
                "P1",
                "team_explorer",
                "main",
                "reuse",
                "read",
                "model-a",
                "provider-a",
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: select(), range(16)))
        selected = [result for result in results if result is not None]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["agent_id"], agent["agent_id"])

    def test_loaded_and_stored_agents_remain_selectable(self) -> None:
        agent = self.register(thread_id="loaded-stored")
        with closing(connect(self.db_path)) as connection:
            connection.execute(
                "UPDATE cluster_agents SET status = 'loaded' WHERE agent_id = ?",
                (agent["agent_id"],),
            )
            connection.commit()
        selected = self.pool.select_agent(
            "P1", "team_explorer", "main", "reuse", "read", "model-a", "provider-a"
        )
        self.assertEqual(selected["agent_id"], agent["agent_id"])
        stored = self.pool.release(
            agent["agent_id"],
            {
                "input": 1,
                "cached": 1,
                "context": 1,
                "modelContextWindow": 100,
            },
            cache_epoch_changed=True,
        )
        self.assertEqual(stored["status"], "stored")
        selected_again = self.pool.select_agent(
            "P1", "team_explorer", "main", "reuse", "read", "model-a", "provider-a"
        )
        self.assertEqual(selected_again["agent_id"], agent["agent_id"])

    def test_unknown_context_is_not_reused(self) -> None:
        agent = self.register(thread_id="unknown-context")
        selected = self.pool.select_agent(
            "P1",
            "team_explorer",
            "main",
            "reuse",
            "read",
            "model-a",
            "provider-a",
        )
        self.assertEqual(selected["agent_id"], agent["agent_id"])
        released = self.pool.release(
            agent["agent_id"],
            {"input": 100, "cached": 50, "modelContextWindow": 1000},
        )
        self.assertEqual(released["turn_count"], 1)
        self.assertIsNone(released["model_context_window"])
        self.assertIsNone(
            self.pool.select_agent(
                "P1",
                "team_explorer",
                "main",
                "reuse",
                "read",
                "model-a",
                "provider-a",
            )
        )

    def test_busy_and_release_update_links_usage_and_audit(self) -> None:
        self.seed_runtime_references()
        agent = self.register()
        selected = self.pool.select_agent(
            "P1", "team_explorer", "main", "reuse", "read", "model-a", "provider-a"
        )
        busy = self.pool.mark_busy(
            selected["agent_id"],
            "G1",
            "T1",
            "J1",
            "turn-1",
        )
        self.assertEqual(busy["status"], "busy")
        self.assertEqual(busy["current_goal_id"], "G1")
        self.assertEqual(busy["current_task_id"], "T1")
        self.assertEqual(busy["current_job_id"], "J1")
        self.assertEqual(busy["last_turn_id"], "turn-1")
        with self.assertRaisesRegex(AgentPoolError, "busy"):
            self.pool.request_reset(agent["agent_id"], "manual", "human")

        released = self.pool.release(
            agent["agent_id"],
            {
                "input": 100,
                "cached": 60,
                "output": 12,
                "context": 44,
                "modelContextWindow": 1000,
            },
        )
        self.assertEqual(released["status"], "idle")
        self.assertEqual(released["last_input_tokens"], 100)
        self.assertEqual(released["last_cached_input_tokens"], 60)
        self.assertEqual(released["last_uncached_input_tokens"], 40)
        self.assertEqual(released["total_input_tokens"], 100)
        self.assertEqual(released["total_cached_input_tokens"], 60)
        self.assertEqual(released["total_uncached_input_tokens"], 40)
        self.assertEqual(released["context_tokens"], 44)
        self.assertEqual(released["model_context_window"], 1000)
        self.assertEqual(released["turn_count"], 1)
        self.assertIsNone(released["current_goal_id"])
        self.assertEqual(released["last_turn_id"], "turn-1")

        self.pool.select_agent(
            "P1", "team_explorer", "main", "reuse", "read", "model-a", "provider-a"
        )
        self.pool.mark_busy(agent["agent_id"], "G1", "T1", "J1", "turn-2")
        released_again = self.pool.release(
            agent["agent_id"],
            {"input_tokens": 20, "cached_input_tokens": 5, "context_tokens": 9},
            cache_epoch_changed=True,
        )
        self.assertEqual(released_again["status"], "stored")
        self.assertEqual(released_again["total_input_tokens"], 120)
        self.assertEqual(released_again["total_cached_input_tokens"], 65)
        self.assertEqual(released_again["total_uncached_input_tokens"], 55)
        self.assertEqual(released_again["last_uncached_input_tokens"], 15)
        self.assertEqual(released_again["turn_count"], 2)

        events = self.agent_events(agent["agent_id"])
        self.assertEqual(
            [event["event_type"] for event in events],
            ["registered", "selected", "busy", "released", "selected", "busy", "released"],
        )
        self.assertEqual(events[-1]["data"]["usage"]["uncached_input_tokens"], 15)
        self.assertTrue(events[-1]["data"]["cache_epoch_changed"])

    def test_reset_request_unsubscribes_and_supersedes(self) -> None:
        agent = self.register(thread_id="reset-thread")
        requested = self.pool.request_reset(agent["agent_id"], "manual reset", "human")
        self.assertIsNotNone(requested["reset_requested_at"])
        self.assertIsNone(
            self.pool.select_agent(
                "P1", "team_explorer", "main", "reuse", "read", "model-a", "provider-a"
            )
        )
        unsubscribed: list[str] = []
        applied = self.pool.apply_reset_requests(unsubscribed.append)
        self.assertEqual(unsubscribed, ["reset-thread"])
        self.assertEqual([item["agent_id"] for item in applied], [agent["agent_id"]])
        self.assertEqual(applied[0]["status"], "superseded")
        self.assertIsNone(applied[0]["reset_requested_at"])
        self.assertEqual(self.pool.apply_reset_requests(unsubscribed.append), [])
        events = self.agent_events(agent["agent_id"])
        self.assertEqual(
            [event["event_type"] for event in events],
            ["registered", "reset_requested", "reset_applied"],
        )
        self.assertEqual(events[1]["actor"], "human")
        self.assertIn("requested_at", events[1]["data"])

    def test_reset_rejects_superseded_and_archived_agents(self) -> None:
        superseded = self.register(thread_id="superseded-thread")
        archived = self.register(
            thread_id="archived-thread",
            workstream_key="archived",
        )
        self.pool.mark_superseded(
            superseded["agent_id"],
            "replaced by a fresh thread",
            "human",
        )
        with self.assertRaisesRegex(AgentPoolError, "superseded"):
            self.pool.request_reset(
                superseded["agent_id"],
                "must be rejected",
                "human",
            )

        self.pool.archive_project("P1", True)
        with self.assertRaisesRegex(AgentPoolError, "archived"):
            self.pool.request_reset(
                archived["agent_id"],
                "must be rejected",
                "human",
            )

    def test_failed_agent_with_reset_need_is_recovered(self) -> None:
        agent = self.register(thread_id="failed-thread")
        failed = self.pool.mark_failed(
            agent["agent_id"],
            "context window exceeded",
            needs_reset=True,
        )
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["last_error"], "context window exceeded")
        self.assertIsNotNone(failed["reset_requested_at"])
        self.assertIsNone(
            self.pool.select_agent(
                "P1", "team_explorer", "main", "reuse", "read", "model-a", "provider-a"
            )
        )
        with self.assertRaisesRegex(AgentPoolError, "failed"):
            self.pool.mark_busy(agent["agent_id"], None, None, None, None)
        unsubscribed: list[str] = []
        applied = self.pool.apply_reset_requests(unsubscribed.append)
        self.assertEqual(unsubscribed, ["failed-thread"])
        self.assertEqual(applied[0]["status"], "superseded")
        self.assertEqual(
            [event["event_type"] for event in self.agent_events(agent["agent_id"])],
            ["registered", "failed", "reset_applied"],
        )

    def test_archive_project_hides_and_restores_agents(self) -> None:
        first = self.register(thread_id="archive-one")
        second = self.register(
            project_id="P2",
            thread_id="archive-two",
            workstream_key="two",
        )
        archived = self.pool.archive_project("P1", True)
        self.assertEqual([item["agent_id"] for item in archived], [first["agent_id"]])
        self.assertEqual(len(self.pool.list_agents("P1")), 0)
        self.assertEqual(len(self.pool.list_agents("P1", include_archived=True)), 1)
        archive_events = self.agent_events(first["agent_id"])
        self.pool.archive_project("P1", True)
        self.assertEqual(self.agent_events(first["agent_id"]), archive_events)
        self.assertIsNone(
            self.pool.select_agent(
                "P1", "team_explorer", "main", "reuse", "read", "model-a", "provider-a"
            )
        )
        self.assertEqual(len(self.pool.list_agents("P2")), 1)
        with closing(connect(self.db_path)) as connection:
            project = connection.execute(
                "SELECT archived_at FROM projects WHERE project_id = 'P1'"
            ).fetchone()
        self.assertIsNotNone(project["archived_at"])

        restored = self.pool.archive_project("P1", False)
        self.assertEqual([item["agent_id"] for item in restored], [first["agent_id"]])
        self.assertIsNone(restored[0]["archived_at"])
        selected = self.pool.select_agent(
            "P1", "team_explorer", "main", "reuse", "read", "model-a", "provider-a"
        )
        self.assertEqual(selected["agent_id"], first["agent_id"])
        self.assertEqual(len(self.pool.list_agents("P2")), 1)
        self.assertEqual(second["archived_at"], None)

    def test_cannot_archive_project_with_busy_agent(self) -> None:
        self.register()
        self.pool.select_agent(
            "P1", "team_explorer", "main", "reuse", "read", "model-a", "provider-a"
        )
        with self.assertRaisesRegex(AgentPoolError, "busy"):
            self.pool.archive_project("P1", True)

    def test_register_thread_is_idempotent_without_metadata_conflicts(self) -> None:
        first = self.register(thread_id="stable-thread")
        second = self.register(thread_id="stable-thread")
        self.assertEqual(first, second)
        self.assertEqual(first["profile"], CURRENT_AGENT_PROFILE)
        self.assertEqual(len(self.agent_events(first["agent_id"])), 1)
        with self.assertRaisesRegex(AgentPoolError, "different agent metadata"):
            self.register(thread_id="stable-thread", role="team_reviewer")


if __name__ == "__main__":
    unittest.main()
