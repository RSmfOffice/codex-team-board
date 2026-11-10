from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from codex_team.native_threads import (
    discover_native_sessions,
    find_native_session,
    inspect_native_session,
)


class NativeThreadTests(unittest.TestCase):
    def test_usage_parser_ignores_parent_history_and_reads_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            thread_id = "01a1cafe-4444-7222-8333-944455556666"
            path = root / f"rollout-{thread_id}.jsonl"
            target_task = "T-20261110-ABCDEF"
            path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "session_meta",
                                "ordinal": 0,
                                "payload": {
                                    "id": thread_id,
                                    "context_window": 996147,
                                    "subagent_history_start_ordinal": 10,
                                    "source": {
                                        "subagent": {
                                            "thread_spawn": {
                                                "parent_thread_id": "parent-123",
                                                "agent_path": "/root/reviewer",
                                            }
                                        }
                                    },
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "response_item",
                                "ordinal": 1,
                                "payload": {
                                    "type": "message",
                                    "role": "user",
                                    "content": [
                                        {
                                            "type": "input_text",
                                            "text": "Parent task T-20261109-999999",
                                        }
                                    ],
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "response_item",
                                "ordinal": 10,
                                "payload": {
                                    "type": "message",
                                    "role": "user",
                                    "content": [
                                        {
                                            "type": "input_text",
                                            "text": f"Review {target_task}",
                                        }
                                    ],
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "token_usage_record",
                                "ordinal": 11,
                                "payload": {
                                    "thread_id": thread_id,
                                    "turn_id": "turn-1",
                                    "usage": {
                                        "input_tokens": 1000,
                                        "cached_input_tokens": 800,
                                        "output_tokens": 50,
                                    },
                                    "turn_token_usage": {
                                        "input_tokens": 1000,
                                        "cached_input_tokens": 800,
                                        "output_tokens": 50,
                                    },
                                    "thread_token_usage": {
                                        "input_tokens": 2000,
                                        "cached_input_tokens": 1600,
                                        "output_tokens": 100,
                                    },
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "event_msg",
                                "ordinal": 12,
                                "payload": {
                                    "type": "token_count",
                                    "info": {
                                        "total_token_usage": {
                                            "input_tokens": 2000,
                                            "cached_input_tokens": 1600,
                                            "output_tokens": 100,
                                        },
                                        "last_token_usage": {
                                            "input_tokens": 1000,
                                            "cached_input_tokens": 800,
                                            "output_tokens": 50,
                                        },
                                        "model_context_window": 996147,
                                    },
                                },
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            inspected = inspect_native_session(
                thread_id,
                task_id=target_task,
                root=root,
            )
            self.assertEqual(inspected["prompt_status"], "delivered")
            self.assertEqual(inspected["task_ids"], [target_task])
            usage = inspected["usage"]
            self.assertTrue(usage["available"])
            self.assertEqual(usage["last_input_tokens"], 1000)
            self.assertEqual(usage["last_cached_input_tokens"], 800)
            self.assertEqual(usage["last_uncached_input_tokens"], 200)
            self.assertEqual(usage["total_input_tokens"], 2000)
            self.assertEqual(usage["context_tokens"], 1000)
            self.assertEqual(usage["model_context_window"], 996147)
            self.assertEqual(usage["turn_count"], 1)
            self.assertEqual(usage["last_turn_id"], "turn-1")

    def test_discovery_finds_only_subagent_sessions_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "sessions"
            root.mkdir()
            thread_id = "01a1cafe-1111-7222-8333-944455556666"
            subagent_path = root / f"rollout-{thread_id}.jsonl"
            subagent_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "session_meta",
                                "payload": {
                                    "id": thread_id,
                                    "cwd": "D:\\project",
                                    "model_provider": "deepseek",
                                    "source": {
                                        "subagent": {
                                            "thread_spawn": {
                                                "parent_thread_id": "parent-123",
                                                "agent_path": "/root/cache_prefix_review",
                                                "agent_nickname": "Hume",
                                                "agent_role": None,
                                            }
                                        }
                                    },
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
                                            "text": "Review T-20261110-ABCDEF",
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
            (root / "rollout-main.jsonl").write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "01a1cafe-9999-7222-8333-944455556666",
                            "cwd": "D:\\project",
                            "source": "vscode",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            discovered = discover_native_sessions(root=root)
            self.assertEqual(len(discovered), 1)
            self.assertEqual(discovered[0]["thread_id"], thread_id)
            self.assertEqual(discovered[0]["parent_thread_id"], "parent-123")
            self.assertEqual(discovered[0]["agent_nickname"], "Hume")
            self.assertEqual(discovered[0]["agent_role"], "team_reviewer")
            self.assertEqual(
                discovered[0]["task_ids"],
                ["T-20261110-ABCDEF"],
            )
            self.assertEqual(discovered[0]["cwd"], "D:\\project")
            self.assertEqual(discovered[0]["model_provider"], "deepseek")
            old_timestamp = time.time() - 2 * 24 * 3600
            os.utime(subagent_path, (old_timestamp, old_timestamp))
            self.assertEqual(
                discover_native_sessions(
                    root=root,
                    max_age_seconds=3600,
                ),
                [],
            )

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
