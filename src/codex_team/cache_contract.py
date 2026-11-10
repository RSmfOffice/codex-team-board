from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import __version__
from .db import SCHEMA_VERSION
from .prompt_contract import (
    PROMPT_CONTRACT_VERSION,
    bundled_manifest_hash,
    configured_access_mode,
    instructions_for_surface,
)


CORE_SURFACE_VERSION = "core-v1"
CACHE_PROFILE_PREFIX = "cache-v1"
WORKFLOW_EPOCH_VERSION = "workflow-v3"
SURFACES = frozenset({"core", "admin", "legacy"})
CURRENT_MANIFEST_HASH = bundled_manifest_hash()

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
        "team_goal_migrate_workflow",
        "team_agent_register_native",
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


def configured_surface() -> str:
    surface = os.environ.get("CODEX_TEAM_MCP_SURFACE", "core").strip().lower()
    return surface if surface in SURFACES else surface


def tool_surface_identity(surface: str | None = None) -> tuple[str, str]:
    selected = (surface or configured_surface()).strip().lower()
    if selected not in SURFACES:
        return selected, sha256_text(f"invalid-surface:{selected}")
    if selected == "core":
        return selected, core_tool_schema_hash()
    # Non-core surfaces are admin compatibility modes. Their version must be
    # bumped whenever their tool declarations change.
    return selected, sha256_text(f"{selected}-surface-v1")


def file_hash(path: Path | None) -> str:
    if path is None or not path.is_file():
        return sha256_text("")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def agent_cache_profile(
    developer_instructions: str,
    *,
    policy_path: Path | None,
    sandbox_mode: str,
    surface: str | None = None,
    tool_schema_hash: str | None = None,
    access_mode: str | None = None,
    mcp_instructions: str | None = None,
    runtime_version: str = "",
    runtime_identity: str = "",
) -> str:
    """Build a compact exact compatibility identity for one Agent thread."""

    selected_surface, surface_hash = tool_surface_identity(surface)
    selected_access = access_mode or configured_access_mode()
    instruction_text = mcp_instructions or instructions_for_surface(
        selected_surface,
        selected_access,
    )
    tools_hash = tool_schema_hash or surface_hash
    role_hash = sha256_text(developer_instructions)
    policy_hash = file_hash(policy_path)
    sandbox_hash = sha256_text(str(sandbox_mode).strip().lower())
    instructions_hash = sha256_text(instruction_text)
    runtime_version_hash = sha256_text(str(runtime_version).strip())
    runtime_identity_hash = sha256_text(str(runtime_identity).strip())
    manifest_hash = CURRENT_MANIFEST_HASH
    return (
        f"{CACHE_PROFILE_PREFIX};{CORE_SURFACE_VERSION};surface={selected_surface};"
        f"tools={tools_hash};policy={policy_hash};role={role_hash};"
        f"sandbox={sandbox_hash};access={selected_access};"
        f"instructions={instructions_hash};manifest={manifest_hash};"
        f"contract={PROMPT_CONTRACT_VERSION};"
        f"workflow={WORKFLOW_EPOCH_VERSION};schema={SCHEMA_VERSION};"
        f"runtime_version={runtime_version_hash};"
        f"runtime_identity={runtime_identity_hash}"
    )


def workflow_epoch(
    *,
    policy_hash: str,
    role_prompts: Mapping[str, str],
    native_role_hashes: Mapping[str, str],
    runtime: str,
    model: str,
    model_provider: str,
    surface: str | None = None,
    access_mode: str | None = None,
) -> str:
    """Build the semantic cohort for new goals and task dispatch."""

    selected_surface, surface_hash = tool_surface_identity(surface)
    selected_access = access_mode or configured_access_mode()
    payload = {
        "epoch_version": WORKFLOW_EPOCH_VERSION,
        "package_version": __version__,
        "schema_version": SCHEMA_VERSION,
        "prompt_contract": PROMPT_CONTRACT_VERSION,
        "manifest_hash": CURRENT_MANIFEST_HASH,
        "surface": selected_surface,
        "surface_hash": surface_hash,
        "access_mode": selected_access,
        "instructions_hash": sha256_text(
            instructions_for_surface(selected_surface, selected_access)
        ),
        "policy_hash": str(policy_hash),
        "role_prompts": {
            str(role): sha256_text(str(prompt))
            for role, prompt in sorted(role_prompts.items())
        },
        "native_role_hashes": {
            str(role): str(value)
            for role, value in sorted(native_role_hashes.items())
        },
        "runtime": str(runtime).strip().lower(),
        "model": str(model).strip(),
        "model_provider": str(model_provider).strip(),
    }
    digest = sha256_text(canonical_json(payload))[:32]
    return f"{WORKFLOW_EPOCH_VERSION}:{digest}"


def cache_profile_contract_status(profile: str | None) -> str:
    """Classify a stored profile against the current model-visible contract."""

    normalized = str(profile or "").strip()
    if not normalized:
        return "unknown"
    parts = normalized.split(";")
    if (
        len(parts) < 2
        or parts[0] != CACHE_PROFILE_PREFIX
        or parts[1] != CORE_SURFACE_VERSION
    ):
        return "historical"
    fields: dict[str, str] = {}
    for part in parts[2:]:
        name, separator, value = part.partition("=")
        if separator:
            fields[name] = value
    if (
        fields.get("contract") == PROMPT_CONTRACT_VERSION
        and fields.get("manifest") == CURRENT_MANIFEST_HASH
        and fields.get("workflow") == WORKFLOW_EPOCH_VERSION
        and fields.get("schema") == str(SCHEMA_VERSION)
    ):
        return "current"
    return "historical"


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
        mode = "read" if name in READ_OPERATIONS else "write"
        if mode == "write" and not include_write:
            continue
        catalog[mode].append(
            {
                "operation": name,
                "description": str(tool.get("description") or ""),
                "inputSchema": schema,
            }
        )
    return {
        "surface": CORE_SURFACE_VERSION,
        "tools": ["team_read", "team_write"],
        "operations": catalog,
    }
