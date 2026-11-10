from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from codex_team.cache_contract import (
    OPERATIONS,
    READ_OPERATIONS,
    WRITE_OPERATIONS,
    agent_cache_profile,
    core_tool_definitions,
    core_tool_schema_hash,
    operation_help,
)
from codex_team.mcp import (
    _normalize_tool_call,
    _process,
    _tool_definitions,
    _visible_tool_definitions,
)


class CacheContractTests(unittest.TestCase):
    def test_core_schema_is_small_fixed_and_has_no_operation_enum(self) -> None:
        tools = core_tool_definitions()
        self.assertEqual({"team_read", "team_write"}, {tool["name"] for tool in tools})
        for tool in tools:
            schema = tool["inputSchema"]
            self.assertEqual(["operation"], schema["required"])
            self.assertNotIn(
                "enum",
                schema["properties"]["operation"],
            )
            self.assertEqual(
                {"operation", "arguments", "request_id"},
                set(schema["properties"]),
            )
        self.assertEqual(core_tool_schema_hash(), core_tool_schema_hash())

    def test_operation_help_keeps_catalog_out_of_tool_schema(self) -> None:
        help_result = operation_help(_tool_definitions())
        self.assertEqual("core-v1", help_result["surface"])
        self.assertTrue(help_result["operations"]["read"])
        self.assertTrue(help_result["operations"]["write"])
        self.assertIn(
            "team_get_task",
            {
                item["operation"]
                for item in help_result["operations"]["read"]
            },
        )
        get_task = next(
            item
            for item in help_result["operations"]["read"]
            if item["operation"] == "team_get_task"
        )
        self.assertIn("inputSchema", get_task)
        create_task = next(
            item
            for item in help_result["operations"]["write"]
            if item["operation"] == "team_create_task"
        )
        priority = create_task["inputSchema"]["properties"]["priority"]
        self.assertEqual((0, 3), (priority["minimum"], priority["maximum"]))
        self.assertIn("0 is highest", priority["description"])

    def test_core_dispatcher_covers_every_legacy_operation(self) -> None:
        self.assertEqual(
            OPERATIONS,
            {tool["name"] for tool in _tool_definitions()},
        )
        with patch.dict(
            os.environ,
            {
                "CODEX_TEAM_MCP_ACCESS": "full",
                "CODEX_TEAM_MCP_SURFACE": "core",
            },
            clear=False,
        ):
            for operation in READ_OPERATIONS:
                self.assertEqual(
                    (operation, {}),
                    _normalize_tool_call(
                        "team_read",
                        {"operation": operation, "arguments": {}},
                    ),
                )
            for operation in WRITE_OPERATIONS:
                self.assertEqual(
                    (operation, {}),
                    _normalize_tool_call(
                        "team_write",
                        {"operation": operation, "arguments": {}},
                    ),
                )

    def test_core_and_legacy_surfaces_are_explicit(self) -> None:
        with patch.dict(
            os.environ,
            {
                "CODEX_TEAM_MCP_ACCESS": "full",
                "CODEX_TEAM_MCP_SURFACE": "core",
            },
            clear=False,
        ):
            self.assertEqual(
                {"team_read", "team_write"},
                {tool["name"] for tool in _visible_tool_definitions()},
            )
            self.assertEqual(
                ("team_get_task", {"task_id": "T-1"}),
                _normalize_tool_call(
                    "team_read",
                    {
                        "operation": "team_get_task",
                        "arguments": {"task_id": "T-1"},
                    },
                ),
            )
            with self.assertRaisesRegex(Exception, "team_write"):
                _normalize_tool_call(
                    "team_read",
                    {
                        "operation": "team_create_task",
                        "arguments": {},
                    },
                )
        with patch.dict(
            os.environ,
            {
                "CODEX_TEAM_MCP_ACCESS": "full",
                "CODEX_TEAM_MCP_SURFACE": "legacy",
            },
            clear=False,
        ):
            self.assertGreater(len(_visible_tool_definitions()), 2)

    def test_core_dispatcher_calls_existing_tool_handler(self) -> None:
        server = Mock()
        server.call_tool.return_value = {"task": "T-1"}
        with patch.dict(
            os.environ,
            {
                "CODEX_TEAM_MCP_ACCESS": "full",
                "CODEX_TEAM_MCP_SURFACE": "core",
            },
            clear=False,
        ):
            result = _process(
                server,
                {
                    "id": "request-1",
                    "method": "tools/call",
                    "params": {
                        "name": "team_read",
                        "arguments": {
                            "operation": "team_get_task",
                            "arguments": {"task_id": "T-1"},
                        },
                    },
                },
            )
        self.assertIn("result", result)
        server.call_tool.assert_called_once_with(
            "team_get_task",
            {"task_id": "T-1"},
        )

    def test_core_dispatcher_rejects_schema_invalid_arguments(self) -> None:
        server = Mock()
        with patch.dict(
            os.environ,
            {
                "CODEX_TEAM_MCP_ACCESS": "full",
                "CODEX_TEAM_MCP_SURFACE": "core",
            },
            clear=False,
        ):
            invalid_requests = (
                {
                    "task_id": "T-1",
                    "actor": "lead",
                    "approve": "false",
                    "notes": "must not become true",
                },
                {
                    "goal_id": "G-1",
                    "action": "invalid-action",
                },
                {
                    "task_id": "T-1",
                    "actor": "worker",
                    "reason": "",
                },
            )
            for index, arguments in enumerate(invalid_requests):
                with self.subTest(arguments=arguments):
                    operation = (
                        "team_review_task"
                        if "approve" in arguments
                        else "team_cluster_control"
                        if "action" in arguments
                        else "team_block_task"
                    )
                    result = _process(
                        server,
                        {
                            "id": f"invalid-{index}",
                            "method": "tools/call",
                            "params": {
                                "name": "team_write",
                                "arguments": {
                                    "operation": operation,
                                    "arguments": arguments,
                                },
                            },
                        },
                    )
                    self.assertTrue(result["result"]["isError"])
        server.call_tool.assert_not_called()

    def test_cluster_control_schema_rejects_unknown_action(self) -> None:
        server = Mock()
        with patch.dict(
            os.environ,
            {
                "CODEX_TEAM_MCP_ACCESS": "full",
                "CODEX_TEAM_MCP_SURFACE": "core",
            },
            clear=False,
        ):
            result = _process(
                server,
                {
                    "id": "invalid-action",
                    "method": "tools/call",
                    "params": {
                        "name": "team_write",
                        "arguments": {
                            "operation": "team_cluster_control",
                            "arguments": {
                                "goal_id": "G-1",
                                "action": "invalid-action",
                            },
                        },
                    },
                },
            )
        self.assertTrue(result["result"]["isError"])
        server.call_tool.assert_not_called()

    def test_legacy_surface_uses_the_same_runtime_validation(self) -> None:
        server = Mock()
        with patch.dict(
            os.environ,
            {
                "CODEX_TEAM_MCP_ACCESS": "full",
                "CODEX_TEAM_MCP_SURFACE": "legacy",
            },
            clear=False,
        ):
            result = _process(
                server,
                {
                    "id": "invalid-legacy",
                    "method": "tools/call",
                    "params": {
                        "name": "team_claim_task",
                        "arguments": {
                            "task_id": "T-1",
                            "actor": "worker",
                            "lease_minutes": "30",
                        },
                    },
                },
            )
        self.assertTrue(result["result"]["isError"])
        server.call_tool.assert_not_called()

    def test_core_read_only_cannot_dispatch_write_operation(self) -> None:
        server = Mock()
        with patch.dict(
            os.environ,
            {
                "CODEX_TEAM_MCP_ACCESS": "read_only",
                "CODEX_TEAM_MCP_SURFACE": "core",
            },
            clear=False,
        ):
            result = _process(
                server,
                {
                    "id": "request-2",
                    "method": "tools/call",
                    "params": {
                        "name": "team_write",
                        "arguments": {
                            "operation": "team_create_task",
                            "arguments": {},
                        },
                    },
                },
            )
        self.assertTrue(result["result"]["isError"])
        server.call_tool.assert_not_called()

        self.assertEqual(
            [],
            operation_help(
                _tool_definitions(),
                include_write=False,
            )["operations"]["write"],
        )

    def test_agent_profile_changes_with_policy_role_and_tool_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            policy = Path(temporary) / "AGENTS.md"
            policy.write_text("policy-v1", encoding="utf-8")
            first = agent_cache_profile(
                "role-v1",
                policy_path=policy,
                sandbox_mode="workspace-write",
                surface="core",
                tool_schema_hash="tools-v1",
            )
            self.assertNotEqual(
                first,
                agent_cache_profile(
                    "role-v2",
                    policy_path=policy,
                    sandbox_mode="workspace-write",
                    surface="core",
                    tool_schema_hash="tools-v1",
                ),
            )
            self.assertNotEqual(
                first,
                agent_cache_profile(
                    "role-v1",
                    policy_path=policy,
                    sandbox_mode="workspace-write",
                    surface="core",
                    tool_schema_hash="tools-v2",
                ),
            )
            policy.write_text("policy-v2", encoding="utf-8")
            self.assertNotEqual(
                first,
                agent_cache_profile(
                    "role-v1",
                    policy_path=policy,
                    sandbox_mode="workspace-write",
                    surface="core",
                    tool_schema_hash="tools-v1",
                ),
            )
            self.assertNotEqual(
                first,
                agent_cache_profile(
                    "role-v1",
                    policy_path=policy,
                    sandbox_mode="danger-full-access",
                    surface="core",
                    tool_schema_hash="tools-v1",
                ),
            )
            self.assertNotEqual(
                first,
                agent_cache_profile(
                    "role-v1",
                    policy_path=policy,
                    sandbox_mode="workspace-write",
                    surface="legacy",
                    tool_schema_hash="tools-v1",
                ),
            )


if __name__ == "__main__":
    unittest.main()
