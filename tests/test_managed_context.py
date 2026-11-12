"""Managed App Server current-context semantics.

The App Server ``total`` bucket is a thread-lifetime cumulative snapshot; only
``last.input`` represents the current turn context. These tests lock the rule
that ``context_tokens`` (job, AgentPool release and Dashboard managed Agent)
uses ``last.input`` and never the cumulative ``total.totalTokens``, and that the
85% reuse threshold is driven by that current context.

Every test uses a temporary Team Home; no real database is read or written.
"""

from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from codex_team.agent_pool import AgentPool
from codex_team.cluster import ClusterManager
from codex_team.config import Settings
from codex_team.db import connect, utc_now
from codex_team.service import TeamService
from codex_team.usage import current_context_input_tokens


def _settings(base: Path) -> Settings:
    return Settings(
        project_root=Path(__file__).resolve().parents[1],
        home=base / "team-home",
        db_path=base / "team-home" / "team.sqlite",
        logs_dir=base / "team-home" / "logs",
    )


class CurrentContextHelperTests(unittest.TestCase):
    def test_nested_uses_last_input_not_total(self) -> None:
        usage = {
            "total": {"input": 6_690_260, "context": 6_732_492},
            "last": {"input": 100_000, "context": 100_010},
            "modelContextWindow": 996_147,
        }
        self.assertEqual(current_context_input_tokens(usage), 100_000)

    def test_missing_last_is_unknown(self) -> None:
        self.assertIsNone(
            current_context_input_tokens(
                {"total": {"input": 100, "context": 150}}
            )
        )

    def test_empty_last_is_unknown(self) -> None:
        self.assertIsNone(
            current_context_input_tokens(
                {"total": {"input": 100, "context": 150}, "last": {}}
            )
        )

    def test_last_without_input_is_unknown(self) -> None:
        self.assertIsNone(
            current_context_input_tokens(
                {"total": {"input": 100}, "last": {"output": 5}}
            )
        )

    def test_flat_snapshot_reads_input(self) -> None:
        self.assertEqual(current_context_input_tokens({"input": 42}), 42)
        self.assertIsNone(current_context_input_tokens({"output": 5}))
        self.assertIsNone(current_context_input_tokens(None))


class JobContextSemanticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
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

    def _job_usage(self, usage: dict) -> dict:
        job = {"job_id": "J-CTX", "agent_id": "", "log_path": ""}
        return self.manager._app_job_usage(job, {"usage": usage})

    def test_large_total_small_last_uses_last_input(self) -> None:
        # Requirement 1: total.context huge but last.input small -> last.input.
        result = self._job_usage(
            {
                "total": {
                    "input": 6_690_260,
                    "cached": 6_000_000,
                    "output": 42_232,
                    "context": 6_732_492,
                },
                "last": {
                    "input": 100_000,
                    "cached": 90_000,
                    "output": 1_000,
                    "context": 100_010,
                },
                "modelContextWindow": 996_147,
            }
        )
        self.assertEqual(result["context_tokens"], 100_000)
        self.assertEqual(result["model_context_window"], 996_147)
        # Cost semantics stay cumulative (total), unchanged by the context fix.
        self.assertEqual(result["input_tokens"], 6_690_260)
        self.assertEqual(result["output_tokens"], 42_232)

    def test_missing_last_context_unknown_not_total(self) -> None:
        # Requirement 2: no last -> context unknown, never the cumulative total.
        result = self._job_usage(
            {
                "total": {"input": 100, "cached": 20, "output": 30, "context": 150},
                "modelContextWindow": 200,
            }
        )
        self.assertIsNone(result["context_tokens"])
        self.assertEqual(result["input_tokens"], 100)

    def test_empty_last_context_unknown_not_total(self) -> None:
        result = self._job_usage(
            {
                "total": {"input": 100, "cached": 20, "output": 30, "context": 150},
                "last": {},
                "modelContextWindow": 200,
            }
        )
        self.assertIsNone(result["context_tokens"])

    def test_first_job_cumulative_cost_but_current_context(self) -> None:
        # Requirement 3: first job keeps cumulative cost; context is last.input.
        result = self._job_usage(
            {
                "total": {"input": 120, "cached": 20, "output": 30, "context": 150},
                "last": {"input": 50, "cached": 5, "output": 10, "context": 60},
                "modelContextWindow": 200,
            }
        )
        self.assertEqual(result["input_tokens"], 120)
        self.assertEqual(result["cached_input_tokens"], 20)
        self.assertEqual(result["uncached_input_tokens"], 100)
        self.assertEqual(result["output_tokens"], 30)
        self.assertEqual(result["first_input_tokens"], 120)
        self.assertEqual(result["context_tokens"], 50)


class ReleaseAndReuseThresholdTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.db_path = self.base / "team-home" / "team.sqlite"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        now = utc_now()
        with closing(connect(self.db_path)) as connection:
            connection.execute(
                """
                INSERT INTO projects(project_id, name, root_path, created_at, updated_at)
                VALUES ('P1', 'Project One', ?, ?, ?)
                """,
                (str(self.base / "P1"), now, now),
            )
            connection.commit()
        self.pool = AgentPool(self.db_path)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _register(self, thread_id: str = "thread-ctx") -> dict:
        return self.pool.register_thread(
            "P1",
            "team_explorer",
            "main",
            thread_id,
            "model-a",
            "provider-a",
            "Context Explorer",
        )

    def _select(self):
        return self.pool.select_agent(
            "P1", "team_explorer", "main", "reuse", "read", "model-a", "provider-a"
        )

    def test_release_stores_current_last_input_context(self) -> None:
        # Requirement 3: released Agent context is the current last input, while
        # cumulative cost (total_input_tokens) keeps accumulating.
        agent = self._register()
        self.pool.release(
            agent["agent_id"],
            {"input": 50, "cached": 5, "output": 10, "context": 50, "modelContextWindow": 200},
        )
        self.pool.release(
            agent["agent_id"],
            {"input": 60, "cached": 6, "output": 12, "context": 60, "modelContextWindow": 200},
        )
        with closing(connect(self.db_path)) as connection:
            row = connection.execute(
                "SELECT context_tokens, total_input_tokens, model_context_window "
                "FROM cluster_agents WHERE agent_id=?",
                (agent["agent_id"],),
            ).fetchone()
        self.assertEqual(row["context_tokens"], 60)  # current last input
        self.assertEqual(row["total_input_tokens"], 110)  # cumulative cost
        self.assertEqual(row["model_context_window"], 200)

    def test_below_threshold_reuses_same_thread(self) -> None:
        # Requirement 4: current last input below 85% -> reuse the same thread.
        agent = self._register()
        self.pool.release(
            agent["agent_id"],
            {"input": 100, "cached": 10, "context": 100, "modelContextWindow": 200},
        )  # 100 / 200 = 50% < 85%
        selected = self._select()
        self.assertIsNotNone(selected)
        self.assertEqual(selected["agent_id"], agent["agent_id"])

    def test_above_threshold_requires_new_thread(self) -> None:
        # Requirement 5: current last input above 85% -> no reuse (fresh thread).
        agent = self._register()
        self.pool.release(
            agent["agent_id"],
            {"input": 180, "cached": 10, "context": 180, "modelContextWindow": 200},
        )  # 180 / 200 = 90% > 85%
        self.assertIsNone(self._select())

    def test_cumulative_total_would_exceed_but_current_context_reuses(self) -> None:
        # The regression at the heart of the bug: a long thread whose cumulative
        # input far exceeds the window must still reuse when the CURRENT last
        # input is small. Using total.totalTokens here would wrongly block reuse.
        agent = self._register()
        self.pool.release(
            agent["agent_id"],
            {
                "input": 100_000,
                "cached": 90_000,
                "output": 1_000,
                "context": 100_000,  # current last input, well below the window
                "modelContextWindow": 996_147,
            },
        )
        # Inflate cumulative cost to simulate a long-lived thread (>> window).
        with closing(connect(self.db_path)) as connection:
            connection.execute(
                "UPDATE cluster_agents SET total_input_tokens = 6690260 "
                "WHERE agent_id=?",
                (agent["agent_id"],),
            )
            connection.commit()
        selected = self._select()
        self.assertIsNotNone(selected)
        self.assertEqual(selected["agent_id"], agent["agent_id"])

    def test_unknown_context_is_not_reused(self) -> None:
        # When the current context is unknown (no last input), the thread is not
        # reused; the cumulative total must not stand in for it.
        agent = self._register()
        self.pool.release(
            agent["agent_id"],
            {"input": 100, "cached": 20, "output": 30, "modelContextWindow": 200},
        )  # no context key -> unknown
        self.assertIsNone(self._select())


if __name__ == "__main__":
    unittest.main()