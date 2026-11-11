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
                        "review_target_commit",
                        "target_branch",
                        "goal_id",
                        "workflow_epoch",
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
                self.assertIn("workflow_epoch", cluster_goal_columns)
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
                        "workflow_epoch",
                        "process_creation_time",
                        "process_executable",
                        "process_instance_id",
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

    def test_current_schema_keeps_usage_presence_and_archive_migrations_idempotent(self) -> None:
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
                    SELECT usage_missing_fields, correlation_id, outcome,
                           settled_at, workflow_epoch
                    FROM cluster_jobs WHERE job_id = 'J-OLD'
                    """
                ).fetchone()
                self.assertEqual(old["usage_missing_fields"], "[]")
                self.assertEqual(old["correlation_id"], "J-OLD")
                self.assertEqual(old["outcome"], "completed")
                self.assertEqual(old["settled_at"], "2026-11-08T00:00:00Z")
                self.assertEqual(old["workflow_epoch"], "legacy-unbound")
                self.assertEqual(
                    upgraded.execute("PRAGMA user_version").fetchone()[0],
                    SCHEMA_VERSION,
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
                self.assertEqual(versions, [14, SCHEMA_VERSION])
                columns = {
                    row["name"]
                    for row in reopened.execute(
                        "PRAGMA table_info(cluster_jobs)"
                    ).fetchall()
                }
                self.assertIn("usage_missing_fields", columns)
                self.assertTrue(
                    {
                        "correlation_id",
                        "prompt_hash",
                        "cache_profile",
                        "thread_action",
                        "outcome",
                        "settled_at",
                        "workflow_epoch",
                        "process_creation_time",
                        "process_executable",
                        "process_instance_id",
                    }.issubset(columns)
                )
                operation_table = reopened.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type = 'table' AND name = 'external_operations'
                    """
                ).fetchone()
                self.assertIsNotNone(operation_table)
                receipt_columns = {
                    row["name"]
                    for row in reopened.execute(
                        "PRAGMA table_info(request_receipts)"
                    ).fetchall()
                }
                self.assertEqual(
                    {
                        "request_id",
                        "operation",
                        "payload_hash",
                        "status",
                        "result_json",
                        "error",
                        "created_at",
                        "updated_at",
                    },
                    receipt_columns,
                )
                event_columns = {
                    row["name"]
                    for row in reopened.execute(
                        "PRAGMA table_info(task_events)"
                    ).fetchall()
                }
                self.assertTrue(
                    {
                        "thread_id",
                        "job_id",
                        "correlation_id",
                        "assignment_id",
                    }.issubset(event_columns)
                )
                agent_columns = {
                    row["name"]
                    for row in reopened.execute(
                        "PRAGMA table_info(cluster_agents)"
                    ).fetchall()
                }
                self.assertTrue(
                    {
                        "runtime",
                        "parent_thread_id",
                        "agent_path",
                        "agent_nickname",
                        "actor",
                        "prompt_hash",
                        "prompt_status",
                        "native_session_path",
                        "last_seen_at",
                        "identity_source",
                        "identity_confidence",
                        "lifecycle",
                        "lifecycle_source",
                        "assignment_source",
                        "assignment_confidence",
                        "task_candidates_json",
                        "task_assignment_ambiguous",
                        "session_stale",
                        "native_usage_status",
                        "native_usage_missing_fields",
                    }.issubset(agent_columns)
                )
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
                        "workflow_epoch",
                    }
                    <= goal_columns
                )
            finally:
                reopened.close()

    def test_schema_20_active_job_gains_null_process_identity_idempotently(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "schema-20-process-identity.sqlite"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                INSERT INTO schema_migrations(version, applied_at)
                VALUES (20, '2026-11-10T00:00:00Z');
                CREATE TABLE cluster_jobs (
                    job_id TEXT PRIMARY KEY,
                    goal_id TEXT NOT NULL,
                    task_id TEXT,
                    kind TEXT NOT NULL,
                    role TEXT NOT NULL,
                    status TEXT NOT NULL,
                    pid INTEGER,
                    thread_id TEXT,
                    log_path TEXT NOT NULL,
                    output_path TEXT NOT NULL,
                    started_at TEXT NOT NULL
                );
                INSERT INTO cluster_jobs(
                    job_id, goal_id, kind, role, status, pid,
                    log_path, output_path, started_at
                ) VALUES (
                    'J-ACTIVE', 'G-20', 'worker', 'team_worker', 'running',
                    4242, 'active.log', 'active.out', '2026-11-10T00:00:00Z'
                );
                PRAGMA user_version = 20;
                """
            )
            connection.commit()
            connection.close()

            for _ in range(2):
                upgraded = connect(path)
                try:
                    active = upgraded.execute(
                        """
                        SELECT status, pid, process_creation_time,
                               process_executable, process_instance_id
                        FROM cluster_jobs WHERE job_id = 'J-ACTIVE'
                        """
                    ).fetchone()
                    self.assertEqual(active["status"], "running")
                    self.assertEqual(active["pid"], 4242)
                    self.assertIsNone(active["process_creation_time"])
                    self.assertIsNone(active["process_executable"])
                    self.assertIsNone(active["process_instance_id"])
                    self.assertEqual(
                        upgraded.execute(
                            "PRAGMA user_version"
                        ).fetchone()[0],
                        SCHEMA_VERSION,
                    )
                finally:
                    upgraded.close()

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

    def test_schema_24_native_identity_migration_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "native-identity-v23.sqlite"
            connection = connect(path)
            try:
                now = "2026-11-11T00:00:00Z"
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
                    INSERT INTO tasks(
                        task_id, project_id, title, description, acceptance,
                        kind, status, priority, owner_role, created_at, updated_at
                    ) VALUES (
                        'T1', 'P1', 'Native task', 'Migration fixture',
                        'Fields are backfilled', 'read', 'ready', 1,
                        'team_explorer', ?, ?
                    )
                    """,
                    (now, now),
                )
                connection.execute(
                    """
                    INSERT INTO cluster_agents(
                        agent_id, project_id, role, workstream_key, display_name,
                        thread_id, runtime, status, current_task_id, turn_count,
                        created_at, updated_at
                    ) VALUES (
                        'A-NATIVE', 'P1', 'team_explorer', 'native',
                        'Native Explorer', 'native-thread', 'native', 'loaded',
                        'T1', 2, ?, ?
                    )
                    """,
                    (now, now),
                )
                for column in (
                    "identity_source",
                    "identity_confidence",
                    "lifecycle",
                    "lifecycle_source",
                    "assignment_source",
                    "assignment_confidence",
                    "task_candidates_json",
                    "task_assignment_ambiguous",
                    "session_stale",
                    "native_usage_status",
                    "native_usage_missing_fields",
                ):
                    connection.execute(
                        f"ALTER TABLE cluster_agents DROP COLUMN {column}"
                    )
                connection.execute(
                    "DELETE FROM schema_migrations WHERE version = ?",
                    (SCHEMA_VERSION,),
                )
                connection.execute("PRAGMA user_version = 23")
                connection.commit()
            finally:
                connection.close()

            upgraded = connect(path)
            try:
                native = upgraded.execute(
                    """
                    SELECT identity_source, identity_confidence,
                           assignment_source, assignment_confidence,
                           task_candidates_json, native_usage_status
                    FROM cluster_agents
                    WHERE agent_id = 'A-NATIVE'
                    """
                ).fetchone()
                self.assertEqual(native["identity_source"], "registry")
                self.assertEqual(
                    native["identity_confidence"],
                    "medium",
                )
                self.assertEqual(native["assignment_source"], "registry")
                self.assertEqual(
                    native["assignment_confidence"],
                    "medium",
                )
                self.assertEqual(
                    native["task_candidates_json"],
                    '["T1"]',
                )
                self.assertEqual(
                    native["native_usage_status"],
                    "partial",
                )
                upgraded.execute(
                    """
                    UPDATE cluster_agents
                    SET identity_source = 'agent_path_inference',
                        identity_confidence = 'low',
                        assignment_source = 'none',
                        assignment_confidence = 'unknown',
                        task_candidates_json = '[]',
                        native_usage_status = 'unavailable'
                    WHERE agent_id = 'A-NATIVE'
                    """
                )
                upgraded.commit()
            finally:
                upgraded.close()

            reopened = connect(path)
            try:
                native = reopened.execute(
                    """
                    SELECT identity_source, identity_confidence,
                           assignment_source, assignment_confidence,
                           task_candidates_json, native_usage_status
                    FROM cluster_agents
                    WHERE agent_id = 'A-NATIVE'
                    """
                ).fetchone()
                self.assertEqual(
                    native["identity_source"],
                    "agent_path_inference",
                )
                self.assertEqual(native["identity_confidence"], "low")
                self.assertEqual(native["assignment_source"], "none")
                self.assertEqual(
                    native["assignment_confidence"],
                    "unknown",
                )
                self.assertEqual(native["task_candidates_json"], "[]")
                self.assertEqual(
                    native["native_usage_status"],
                    "unavailable",
                )
                self.assertEqual(
                    reopened.execute(
                        "PRAGMA user_version"
                    ).fetchone()[0],
                    SCHEMA_VERSION,
                )
            finally:
                reopened.close()

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
