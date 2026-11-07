from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from codex_team.db import connect


class MigrationTests(unittest.TestCase):
    def test_old_task_table_is_upgraded_before_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "old.sqlite"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE tasks (
                    task_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    acceptance TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    priority INTEGER NOT NULL,
                    owner_role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                PRAGMA user_version = 0;
                """
            )
            connection.commit()
            connection.close()

            upgraded = connect(path)
            try:
                self.assertEqual(
                    upgraded.execute("PRAGMA user_version").fetchone()[0],
                    8,
                )
                columns = {
                    row["name"]
                    for row in upgraded.execute("PRAGMA table_info(tasks)").fetchall()
                }
                self.assertTrue(
                    {
                        "claim_token",
                        "commit_sha",
                        "merge_commit",
                        "target_branch",
                        "goal_id",
                        "worktree_path",
                        "completed_at",
                        "archived_at",
                    }.issubset(columns)
                )
                project_columns = {
                    row["name"]
                    for row in upgraded.execute("PRAGMA table_info(projects)").fetchall()
                }
                self.assertIn("archived_at", project_columns)
                cluster_goal_columns = {
                    row["name"]
                    for row in upgraded.execute(
                        "PRAGMA table_info(cluster_goals)"
                    ).fetchall()
                }
                self.assertIn("archived_at", cluster_goal_columns)
                self.assertEqual(
                    upgraded.execute("PRAGMA integrity_check").fetchone()[0],
                    "ok",
                )
            finally:
                upgraded.close()


if __name__ == "__main__":
    unittest.main()
