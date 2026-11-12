"""Regression tests for hardening round 1.

Covers:
- service-layer privileged authority token enforcement
- MCP crash recovery returning the live claim token
- nested App Server usage evidence detection
- fenced goal status transitions
- redacted MCP protocol errors
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_team.cluster import ClusterManager, _usage_state_has_evidence
from codex_team.config import Settings
from codex_team.mcp import TeamMcpServer, _process
from codex_team.service import TeamError, TeamService
from codex_team.worktrees import WorktreeManager


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


class HardeningRound1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        git(self.repo, "init")
        git(self.repo, "config", "user.email", "team-test@example.invalid")
        git(self.repo, "config", "user.name", "Team Test")
        (self.repo / "README.md").write_text("# Test\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "initial")
        self.settings = Settings(
            project_root=Path(__file__).resolve().parents[1],
            home=self.base / "team-home",
            db_path=self.base / "team-home" / "team.sqlite",
            logs_dir=self.base / "team-home" / "logs",
        )
        self.team = TeamService(self.settings)
        self.worktrees = WorktreeManager(self.team)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _read_task(self, title: str) -> dict[str, object]:
        return self.team.create_task(
            self.repo,
            title=title,
            description="Hardening regression fixture.",
            acceptance="The invariant holds.",
            kind="read",
            owner_role="team_explorer",
        )

    def test_service_privileged_paths_enforce_authority_token(self) -> None:
        task = self._read_task("Service forged review")
        claimed = self.team.claim_task(
            str(task["task_id"]), actor="service-victim"
        )
        self.team.submit_task(
            str(task["task_id"]),
            actor="service-victim",
            claim_token=str(claimed["claim_token"]),
            result="ready for review",
            tests="hardening fixture",
        )
        with patch.dict(
            os.environ,
            {"CODEX_TEAM_ROLE": "", "CODEX_TEAM_ACTOR": ""},
            clear=False,
        ):
            with self.assertRaisesRegex(TeamError, "local authority token"):
                self.team.review_task(
                    str(task["task_id"]),
                    actor="lead",
                    approve=True,
                    notes="forged service-level review",
                    authority_token="forged-token-value",
                )
            reviewed = self.team.review_task(
                str(task["task_id"]),
                actor="lead",
                approve=True,
                notes="authorized service-level review",
                authority_token=self.team.operator_authority(),
            )
        self.assertEqual(reviewed["status"], "done")

        archive_task = self._read_task("Service forged archive")
        with patch.dict(
            os.environ,
            {"CODEX_TEAM_ROLE": "", "CODEX_TEAM_ACTOR": ""},
            clear=False,
        ):
            with self.assertRaisesRegex(TeamError, "local authority token"):
                self.team.set_task_archived(
                    str(archive_task["task_id"]),
                    True,
                    actor="human",
                    authority_token="forged-token-value",
                )
            archived = self.team.set_task_archived(
                str(archive_task["task_id"]),
                True,
                actor="human",
                authority_token=self.team.operator_authority(),
            )
        self.assertIsNotNone(archived["archived_at"])

    def test_managed_role_binding_still_authorizes_without_token(self) -> None:
        task = self._read_task("Managed lead review")
        claimed = self.team.claim_task(
            str(task["task_id"]), actor="managed-victim"
        )
        self.team.submit_task(
            str(task["task_id"]),
            actor="managed-victim",
            claim_token=str(claimed["claim_token"]),
            result="ready for review",
            tests="hardening fixture",
        )
        with patch.dict(
            os.environ,
            {"CODEX_TEAM_ROLE": "lead", "CODEX_TEAM_ACTOR": "auto-lead"},
            clear=False,
        ):
            reviewed = self.team.review_task(
                str(task["task_id"]),
                actor="lead",
                approve=True,
                notes="managed lead review",
            )
        self.assertEqual(reviewed["status"], "done")

    def test_mcp_claim_recovery_returns_live_token(self) -> None:
        mcp = TeamMcpServer(self.settings)
        task = self._read_task("Claim recovery token")
        payload = {
            "task_id": str(task["task_id"]),
            "actor": "recovery-worker",
        }

        def crash_before_receipt_finish(*args: object, **kwargs: object) -> None:
            raise OSError("simulated process death after claim commit")

        mcp._finish_request_receipt = (  # type: ignore[method-assign]
            crash_before_receipt_finish
        )
        with self.assertRaisesRegex(OSError, "claim commit"):
            mcp.call_tool(
                "team_claim_task",
                payload,
                request_id="crash-safe-claim",
            )

        retry_server = TeamMcpServer(self.settings)
        recovered = retry_server.call_tool(
            "team_claim_task",
            payload,
            request_id="crash-safe-claim",
        )
        token = recovered.get("claim_token")
        self.assertIsInstance(token, str)
        self.assertTrue(token)
        heartbeat = self.team.heartbeat(
            str(task["task_id"]),
            claim_token=str(token),
            actor="recovery-worker",
        )
        self.assertEqual(heartbeat["status"], "running")

    def test_nested_usage_state_evidence(self) -> None:
        nested_total = {
            "usage": {"total": {"input": 10, "cached": 4}, "last": None}
        }
        nested_last = {
            "usage": {"total": None, "last": {"input": 7, "cached": 2}}
        }
        flat = {"usage": {"input_tokens": 5, "cached_input_tokens": 1}}
        empty_nested = {"usage": {"total": None, "last": None}}
        self.assertTrue(_usage_state_has_evidence(nested_total))
        self.assertTrue(_usage_state_has_evidence(nested_last))
        self.assertTrue(_usage_state_has_evidence(flat))
        self.assertFalse(_usage_state_has_evidence(empty_nested))
        self.assertFalse(_usage_state_has_evidence({}))
        self.assertFalse(_usage_state_has_evidence(None))

    def test_goal_status_update_is_fenced(self) -> None:
        manager = ClusterManager(self.settings, team=self.team)
        goal = manager.create_goal(
            self.repo,
            objective="Fenced status transition",
            autonomy="auto",
        )
        goal_id = str(goal["goal_id"])
        db_path = self.settings.db_path
        original_connection = self.team.connection
        observed: dict[str, object] = {}

        def racing_connection(*args: object, **kwargs: object):
            context = original_connection(*args, **kwargs)

            class RacingConnection:
                def __enter__(self):
                    self.connection = context.__enter__()
                    return self.connection

                def __exit__(self, exc_type, exc, traceback):
                    return context.__exit__(exc_type, exc, traceback)

            class RacingCursor:
                def __init__(self, cursor: object) -> None:
                    self._cursor = cursor

                def fetchone(self):
                    row = self._cursor.fetchone()
                    if "race" not in observed:
                        # The transition holds BEGIN IMMEDIATE. A competing
                        # writer must be blocked by the write lock instead of
                        # interleaving between the read and the update.
                        writer = sqlite3.connect(db_path, timeout=0.25)
                        try:
                            writer.execute(
                                "UPDATE cluster_goals SET status = 'paused' "
                                "WHERE goal_id = ?",
                                (goal_id,),
                            )
                            writer.commit()
                            observed["race"] = "committed"
                        except sqlite3.OperationalError as exc:
                            observed["race"] = str(exc)
                        finally:
                            writer.close()
                    return row

                def __getattr__(self, name: str):
                    return getattr(self._cursor, name)

            class RacingConnectionProxy:
                def __init__(self, racing: RacingConnection) -> None:
                    self._racing = racing

                def __enter__(self):
                    self._racing.__enter__()
                    return self

                def __exit__(self, exc_type, exc, traceback):
                    return self._racing.__exit__(exc_type, exc, traceback)

                def execute(self, sql: str, *args: object):
                    cursor = self._racing.connection.execute(sql, *args)
                    if sql.strip().upper().startswith("SELECT"):
                        return RacingCursor(cursor)
                    return cursor

                def __getattr__(self, name: str):
                    return getattr(self._racing.connection, name)

            return RacingConnectionProxy(RacingConnection())

        with patch.object(
            self.team, "connection", side_effect=racing_connection
        ):
            updated = manager.set_goal_status(goal_id, "active")
        self.assertIn("locked", str(observed.get("race", "")).lower())
        self.assertEqual(updated["raw_status"], "active")
        current = manager.get_goal(goal_id)
        self.assertEqual(current["raw_status"], "active")

    def test_mcp_protocol_errors_are_redacted(self) -> None:
        server = TeamMcpServer(self.settings)
        secret = "super-secret-token-value-123"
        with patch.object(
            server,
            "call_tool",
            side_effect=TeamError(f"claim_token={secret} rejected"),
        ):
            response = _process(
                server,
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "tools/call",
                    "params": {
                        "name": "team_read",
                        "arguments": {"operation": "team_status"},
                    },
                },
            )
        text = response["result"]["content"][0]["text"]
        self.assertTrue(response["result"]["isError"])
        self.assertNotIn(secret, text)
        self.assertIn("[REDACTED]", text)


if __name__ == "__main__":
    unittest.main()