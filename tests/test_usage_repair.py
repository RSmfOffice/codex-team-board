from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_team.config import Settings
from codex_team.db import SCHEMA_VERSION, connect, utc_now
from codex_team.mcp import (
    _guard_tool_call,
    _visible_tool_definitions,
)
from codex_team.service import TeamService
from codex_team.usage import (
    cache_hit_rate,
    usage_from_exec_log,
    usage_is_cost_complete,
)
from codex_team.usage_backfill import backfill_exec_usage


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


class UsageRepairTests(unittest.TestCase):
    def test_exec_log_normalizes_first_and_final_usage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "job.jsonl"
            log.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "thread.started",
                                "thread_id": "thread-1",
                            }
                        ),
                        json.dumps(
                            {
                                "type": "turn.completed",
                                "usage": {
                                    "input_tokens": 100,
                                    "cached_input_tokens": 80,
                                    "cache_write_input_tokens": 0,
                                    "output_tokens": 10,
                                    "reasoning_output_tokens": 3,
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "turn.completed",
                                "usage": {
                                    "input_tokens": 250,
                                    "cached_input_tokens": 210,
                                    "cache_write_input_tokens": 2,
                                    "output_tokens": 25,
                                    "reasoning_output_tokens": 8,
                                },
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            usage = usage_from_exec_log(log)
        self.assertEqual(usage["input_tokens"], 250)
        self.assertEqual(usage["cached_input_tokens"], 210)
        self.assertEqual(usage["uncached_input_tokens"], 40)
        self.assertEqual(usage["output_tokens"], 25)
        self.assertEqual(usage["reasoning_output_tokens"], 8)
        self.assertEqual(usage["first_usage"]["input_tokens"], 100)
        self.assertTrue(usage_is_cost_complete(usage))
        self.assertAlmostEqual(cache_hit_rate(usage) or 0, 0.84)

    def test_missing_usage_is_not_reported_as_zero(self) -> None:
        self.assertIsNone(cache_hit_rate({}))
        self.assertFalse(usage_is_cost_complete({}))

    def test_schema_and_backfill_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repo = base / "repo"
            repo.mkdir()
            _git(repo, "init")
            _git(repo, "config", "user.email", "test@example.com")
            _git(repo, "config", "user.name", "Test")
            (repo / "README.md").write_text("test\n", encoding="utf-8")
            _git(repo, "add", "README.md")
            _git(repo, "commit", "-m", "init")
            settings = Settings(
                project_root=base,
                home=base / "home",
                db_path=base / "home" / "team.sqlite",
                logs_dir=base / "home" / "logs",
            )
            team = TeamService(settings)
            project = team.register_project(repo)
            log_path = base / "exec.jsonl"
            log_path.write_text(
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 120,
                            "cached_input_tokens": 90,
                            "output_tokens": 12,
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            now = utc_now()
            connection = connect(settings.db_path)
            try:
                connection.execute(
                    """
                    INSERT INTO cluster_goals(
                        goal_id, project_id, objective, status, autonomy,
                        created_at, updated_at
                    ) VALUES ('G-TEST', ?, 'test', 'paused', 'auto', ?, ?)
                    """,
                    (project["project_id"], now, now),
                )
                connection.execute(
                    """
                    INSERT INTO cluster_jobs(
                        job_id, goal_id, kind, role, status, log_path,
                        output_path, started_at
                    ) VALUES (
                        'J-TEST', 'G-TEST', 'worker', 'team_worker',
                        'completed', ?, '', ?
                    )
                    """,
                    (str(log_path), now),
                )
                columns = {
                    row["name"]
                    for row in connection.execute(
                        "PRAGMA table_info(cluster_jobs)"
                    )
                }
                connection.commit()
            finally:
                connection.close()
            self.assertGreaterEqual(SCHEMA_VERSION, 12)
            self.assertTrue(
                {
                    "cache_write_input_tokens",
                    "reasoning_output_tokens",
                    "usage_source",
                    "telemetry_complete",
                }
                <= columns
            )
            dry_run = backfill_exec_usage(settings, dry_run=True)
            self.assertEqual(dry_run["would_update_count"], 1)
            applied = backfill_exec_usage(settings, dry_run=False)
            self.assertEqual(applied["updated_count"], 1)
            repeated = backfill_exec_usage(settings, dry_run=False)
            self.assertEqual(repeated["updated_count"], 0)
            connection = connect(settings.db_path)
            try:
                row = connection.execute(
                    "SELECT * FROM cluster_jobs WHERE job_id = 'J-TEST'"
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(row["input_tokens"], 120)
            self.assertEqual(row["cached_input_tokens"], 90)
            self.assertEqual(row["uncached_input_tokens"], 30)
            self.assertEqual(row["output_tokens"], 12)
            self.assertEqual(row["telemetry_complete"], 1)

    def test_mcp_access_modes_and_allowed_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            inside = root / "repo"
            outside = root.parent / "outside"
            with patch.dict(
                os.environ,
                {"CODEX_TEAM_MCP_ACCESS": "off"},
                clear=False,
            ):
                self.assertEqual(_visible_tool_definitions(), [])
            with patch.dict(
                os.environ,
                {"CODEX_TEAM_MCP_ACCESS": "read_only"},
                clear=False,
            ):
                names = {
                    tool["name"] for tool in _visible_tool_definitions()
                }
                self.assertIn("team_status", names)
                self.assertNotIn("team_create_task", names)
                with self.assertRaisesRegex(Exception, "read-only"):
                    _guard_tool_call("team_create_task", {})
            with patch.dict(
                os.environ,
                {
                    "CODEX_TEAM_MCP_ACCESS": "full",
                    "CODEX_TEAM_ALLOWED_ROOTS": str(root),
                },
                clear=False,
            ):
                _guard_tool_call("team_create_task", {"repo_root": str(inside)})
                with self.assertRaisesRegex(Exception, "outside"):
                    _guard_tool_call(
                        "team_create_task",
                        {"repo_root": str(outside)},
                    )


if __name__ == "__main__":
    unittest.main()
