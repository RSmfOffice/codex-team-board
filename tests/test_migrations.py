from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from codex_team.db import SCHEMA_VERSION, connect


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
                    SCHEMA_VERSION,
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
                        "archived_by_goal",
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
                self.assertTrue(
                    {"workstream_key", "continuity_reason"}.issubset(
                        cluster_goal_columns
                    )
                )
                task_columns = {
                    row["name"]
                    for row in upgraded.execute("PRAGMA table_info(tasks)").fetchall()
                }
                self.assertIn("continuity_mode", task_columns)
                job_columns = {
                    row["name"]
                    for row in upgraded.execute(
                        "PRAGMA table_info(cluster_jobs)"
                    ).fetchall()
                }
                self.assertTrue(
                    {
                        "agent_id",
                        "turn_id",
                        "runtime_generation",
                        "cached_input_tokens",
                        "uncached_input_tokens",
                        "model_context_window",
                    }.issubset(job_columns)
                )
                agent_columns = {
                    row["name"]
                    for row in upgraded.execute(
                        "PRAGMA table_info(cluster_agents)"
                    ).fetchall()
                }
                self.assertTrue(
                    {
                        "agent_id",
                        "thread_id",
                        "workstream_key",
                        "role",
                        "status",
                        "total_cached_input_tokens",
                    }.issubset(agent_columns)
                )
                self.assertEqual(
                    upgraded.execute("PRAGMA integrity_check").fetchone()[0],
                    "ok",
                )
            finally:
                upgraded.close()

    def test_schema_17_keeps_usage_presence_and_archive_migrations_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "usage-v14.sqlite"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                INSERT INTO schema_migrations(version, applied_at)
                VALUES (14, '2026-11-08T00:00:00Z');
                CREATE TABLE cluster_jobs (
                    job_id TEXT PRIMARY KEY,
                    goal_id TEXT NOT NULL,
                    task_id TEXT,
                    kind TEXT NOT NULL,
                    role TEXT NOT NULL,
                    status TEXT NOT NULL,
                    thread_id TEXT,
                    log_path TEXT NOT NULL,
                    output_path TEXT NOT NULL,
                    started_at TEXT NOT NULL
                );
                INSERT INTO cluster_jobs(
                    job_id, goal_id, kind, role, status,
                    log_path, output_path, started_at
                ) VALUES (
                    'J-OLD', 'G-OLD', 'worker', 'team_worker', 'completed',
                    'old.log', 'old.out', '2026-11-08T00:00:00Z'
                );
                PRAGMA user_version = 14;
                """
            )
            connection.commit()
            connection.close()

            upgraded = connect(path)
            try:
                old = upgraded.execute(
                    """
                    SELECT usage_missing_fields
                    FROM cluster_jobs WHERE job_id = 'J-OLD'
                    """
                ).fetchone()
                self.assertEqual(old["usage_missing_fields"], "[]")
                self.assertEqual(
                    upgraded.execute("PRAGMA user_version").fetchone()[0],
                    17,
                )
            finally:
                upgraded.close()

            reopened = connect(path)
            try:
                versions = [
                    row["version"]
                    for row in reopened.execute(
                        """
                        SELECT version FROM schema_migrations
                        ORDER BY version
                        """
                    ).fetchall()
                ]
                self.assertEqual(versions, [14, 17])
                columns = {
                    row["name"]
                    for row in reopened.execute(
                        "PRAGMA table_info(cluster_jobs)"
                    ).fetchall()
                }
                self.assertIn("usage_missing_fields", columns)
                goal_columns = {
                    row["name"]
                    for row in reopened.execute(
                        "PRAGMA table_info(cluster_goals)"
                    ).fetchall()
                }
                self.assertTrue(
                    {
                        "stall_ticks",
                        "stall_signature",
                        "stalled_at",
                    }
                    <= goal_columns
                )
            finally:
                reopened.close()

    def test_schema_17_goal_stall_columns_migrate_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "goal-stall-migration.sqlite"
            connection = connect(path)
            connection.close()
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "ALTER TABLE cluster_goals DROP COLUMN stalled_at"
                )
                connection.execute(
                    "ALTER TABLE cluster_goals DROP COLUMN stall_signature"
                )
                connection.execute(
                    "ALTER TABLE cluster_goals DROP COLUMN stall_ticks"
                )
                connection.execute(
                    "DELETE FROM schema_migrations WHERE version = 17"
                )
                connection.execute("PRAGMA user_version = 16")
                connection.commit()
            finally:
                connection.close()

            for _ in range(2):
                upgraded = connect(path)
                try:
                    columns = {
                        row["name"]
                        for row in upgraded.execute(
                            "PRAGMA table_info(cluster_goals)"
                        ).fetchall()
                    }
                    self.assertTrue(
                        {
                            "stall_ticks",
                            "stall_signature",
                            "stalled_at",
                        }
                        <= columns
                    )
                    self.assertEqual(
                        upgraded.execute(
                            "PRAGMA user_version"
                        ).fetchone()[0],
                        SCHEMA_VERSION,
                    )
                finally:
                    upgraded.close()

    def test_archived_goal_done_tasks_are_backfilled_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "backfill.sqlite"
            connection = connect(path)
            try:
                now = "2026-11-07T00:00:00Z"
                connection.execute(
                    """
                    INSERT INTO projects(
                        project_id, name, root_path, created_at, updated_at
                    ) VALUES ('P1', 'Project', 'D:/project', ?, ?)
                    """,
                    (now, now),
                )
                connection.execute(
                    """
                    INSERT INTO cluster_goals(
                        goal_id, project_id, objective, status, autonomy,
                        created_at, updated_at, archived_at
                    ) VALUES ('G1', 'P1', 'Archived goal', 'completed', 'auto',
                              ?, ?, ?)
                    """,
                    (now, now, now),
                )
                connection.execute(
                    """
                    INSERT INTO tasks(
                        task_id, project_id, goal_id, title, description,
                        acceptance, kind, status, priority, owner_role,
                        created_at, updated_at
                    ) VALUES ('T1', 'P1', 'G1', 'Done task', 'Description',
                              'Acceptance', 'read', 'done', 2, 'team_explorer',
                              ?, ?)
                    """,
                    (now, now),
                )
                connection.execute(
                    """
                    INSERT INTO tasks(
                        task_id, project_id, goal_id, title, description,
                        acceptance, kind, status, priority, owner_role,
                        blocker, created_at, updated_at
                    ) VALUES ('T2', 'P1', 'G1', 'Blocked task', 'Description',
                              'Acceptance', 'read', 'blocked', 2,
                              'team_explorer', 'fixture blocker', ?, ?)
                    """,
                    (now, now),
                )
                connection.execute(
                    "DELETE FROM metadata WHERE key = ?",
                    ("goal_archive_done_backfill_v11",),
                )
                connection.execute(
                    "DELETE FROM metadata WHERE key = ?",
                    ("goal_archive_all_tasks_backfill_v16",),
                )
                connection.commit()
            finally:
                connection.close()

            upgraded = connect(path)
            try:
                tasks = upgraded.execute(
                    """
                    SELECT task_id, archived_at, archived_by_goal
                    FROM tasks WHERE task_id IN ('T1', 'T2')
                    ORDER BY task_id
                    """
                ).fetchall()
                for task in tasks:
                    self.assertIsNotNone(task["archived_at"])
                    self.assertEqual(task["archived_by_goal"], 1)
                events = [
                    row["event_type"]
                    for row in upgraded.execute(
                        """
                        SELECT event_type FROM task_events
                        WHERE task_id IN ('T1', 'T2')
                        ORDER BY task_id, event_id
                        """
                    ).fetchall()
                ]
                self.assertEqual(
                    events,
                    ["archived_by_goal", "archived_by_goal"],
                )
            finally:
                upgraded.close()

            reopened = connect(path)
            try:
                event_count = reopened.execute(
                    "SELECT COUNT(*) FROM task_events "
                    "WHERE task_id IN ('T1', 'T2')"
                ).fetchone()[0]
                self.assertEqual(event_count, 2)
            finally:
                reopened.close()


if __name__ == "__main__":
    unittest.main()
