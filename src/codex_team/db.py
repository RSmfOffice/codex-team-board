from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 11

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
    model_context_window INTEGER
);

CREATE TABLE IF NOT EXISTS cluster_agents (
    agent_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    workstream_key TEXT NOT NULL,
    display_name TEXT NOT NULL,
    thread_id TEXT NOT NULL UNIQUE,
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
CREATE INDEX IF NOT EXISTS idx_cluster_jobs_turn
    ON cluster_jobs(thread_id, turn_id);
CREATE INDEX IF NOT EXISTS idx_cluster_agent_events_agent
    ON cluster_agent_events(agent_id, event_id);
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

PROJECT_MIGRATION_COLUMNS = {
    "archived_at": "TEXT",
}

CLUSTER_GOAL_MIGRATION_COLUMNS = {
    "archived_at": "TEXT",
    "workstream_key": "TEXT",
    "continuity_reason": "TEXT NOT NULL DEFAULT ''",
}

CLUSTER_JOB_MIGRATION_COLUMNS = {
    "agent_id": "TEXT",
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
