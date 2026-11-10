from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codex_team.native_threads import (
    find_native_session,
    inspect_native_session,
)


class NativeThreadTests(unittest.TestCase):
    def test_session_inspection_detects_parent_identity_and_task_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "sessions"
            path = (
                root
                / "2026"
                / "11"
                / "10"
                / "rollout-2026-11-10T12-00-00-01a1cafe-1111-7222-8333-944455556666.jsonl"
            )
            path.parent.mkdir(parents=True)
            path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "session_meta",
                                "payload": {
                                    "source": {
                                        "subagent": {
                                            "thread_spawn": {
                                                "parent_thread_id": "parent-123",
                                                "agent_path": "/root/reviewer",
                                                "agent_nickname": "Socrates",
                                                "agent_role": "team_reviewer",
                                            }
                                        }
                                    }
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "response_item",
                                "payload": {
                                    "type": "message",
                                    "role": "user",
                                    "content": [
                                        {
                                            "type": "input_text",
                                            "text": "Execute T-20261110-ABCDEF",
                                        }
                                    ],
                                },
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            thread_id = "01a1cafe-1111-7222-8333-944455556666"
            self.assertEqual(find_native_session(thread_id, root=root), path)
            result = inspect_native_session(
                thread_id,
                task_id="T-20261110-ABCDEF",
                root=root,
            )
            self.assertTrue(result["session_found"])
            self.assertEqual(result["prompt_status"], "delivered")
            self.assertTrue(result["task_id_present"])
            self.assertEqual(result["parent_thread_id"], "parent-123")
            self.assertEqual(result["agent_path"], "/root/reviewer")
            self.assertEqual(result["agent_nickname"], "Socrates")
            self.assertEqual(result["agent_role"], "team_reviewer")
            self.assertEqual(len(result["prompt_hash"]), 64)

    def test_missing_session_and_missing_task_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            thread_id = "01a1cafe-1111-7222-8333-944455556666"
            missing = inspect_native_session(
                thread_id,
                task_id="T-20261110-ABCDEF",
                root=root,
            )
            self.assertFalse(missing["session_found"])
            self.assertEqual(missing["prompt_status"], "missing")

            path = root / f"rollout-{thread_id}.jsonl"
            path.write_text(
                json.dumps({"type": "session_meta", "payload": {}}) + "\n",
                encoding="utf-8",
            )
            missing_prompt = inspect_native_session(
                thread_id,
                task_id="T-20261110-ABCDEF",
                root=root,
            )
            self.assertTrue(missing_prompt["session_found"])
            self.assertEqual(missing_prompt["prompt_status"], "missing")

    def test_only_user_input_delivers_task_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            thread_id = "01a1cafe-1111-7222-8333-944455556666"
            path = root / f"rollout-{thread_id}.jsonl"
            task_id = "T-20261110-ABCDEF"
            path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "response_item",
                                "payload": {
                                    "type": "message",
                                    "role": "assistant",
                                    "content": [
                                        {
                                            "type": "output_text",
                                            "text": f"Working on {task_id}",
                                        }
                                    ],
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "response_item",
                                "payload": {
                                    "type": "function_call_output",
                                    "output": f"log echo {task_id}",
                                },
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            result = inspect_native_session(
                thread_id,
                task_id=task_id,
                root=root,
            )
            self.assertTrue(result["session_found"])
            self.assertFalse(result["task_id_present"])
            self.assertEqual(result["prompt_status"], "missing")

            with path.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "user",
                                "content": [
                                    {
                                        "type": "input_text",
                                        "text": f"Please execute {task_id} now.",
                                    }
                                ],
                            },
                        }
                    )
                    + "\n"
                )
            delivered = inspect_native_session(
                thread_id,
                task_id=task_id,
                root=root,
            )
            self.assertTrue(delivered["task_id_present"])
            self.assertEqual(delivered["prompt_status"], "delivered")
            self.assertEqual(len(delivered["prompt_hash"]), 64)

    def test_top_level_session_metadata_supplies_parent_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            thread_id = "01a1cafe-1111-7222-8333-944455556666"
            path = root / f"rollout-{thread_id}.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "parent_thread_id": "parent-top-level",
                            "agent_path": "/root/worker",
                            "agent_nickname": "Noether",
                            "agent_role": "team_worker",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            result = inspect_native_session(thread_id, root=root)
            self.assertEqual(
                result["parent_thread_id"],
                "parent-top-level",
            )
            self.assertEqual(result["agent_path"], "/root/worker")
            self.assertEqual(result["agent_nickname"], "Noether")
            self.assertEqual(result["agent_role"], "team_worker")


if __name__ == "__main__":
    unittest.main()
