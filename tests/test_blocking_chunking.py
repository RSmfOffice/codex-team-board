"""Tests for chunked dependency loading: large boards must not exceed SQLite's
host-parameter limit, and the batched result must equal the legacy per-task
query output (order and fields included).
"""

from __future__ import annotations

import sqlite3
import unittest

from codex_team.service import TeamService


def _build_db(task_count: int, deps_per_task: int) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, title TEXT, status TEXT, archived_at TEXT)"
    )
    connection.execute(
        "CREATE TABLE task_dependencies (task_id TEXT, depends_on TEXT)"
    )
    for index in range(task_count):
        connection.execute(
            "INSERT INTO tasks VALUES (?, ?, 'ready', NULL)",
            (f"T-{index:06d}", f"Task {index}"),
        )
    # Each task depends on the next `deps_per_task` tasks (wrap around), all
    # unfinished, so every task has a non-empty ordered blocked_by list.
    for index in range(task_count):
        for offset in range(1, deps_per_task + 1):
            connection.execute(
                "INSERT INTO task_dependencies VALUES (?, ?)",
                (f"T-{index:06d}", f"T-{(index + offset) % task_count:06d}"),
            )
    connection.commit()
    return connection


class BlockingChunkingTests(unittest.TestCase):
    def test_large_board_does_not_exceed_parameter_limit(self) -> None:
        # 35000 ids > SQLite's 32766 limit; the unchunked query would raise.
        task_count = 35000
        connection = _build_db(task_count, deps_per_task=2)
        try:
            task_ids = [f"T-{index:06d}" for index in range(task_count)]
            result = TeamService._blocking_tasks_map(connection, task_ids)
            self.assertEqual(len(result), task_count)
            self.assertEqual(len(result["T-000000"]), 2)
        finally:
            connection.close()

    def test_batched_matches_legacy_per_task_output(self) -> None:
        task_count = 2500  # spans multiple 1000-id chunks
        connection = _build_db(task_count, deps_per_task=3)
        try:
            task_ids = [f"T-{index:06d}" for index in range(task_count)]
            batched = TeamService._blocking_tasks_map(connection, task_ids)
            for task_id in task_ids:
                legacy = TeamService._blocking_tasks(connection, task_id)
                self.assertEqual(batched[task_id], legacy, task_id)
        finally:
            connection.close()

    def test_empty_input(self) -> None:
        connection = sqlite3.connect(":memory:")
        try:
            self.assertEqual(TeamService._blocking_tasks_map(connection, []), {})
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()