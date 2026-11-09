from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable


CORE_SURFACE_VERSION = "core-v1"
CACHE_PROFILE_PREFIX = "cache-v1"

READ_OPERATIONS = frozenset(
    {
        "team_status",
        "team_list_roles",
        "team_role_prompt",
        "team_list_tasks",
        "team_get_task",
        "team_get_task_events",
        "team_list_worktrees",
        "team_list_messages",
        "team_cluster_status",
        "team_agent_list",
    }
)

WRITE_OPERATIONS = frozenset(
    {
        "team_init_project",
        "team_create_task",
        "team_create_worktree",
        "team_claim_task",
        "team_heartbeat",
        "team_update_task",
        "team_submit_task",
        "team_block_task",
        "team_unblock_task",
        "team_cancel_task",
        "team_review_task",
        "team_add_message",
        "team_cluster_start",
        "team_cluster_ensure",
        "team_cluster_control",
        "team_goal_set_workstream",
        "team_goal_set_checkpoint",
        "team_agent_reset",
    }
)

OPERATIONS = READ_OPERATIONS | WRITE_OPERATIONS
MUTATING_TOOLS = WRITE_OPERATIONS
CORE_TOOL_NAMES = frozenset({"team_read", "team_write"})


def core_tool_definitions() -> list[dict[str, Any]]:
    """Return the cache-stable Agent-facing Team Board MCP surface."""

    operation = {
        "type": "string",
        "description": (
            "Operation name. Call team_read with operation='help' "
            "to inspect the available operations and arguments."
        ),
        "minLength": 1,
    }
    arguments = {
        "type": "object",
        "description": "Operation-specific arguments.",
        "default": {},
    }
    request_id = {
        "type": "string",
        "description": "Optional caller correlation ID.",
    }
    return [
        {
            "name": "team_read",
            "description": (
                "Read Team Board state through a stable operation dispatcher. "
                "This tool schema is intentionally fixed; use operation='help' "
                "to discover read operations without changing the schema."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "operation": operation,
                    "arguments": arguments,
                    "request_id": request_id,
                },
                "required": ["operation"],
                "additionalProperties": False,
            },
        },
        {
            "name": "team_write",
            "description": (
                "Mutate Team Board state through a stable operation dispatcher. "
                "This tool schema is intentionally fixed; use team_read with "
                "operation='help' to discover write operations and arguments."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "operation": operation,
                    "arguments": arguments,
                    "request_id": request_id,
                },
                "required": ["operation"],
                "additionalProperties": False,
            },
        },
    ]


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def core_tool_schema_hash() -> str:
    return sha256_text(canonical_json(core_tool_definitions()))


def file_hash(path: Path | None) -> str:
    if path is None or not path.is_file():
        return sha256_text("")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def agent_cache_profile(
    developer_instructions: str,
    *,
    policy_path: Path | None,
    tool_schema_hash: str | None = None,
) -> str:
    """Build a compact exact compatibility identity for one Agent thread."""

    tools_hash = tool_schema_hash or core_tool_schema_hash()
    role_hash = sha256_text(developer_instructions)
    policy_hash = file_hash(policy_path)
    return (
        f"{CACHE_PROFILE_PREFIX};{CORE_SURFACE_VERSION};"
        f"tools={tools_hash};policy={policy_hash};role={role_hash}"
    )


def operation_help(
    definitions: Iterable[dict[str, Any]],
    *,
    include_write: bool = True,
) -> dict[str, Any]:
    """Build a compact operation catalog from the legacy definitions."""

    catalog: dict[str, list[dict[str, Any]]] = {"read": [], "write": []}
    for tool in definitions:
        name = str(tool.get("name") or "")
        if name not in OPERATIONS:
            continue
        schema = tool.get("inputSchema")
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        required = schema.get("required", []) if isinstance(schema, dict) else []
        mode = "read" if name in READ_OPERATIONS else "write"
        if mode == "write" and not include_write:
            continue
        catalog[mode].append(
            {
                "operation": name,
                "description": str(tool.get("description") or ""),
                "required": list(required),
                "properties": sorted(properties),
            }
        )
    return {
        "surface": CORE_SURFACE_VERSION,
        "tools": ["team_read", "team_write"],
        "operations": catalog,
    }
