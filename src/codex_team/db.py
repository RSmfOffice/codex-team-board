from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 19

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    root_path TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    goal_id TEXT REFERENCES cluster_goals(goal_id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    acceptance TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('read', 'write', 'review', 'ops')),
    status TEXT NOT NULL CHECK(status IN (
        'backlog', 'ready', 'running', 'review', 'blocked', 'done', 'canceled'
    )),
    priority INTEGER NOT NULL DEFAULT 2,
    owner_role TEXT NOT NULL,
    worktree_path TEXT,
    branch TEXT,
    target_branch TEXT,
    base_ref TEXT,
    base_commit TEXT,
    claimed_by TEXT,
    claim_token TEXT,
    lease_until TEXT,
    result TEXT NOT NULL DEFAULT '',
    tests TEXT NOT NULL DEFAULT '',
    commit_sha TEXT NOT NULL DEFAULT '',
    blocker TEXT NOT NULL DEFAULT '',
    review_notes TEXT NOT NULL DEFAULT '',
    merge_commit TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    continuity_mode TEXT NOT NULL DEFAULT 'auto',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    archived_at TEXT,
    archived_by_goal INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS task_dependencies (
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    depends_on TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, depends_on),
    CHECK(task_id != depends_on)
);

CREATE TABLE IF NOT EXISTS task_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    thread_id TEXT,
    job_id TEXT,
    correlation_id TEXT,
    assignment_id TEXT,
    data_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    task_id TEXT REFERENCES tasks(task_id) ON DELETE CASCADE,
    sender TEXT NOT NULL,
    recipient TEXT,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cluster_goals (
    goal_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    objective TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'planning', 'active', 'paused', 'completed', 'failed'
    )),
    autonomy TEXT NOT NULL CHECK(autonomy IN ('auto', 'supervised')),
    lead_thread_id TEXT,
    lead_generation INTEGER NOT NULL DEFAULT 0,
    lead_ticks INTEGER NOT NULL DEFAULT 0,
    lead_failures INTEGER NOT NULL DEFAULT 0,
    max_worker_attempts INTEGER NOT NULL DEFAULT 3,
    max_lead_attempts INTEGER NOT NULL DEFAULT 5,
    workstream_key TEXT,
    continuity_reason TEXT NOT NULL DEFAULT '',
    checkpoint_summary TEXT NOT NULL DEFAULT '',
    checkpoint_updated_at TEXT,
    stall_ticks INTEGER NOT NULL DEFAULT 0,
    stall_signature TEXT NOT NULL DEFAULT '',
    stalled_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    archived_at TEXT
);

CREATE TABLE IF NOT EXISTS cluster_jobs (
    job_id TEXT PRIMARY KEY,
    goal_id TEXT NOT NULL REFERENCES cluster_goals(goal_id) ON DELETE CASCADE,
    task_id TEXT REFERENCES tasks(task_id) ON DELETE SET NULL,
    kind TEXT NOT NULL CHECK(kind IN ('lead', 'worker')),
    role TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK(status IN (
        'starting', 'running', 'completed', 'failed', 'interrupted', 'lost'
    )),
    pid INTEGER,
    thread_id TEXT,
    attempt INTEGER NOT NULL DEFAULT 1,
    log_path TEXT NOT NULL,
    output_path TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    exit_code INTEGER,
    error TEXT NOT NULL DEFAULT '',
    agent_id TEXT REFERENCES cluster_agents(agent_id) ON DELETE SET NULL,
    turn_id TEXT,
    runtime TEXT NOT NULL DEFAULT 'exec',
    runtime_generation INTEGER,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    cached_input_tokens INTEGER NOT NULL DEFAULT 0,
    uncached_input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    first_input_tokens INTEGER NOT NULL DEFAULT 0,
    first_cached_input_tokens INTEGER NOT NULL DEFAULT 0,
    first_uncached_input_tokens INTEGER NOT NULL DEFAULT 0,
    context_tokens INTEGER NOT NULL DEFAULT 0,
    model_context_window INTEGER,
    cache_write_input_tokens INTEGER,
    reasoning_output_tokens INTEGER,
    usage_source TEXT,
    telemetry_complete INTEGER NOT NULL DEFAULT 0,
    usage_missing_fields TEXT NOT NULL DEFAULT '[]',
    correlation_id TEXT,
    prompt_hash TEXT NOT NULL DEFAULT '',
    cache_profile TEXT NOT NULL DEFAULT '',
    thread_action TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT '',
    settled_at TEXT
);

CREATE TABLE IF NOT EXISTS external_operations (
    operation_id TEXT PRIMARY KEY,
    operation_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'pending', 'running', 'completed', 'failed', 'compensated'
    )),
    goal_id TEXT REFERENCES cluster_goals(goal_id) ON DELETE CASCADE,
    task_id TEXT REFERENCES tasks(task_id) ON DELETE CASCADE,
    job_id TEXT REFERENCES cluster_jobs(job_id) ON DELETE CASCADE,
    actor TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    result_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS cluster_agents (
    agent_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    workstream_key TEXT NOT NULL,
    display_name TEXT NOT NULL,
    thread_id TEXT NOT NULL UNIQUE,
    runtime TEXT NOT NULL DEFAULT 'managed',
    parent_thread_id TEXT,
    agent_path TEXT,
    agent_nickname TEXT,
    actor TEXT,
    prompt_hash TEXT NOT NULL DEFAULT '',
    prompt_status TEXT NOT NULL DEFAULT 'unknown',
    native_session_path TEXT,
    last_seen_at TEXT,
    profile TEXT NOT NULL DEFAULT 'legacy',
    status TEXT NOT NULL CHECK(status IN (
        'starting', 'busy', 'loaded', 'idle', 'stored',
        'superseded', 'failed'
    )),
    model TEXT NOT NULL DEFAULT '',
    model_provider TEXT NOT NULL DEFAULT '',
    current_goal_id TEXT REFERENCES cluster_goals(goal_id) ON DELETE SET NULL,
    current_task_id TEXT REFERENCES tasks(task_id) ON DELETE SET NULL,
    current_job_id TEXT REFERENCES cluster_jobs(job_id) ON DELETE SET NULL,
    last_turn_id TEXT,
    model_context_window INTEGER,
    last_input_tokens INTEGER NOT NULL DEFAULT 0,
    last_cached_input_tokens INTEGER NOT NULL DEFAULT 0,
    last_uncached_input_tokens INTEGER NOT NULL DEFAULT 0,
    total_input_tokens INTEGER NOT NULL DEFAULT 0,
    total_cached_input_tokens INTEGER NOT NULL DEFAULT 0,
    total_uncached_input_tokens INTEGER NOT NULL DEFAULT 0,
    context_tokens INTEGER NOT NULL DEFAULT 0,
    turn_count INTEGER NOT NULL DEFAULT 0,
    last_used_at TEXT,
    last_loaded_at TEXT,
    last_unloaded_at TEXT,
    reset_requested_at TEXT,
    reset_reason TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT
);

CREATE TABLE IF NOT EXISTS cluster_agent_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL REFERENCES cluster_agents(agent_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    data_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cluster_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id TEXT NOT NULL REFERENCES cluster_goals(goal_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    data_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_tasks_project_status
    ON tasks(project_id, status, priority);
CREATE INDEX IF NOT EXISTS idx_tasks_claim
    ON tasks(claim_token);
CREATE INDEX IF NOT EXISTS idx_tasks_archived
    ON tasks(project_id, archived_at, status);
CREATE INDEX IF NOT EXISTS idx_tasks_goal_archive
    ON tasks(goal_id, status, archived_at);
CREATE INDEX IF NOT EXISTS idx_dependencies_depends_on
    ON task_dependencies(depends_on);
CREATE INDEX IF NOT EXISTS idx_events_task
    ON task_events(task_id, event_id);
CREATE INDEX IF NOT EXISTS idx_task_events_thread
    ON task_events(thread_id, event_id);
CREATE INDEX IF NOT EXISTS idx_task_events_correlation
    ON task_events(correlation_id, event_id);
CREATE INDEX IF NOT EXISTS idx_messages_project
    ON messages(project_id, message_id);
CREATE INDEX IF NOT EXISTS idx_cluster_jobs_goal
    ON cluster_jobs(goal_id, status, job_id);
CREATE INDEX IF NOT EXISTS idx_cluster_events_goal
    ON cluster_events(goal_id, event_id);
CREATE INDEX IF NOT EXISTS idx_cluster_goals_archived
    ON cluster_goals(project_id, archived_at, updated_at);
CREATE INDEX IF NOT EXISTS idx_cluster_agents_pool
    ON cluster_agents(project_id, role, workstream_key, status, last_used_at);
CREATE INDEX IF NOT EXISTS idx_cluster_agents_runtime
    ON cluster_agents(runtime, status, project_id);
CREATE INDEX IF NOT EXISTS idx_cluster_agents_actor
    ON cluster_agents(actor, project_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cluster_agents_native_actor
    ON cluster_agents(project_id, actor)
    WHERE runtime = 'native' AND actor IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_cluster_jobs_turn
    ON cluster_jobs(thread_id, turn_id);
CREATE INDEX IF NOT EXISTS idx_cluster_jobs_agent_started
    ON cluster_jobs(agent_id, started_at DESC, job_id DESC);
CREATE INDEX IF NOT EXISTS idx_cluster_jobs_correlation
    ON cluster_jobs(correlation_id);
CREATE INDEX IF NOT EXISTS idx_cluster_agent_events_agent
    ON cluster_agent_events(agent_id, event_id);
CREATE INDEX IF NOT EXISTS idx_external_operations_status
    ON external_operations(operation_type, status, updated_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_external_one_active_task_operation
    ON external_operations(operation_type, task_id)
    WHERE task_id IS NOT NULL AND status IN ('pending', 'running');
CREATE UNIQUE INDEX IF NOT EXISTS idx_cluster_one_active_goal
    ON cluster_goals(project_id)
    WHERE status IN ('planning', 'active');
CREATE UNIQUE INDEX IF NOT EXISTS idx_cluster_one_active_task_job
    ON cluster_jobs(task_id)
    WHERE task_id IS NOT NULL AND status IN ('starting', 'running');
CREATE UNIQUE INDEX IF NOT EXISTS idx_cluster_one_active_lead_job
    ON cluster_jobs(goal_id)
    WHERE kind = 'lead' AND status IN ('starting', 'running');
"""

TASK_MIGRATION_COLUMNS = {
    "worktree_path": "TEXT",
    "branch": "TEXT",
    "target_branch": "TEXT",
    "goal_id": "TEXT",
    "base_ref": "TEXT",
    "base_commit": "TEXT",
    "claimed_by": "TEXT",
    "claim_token": "TEXT",
    "lease_until": "TEXT",
    "result": "TEXT NOT NULL DEFAULT ''",
    "tests": "TEXT NOT NULL DEFAULT ''",
    "commit_sha": "TEXT NOT NULL DEFAULT ''",
    "blocker": "TEXT NOT NULL DEFAULT ''",
    "review_notes": "TEXT NOT NULL DEFAULT ''",
    "merge_commit": "TEXT NOT NULL DEFAULT ''",
    "version": "INTEGER NOT NULL DEFAULT 1",
    "completed_at": "TEXT",
    "archived_at": "TEXT",
    "archived_by_goal": "INTEGER NOT NULL DEFAULT 0",
    "continuity_mode": "TEXT NOT NULL DEFAULT 'auto'",
}

TASK_EVENT_MIGRATION_COLUMNS = {
    "thread_id": "TEXT",
    "job_id": "TEXT",
    "correlation_id": "TEXT",
    "assignment_id": "TEXT",
}

PROJECT_MIGRATION_COLUMNS = {
    "archived_at": "TEXT",
}

CLUSTER_GOAL_MIGRATION_COLUMNS = {
    "archived_at": "TEXT",
    "workstream_key": "TEXT",
    "continuity_reason": "TEXT NOT NULL DEFAULT ''",
    "checkpoint_summary": "TEXT NOT NULL DEFAULT ''",
    "checkpoint_updated_at": "TEXT",
    "stall_ticks": "INTEGER NOT NULL DEFAULT 0",
    "stall_signature": "TEXT NOT NULL DEFAULT ''",
    "stalled_at": "TEXT",
}

CLUSTER_JOB_MIGRATION_COLUMNS = {
    "agent_id": "TEXT",
    "actor": "TEXT NOT NULL DEFAULT ''",
    "turn_id": "TEXT",
    "runtime": "TEXT NOT NULL DEFAULT 'exec'",
    "runtime_generation": "INTEGER",
    "input_tokens": "INTEGER NOT NULL DEFAULT 0",
    "cached_input_tokens": "INTEGER NOT NULL DEFAULT 0",
    "uncached_input_tokens": "INTEGER NOT NULL DEFAULT 0",
    "output_tokens": "INTEGER NOT NULL DEFAULT 0",
    "first_input_tokens": "INTEGER NOT NULL DEFAULT 0",
    "first_cached_input_tokens": "INTEGER NOT NULL DEFAULT 0",
    "first_uncached_input_tokens": "INTEGER NOT NULL DEFAULT 0",
    "context_tokens": "INTEGER NOT NULL DEFAULT 0",
    "model_context_window": "INTEGER",
    "cache_write_input_tokens": "INTEGER",
    "reasoning_output_tokens": "INTEGER",
    "usage_source": "TEXT",
    "telemetry_complete": "INTEGER NOT NULL DEFAULT 0",
    "usage_missing_fields": "TEXT NOT NULL DEFAULT '[]'",
    "correlation_id": "TEXT",
    "prompt_hash": "TEXT NOT NULL DEFAULT ''",
    "cache_profile": "TEXT NOT NULL DEFAULT ''",
    "thread_action": "TEXT NOT NULL DEFAULT ''",
    "outcome": "TEXT NOT NULL DEFAULT ''",
    "settled_at": "TEXT",
}

CLUSTER_AGENT_MIGRATION_COLUMNS = {
    "profile": "TEXT NOT NULL DEFAULT 'legacy'",
    "runtime": "TEXT NOT NULL DEFAULT 'managed'",
    "parent_thread_id": "TEXT",
    "agent_path": "TEXT",
    "agent_nickname": "TEXT",
    "actor": "TEXT",
    "prompt_hash": "TEXT NOT NULL DEFAULT ''",
    "prompt_status": "TEXT NOT NULL DEFAULT 'unknown'",
    "native_session_path": "TEXT",
    "last_seen_at": "TEXT",
}


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    connection.executescript(SCHEMA)
    _upgrade_schema(connection)
    connection.executescript(INDEXES)
    connection.execute(
        """
        INSERT OR IGNORE INTO schema_migrations(version, applied_at)
        VALUES (?, ?)
        """,
        (SCHEMA_VERSION, utc_now()),
    )
    connection.execute(
        "INSERT OR REPLACE INTO metadata(key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    connection.commit()
    return connection


def connect_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro",
        uri=True,
        timeout=10,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def effective_schema_version(connection: sqlite3.Connection) -> int:
    """Return the highest applied schema, tolerant of stale writers."""

    try:
        migrated = connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()[0]
    except sqlite3.OperationalError:
        migrated = 0
    pragma = connection.execute("PRAGMA user_version").fetchone()[0]
    return max(int(migrated or 0), int(pragma or 0))


def _upgrade_schema(connection: sqlite3.Connection) -> None:
    existing = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
    }
    for name, declaration in TASK_MIGRATION_COLUMNS.items():
        if name not in existing:
            connection.execute(
                f"ALTER TABLE tasks ADD COLUMN {name} {declaration}"
            )
    event_columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(task_events)"
        ).fetchall()
    }
    for name, declaration in TASK_EVENT_MIGRATION_COLUMNS.items():
        if name not in event_columns:
            connection.execute(
                f"ALTER TABLE task_events ADD COLUMN {name} {declaration}"
            )
    project_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(projects)").fetchall()
    }
    for name, declaration in PROJECT_MIGRATION_COLUMNS.items():
        if name not in project_columns:
            connection.execute(
                f"ALTER TABLE projects ADD COLUMN {name} {declaration}"
            )
    cluster_goal_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(cluster_goals)").fetchall()
    }
    for name, declaration in CLUSTER_GOAL_MIGRATION_COLUMNS.items():
        if name not in cluster_goal_columns:
            connection.execute(
                f"ALTER TABLE cluster_goals ADD COLUMN {name} {declaration}"
            )
    cluster_job_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(cluster_jobs)").fetchall()
    }
    for name, declaration in CLUSTER_JOB_MIGRATION_COLUMNS.items():
        if name not in cluster_job_columns:
            connection.execute(
                f"ALTER TABLE cluster_jobs ADD COLUMN {name} {declaration}"
            )
    cluster_agent_columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(cluster_agents)"
        ).fetchall()
    }
    for name, declaration in CLUSTER_AGENT_MIGRATION_COLUMNS.items():
        if name not in cluster_agent_columns:
            connection.execute(
                f"ALTER TABLE cluster_agents ADD COLUMN {name} {declaration}"
            )
    connection.execute(
        """
        UPDATE cluster_jobs
        SET correlation_id = COALESCE(correlation_id, job_id),
            outcome = CASE
                WHEN status IN ('completed', 'failed', 'interrupted', 'lost')
                     AND TRIM(outcome) = ''
                THEN status
                ELSE outcome
            END,
            settled_at = CASE
                WHEN status IN ('completed', 'failed', 'interrupted', 'lost')
                     AND settled_at IS NULL
                THEN started_at
                ELSE settled_at
            END
        """
    )
    connection.execute(
        """
        UPDATE cluster_agents
        SET profile = CASE
                WHEN profile IS NULL OR TRIM(profile) = '' THEN 'legacy'
                ELSE profile
            END,
            runtime = CASE
                WHEN runtime IS NULL OR TRIM(runtime) = '' THEN 'managed'
                ELSE runtime
            END,
            prompt_status = CASE
                WHEN prompt_status IS NULL OR TRIM(prompt_status) = ''
                THEN 'unknown'
                ELSE prompt_status
            END
        """
    )
    _backfill_goal_archive_tasks(connection)
    _backfill_goal_archive_all_tasks(connection)


def _backfill_goal_archive_tasks(connection: sqlite3.Connection) -> None:
    marker = "goal_archive_done_backfill_v11"
    exists = connection.execute(
        "SELECT 1 FROM metadata WHERE key = ?",
        (marker,),
    ).fetchone()
    if exists is not None:
        return
    now = utc_now()
    rows = connection.execute(
        """
        SELECT t.task_id, t.goal_id
        FROM tasks t
        JOIN cluster_goals g ON g.goal_id = t.goal_id
        WHERE t.status = 'done'
          AND t.archived_at IS NULL
          AND g.archived_at IS NOT NULL
        ORDER BY t.task_id
        """
    ).fetchall()
    for row in rows:
        task_id = str(row["task_id"])
        goal_id = str(row["goal_id"])
        updated = connection.execute(
            """
            UPDATE tasks
            SET archived_at = ?, archived_by_goal = 1,
                updated_at = ?, version = version + 1
            WHERE task_id = ? AND status = 'done'
              AND archived_at IS NULL
            """,
            (now, now, task_id),
        )
        if updated.rowcount != 1:
            continue
        connection.execute(
            """
            INSERT INTO task_events(
                task_id, event_type, actor, data_json, created_at
            ) VALUES (?, 'archived_by_goal', 'schema-migration', ?, ?)
            """,
            (
                task_id,
                json.dumps(
                    {"goal_id": goal_id},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                now,
            ),
        )
    connection.execute(
        "INSERT INTO metadata(key, value) VALUES (?, ?)",
        (marker, now),
    )


def _backfill_goal_archive_all_tasks(connection: sqlite3.Connection) -> None:
    marker = "goal_archive_all_tasks_backfill_v16"
    exists = connection.execute(
        "SELECT 1 FROM metadata WHERE key = ?",
        (marker,),
    ).fetchone()
    if exists is not None:
        return
    now = utc_now()
    rows = connection.execute(
        """
        SELECT t.task_id, t.goal_id
        FROM tasks t
        JOIN cluster_goals g ON g.goal_id = t.goal_id
        WHERE t.archived_at IS NULL
          AND g.archived_at IS NOT NULL
        ORDER BY t.task_id
        """
    ).fetchall()
    for row in rows:
        task_id = str(row["task_id"])
        goal_id = str(row["goal_id"])
        updated = connection.execute(
            """
            UPDATE tasks
            SET archived_at = ?, archived_by_goal = 1,
                updated_at = ?, version = version + 1
            WHERE task_id = ? AND archived_at IS NULL
            """,
            (now, now, task_id),
        )
        if updated.rowcount != 1:
            continue
        connection.execute(
            """
            INSERT INTO task_events(
                task_id, event_type, actor, data_json, created_at
            ) VALUES (?, 'archived_by_goal', 'schema-migration', ?, ?)
            """,
            (
                task_id,
                json.dumps(
                    {"goal_id": goal_id},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                now,
            ),
        )
    connection.execute(
        "INSERT INTO metadata(key, value) VALUES (?, ?)",
        (marker, now),
    )
