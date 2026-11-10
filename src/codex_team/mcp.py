from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Mapping

from . import __version__
from .cache_contract import (
    CORE_TOOL_NAMES,
    MUTATING_TOOLS,
    OPERATIONS,
    READ_OPERATIONS,
    core_tool_definitions,
    operation_help,
)
from .config import Settings
from .cluster import ClusterManager, daemon_status as cluster_daemon_status, ensure_daemon
from .prompt_contract import (
    CORE_INSTRUCTIONS,
    INSTRUCTIONS,
    configured_access_mode,
    instructions_for_surface,
)
from .service import TeamError, TeamService
from .validation import SchemaValidationError, validate_input_schema
from .worktrees import WorktreeManager

LOGGER = logging.getLogger("codex_team.mcp")
PROTOCOL_VERSION = "2025-06-18"


def _access_mode() -> str:
    try:
        return configured_access_mode()
    except ValueError as exc:
        raise TeamError(str(exc)) from exc


def _surface_mode() -> str:
    mode = os.environ.get("CODEX_TEAM_MCP_SURFACE", "core").strip().lower()
    if mode not in {"core", "admin", "legacy"}:
        raise TeamError("CODEX_TEAM_MCP_SURFACE must be core, admin, or legacy")
    return mode


def _instructions_for_surface() -> str:
    return instructions_for_surface(
        _surface_mode(),
        _access_mode(),
    )


def _visible_tool_definitions() -> list[dict[str, Any]]:
    mode = _access_mode()
    if mode == "off":
        return []
    surface = _surface_mode()
    tools = core_tool_definitions() if surface == "core" else _tool_definitions()
    if mode == "read_only":
        if surface == "core":
            return [tool for tool in tools if tool.get("name") == "team_read"]
        return [
            tool
            for tool in tools
            if str(tool.get("name") or "") not in MUTATING_TOOLS
        ]
    return tools


def _unwrap_core_call(
    name: str,
    arguments: Mapping[str, Any],
) -> tuple[str, dict[str, Any]]:
    operation = arguments.get("operation")
    if not isinstance(operation, str) or not operation.strip():
        raise TeamError(f"{name}.operation must not be empty")
    operation = operation.strip()
    if operation == "help":
        if name != "team_read":
            raise TeamError("operation='help' must be called through team_read")
        return operation, {}
    if operation not in OPERATIONS:
        raise TeamError(f"unknown Team Board operation: {operation}")
    expected_tool = "team_read" if operation in READ_OPERATIONS else "team_write"
    if name != expected_tool:
        raise TeamError(
            f"operation {operation} must be called through {expected_tool}"
        )
    raw_arguments = arguments.get("arguments", {})
    if not isinstance(raw_arguments, dict):
        raise TeamError("arguments must be an object")
    return operation, raw_arguments


def _normalize_tool_call(
    name: str,
    arguments: Mapping[str, Any],
) -> tuple[str, dict[str, Any]]:
    surface = _surface_mode()
    if surface == "core":
        if name not in CORE_TOOL_NAMES:
            raise TeamError(
                f"tool {name} is not exposed by the core Team Board surface"
            )
        return _unwrap_core_call(name, arguments)
    if name in CORE_TOOL_NAMES:
        raise TeamError(f"tool {name} requires CODEX_TEAM_MCP_SURFACE=core")
    return name, dict(arguments)


def _guard_tool_call(name: str, arguments: Mapping[str, Any]) -> None:
    mode = _access_mode()
    if mode == "off":
        raise TeamError("Codex Team Board MCP is disabled")
    if mode == "read_only" and name in MUTATING_TOOLS:
        raise TeamError("Codex Team Board MCP is read-only")
    raw_roots = os.environ.get("CODEX_TEAM_ALLOWED_ROOTS", "").strip()
    if not raw_roots:
        return
    allowed = [
        Path(value).expanduser().resolve()
        for value in raw_roots.split(os.pathsep)
        if value.strip()
    ]
    for key in ("repo_root", "repo", "worktree_path"):
        value = arguments.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        candidate = Path(value).expanduser().resolve()
        if not any(
            candidate == root or candidate.is_relative_to(root)
            for root in allowed
        ):
            raise TeamError(
                f"{key} is outside CODEX_TEAM_ALLOWED_ROOTS: {candidate}"
            )


def _validate_tool_arguments(
    name: str,
    arguments: Mapping[str, Any],
) -> None:
    if name == "help":
        return
    definition = next(
        (
            tool
            for tool in _tool_definitions()
            if tool.get("name") == name
        ),
        None,
    )
    if definition is None:
        raise TeamError(f"unknown tool: {name}")
    schema = definition.get("inputSchema")
    if not isinstance(schema, Mapping):
        raise TeamError(f"tool schema is unavailable: {name}")
    validate_input_schema(arguments, schema)


def _optional_boolean(
    arguments: Mapping[str, Any],
    name: str,
    *,
    default: bool,
) -> bool:
    value = arguments.get(name, default)
    if not isinstance(value, bool):
        raise TeamError(f"{name} must be a boolean")
    return value


def _required_boolean(arguments: Mapping[str, Any], name: str) -> bool:
    value = arguments.get(name)
    if not isinstance(value, bool):
        raise TeamError(f"{name} must be a boolean")
    return value


def _tool_definitions() -> list[dict[str, Any]]:
    repo = {
        "type": "string",
        "description": "Absolute or relative path to the target Git repository.",
    }
    task_id = {"type": "string", "description": "Task ID such as T-20261107-ABC123."}

    return [
        {
            "name": "team_init_project",
            "description": (
                "Register a Git repository and install Codex team roles plus the managed "
                "AGENTS.md collaboration policy. Existing files are preserved."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repo_root": repo,
                    "max_threads": {"type": "integer", "minimum": 1, "maximum": 8, "default": 4},
                },
                "required": ["repo_root"],
                "additionalProperties": False,
            },
        },
        {
            "name": "team_status",
            "description": "Show task counts and active projects, optionally scoped to one repository.",
            "inputSchema": {
                "type": "object",
                "properties": {"repo_root": repo},
                "additionalProperties": False,
            },
        },
        {
            "name": "team_list_roles",
            "description": "List the built-in team role prompt packs.",
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "team_role_prompt",
            "description": (
                "Return a role prompt to prepend to a native Codex subagent spawn prompt. "
                "Use this when the installed Codex version cannot auto-load role TOML files."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "role": {
                        "type": "string",
                        "enum": ["team_explorer", "team_worker", "team_reviewer"],
                    },
                    "repo_root": {
                        "type": "string",
                        "description": "Optional repository whose .codex/team-roles customization takes precedence.",
                    },
                },
                "required": ["role"],
                "additionalProperties": False,
            },
        },
        {
            "name": "team_create_task",
            "description": (
                "Create a task in the shared task graph. Write tasks cannot be claimed until "
                "a worktree has been created. Only the lead should create or assign tasks."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repo_root": repo,
                    "title": {"type": "string", "minLength": 1},
                    "description": {"type": "string"},
                    "acceptance": {"type": "string"},
                    "kind": {"type": "string", "enum": ["read", "write", "review", "ops"]},
                    "owner_role": {"type": "string", "minLength": 1},
                    "goal_id": {
                        "type": "string",
                        "description": "Optional automated cluster goal ID.",
                    },
                    "actor": {"type": "string", "default": "lead"},
                    "priority": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 3,
                        "default": 2,
                        "description": (
                            "Scheduling priority. 0 is highest and 3 is lowest; "
                            "values outside 0..3 are rejected."
                        ),
                    },
                    "depends_on": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": [],
                    },
                    "continuity_mode": {
                        "type": "string",
                        "enum": ["auto", "reuse", "fresh"],
                        "default": "auto",
                        "description": (
                            "Write workers default fresh in auto mode; reuse only "
                            "when explicitly requested."
                        ),
                    },
                },
                "required": [
                    "repo_root",
                    "title",
                    "description",
                    "acceptance",
                    "kind",
                    "owner_role",
                ],
                "additionalProperties": False,
            },
        },
        {
            "name": "team_list_tasks",
            "description": (
                "List tasks, blockers, leases, worktrees, and current status. "
                "Set include_archived=true to inspect tasks archived by the UI or "
                "cascaded from an archived goal."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repo_root": repo,
                    "statuses": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["backlog", "ready", "running", "review", "blocked", "done", "canceled"],
                        },
                    },
                    "owner_role": {"type": "string"},
                    "goal_id": {"type": "string"},
                    "include_done": {"type": "boolean", "default": True},
                    "include_archived": {"type": "boolean", "default": False},
                },
                "required": ["repo_root"],
                "additionalProperties": False,
            },
        },
        {
            "name": "team_get_task",
            "description": "Read one task with dependencies, dependents, lease state, and worktree.",
            "inputSchema": {
                "type": "object",
                "properties": {"task_id": task_id},
                "required": ["task_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "team_get_task_events",
            "description": "Read the append-only task audit trail, including claim, progress, submit, and review events.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": task_id,
                    "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
                },
                "required": ["task_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "team_create_worktree",
            "description": (
                "Create an isolated Git worktree and branch for a write task before any worker "
                "claims it. The main checkout must be clean and on a named branch."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": task_id,
                    "repo_root": repo,
                    "actor": {"type": "string", "default": "lead"},
                    "base_ref": {"type": "string", "default": "HEAD"},
                    "worktree_path": {"type": "string"},
                },
                "required": ["task_id", "repo_root"],
                "additionalProperties": False,
            },
        },
        {
            "name": "team_list_worktrees",
            "description": "List Git worktrees registered in the repository.",
            "inputSchema": {
                "type": "object",
                "properties": {"repo_root": repo},
                "required": ["repo_root"],
                "additionalProperties": False,
            },
        },
        {
            "name": "team_claim_task",
            "description": (
                "Atomically claim a ready task and obtain a claim_token. A write task requires "
                "an existing worktree. Keep the token private and use it for all updates."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": task_id,
                    "actor": {"type": "string", "minLength": 1},
                    "lease_minutes": {"type": "integer", "minimum": 5, "maximum": 240, "default": 30},
                },
                "required": ["task_id", "actor"],
                "additionalProperties": False,
            },
        },
        {
            "name": "team_heartbeat",
            "description": "Renew the lease for a running task.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": task_id,
                    "actor": {"type": "string"},
                    "claim_token": {"type": "string"},
                    "lease_minutes": {"type": "integer", "minimum": 5, "maximum": 240, "default": 30},
                },
                "required": ["task_id", "actor", "claim_token"],
                "additionalProperties": False,
            },
        },
        {
            "name": "team_update_task",
            "description": "Record worker progress, result progress, or test evidence for a claimed task.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": task_id,
                    "actor": {"type": "string"},
                    "claim_token": {"type": "string"},
                    "note": {"type": "string"},
                    "result": {"type": "string"},
                    "tests": {"type": "string"},
                    "commit_sha": {
                        "type": "string",
                        "description": "Required for write tasks; the committed worktree HEAD SHA.",
                    },
                },
                "required": ["task_id", "actor", "claim_token", "note"],
                "additionalProperties": False,
            },
        },
        {
            "name": "team_submit_task",
            "description": (
                "Move a running task to review. result must summarize behavior, changed "
                "files, risks, and unresolved review questions; tests must contain exact commands and results."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": task_id,
                    "actor": {"type": "string"},
                    "claim_token": {"type": "string"},
                    "result": {
                        "type": "string",
                        "description": "Behavior summary, changed files, risks, and review focus.",
                    },
                    "tests": {
                        "type": "string",
                        "description": "Exact commands and outcomes, including failures or skipped checks.",
                    },
                    "commit_sha": {
                        "type": "string",
                        "description": "Required for write tasks; the committed worktree HEAD SHA.",
                    },
                },
                "required": ["task_id", "actor", "claim_token", "result", "tests"],
                "additionalProperties": False,
            },
        },
        {
            "name": "team_block_task",
            "description": "Mark a task blocked and record the exact reason.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": task_id,
                    "actor": {"type": "string"},
                    "reason": {"type": "string", "minLength": 1},
                    "claim_token": {"type": "string"},
                },
                "required": ["task_id", "actor", "reason"],
                "additionalProperties": False,
            },
        },
        {
            "name": "team_unblock_task",
            "description": "Return a blocked task to ready after the blocker is resolved.",
            "inputSchema": {
                "type": "object",
                "properties": {"task_id": task_id, "actor": {"type": "string"}},
                "required": ["task_id", "actor"],
                "additionalProperties": False,
            },
        },
        {
            "name": "team_cancel_task",
            "description": (
                "Cancel a stale or superseded non-running task while preserving its event history."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": task_id,
                    "actor": {"type": "string"},
                    "reason": {"type": "string", "minLength": 1},
                },
                "required": ["task_id", "actor", "reason"],
                "additionalProperties": False,
            },
        },
        {
            "name": "team_review_task",
            "description": (
                "Lead-only final review. Approve moves a reviewed task to done; reject returns "
                "it to ready. A write task requires a clean worktree and a merge commit "
                "reachable from the target branch recorded when its worktree was created."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": task_id,
                    "actor": {"type": "string", "enum": ["lead", "main", "human"]},
                    "approve": {"type": "boolean"},
                    "notes": {"type": "string"},
                    "merge_commit": {
                        "type": "string",
                        "description": "Required when approving a write task; record the commit merged into the main branch.",
                    },
                },
                "required": ["task_id", "actor", "approve", "notes"],
                "additionalProperties": False,
            },
        },
        {
            "name": "team_add_message",
            "description": "Send a team message, optionally attached to a task.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repo_root": repo,
                    "sender": {"type": "string"},
                    "body": {"type": "string", "minLength": 1},
                    "recipient": {"type": "string"},
                    "task_id": {"type": "string"},
                },
                "required": ["repo_root", "sender", "body"],
                "additionalProperties": False,
            },
        },
        {
            "name": "team_list_messages",
            "description": "Read recent team messages, optionally for one task.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repo_root": repo,
                    "task_id": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
                },
                "required": ["repo_root"],
                "additionalProperties": False,
            },
        },
        {
            "name": "team_cluster_start",
            "description": (
                "Create a persistent automated cluster goal and start the background scheduler. "
                "The managed Lead will plan/review; deterministic workers execute goal tasks."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repo_root": repo,
                    "objective": {"type": "string", "minLength": 1},
                    "autonomy": {
                        "type": "string",
                        "enum": ["auto", "supervised"],
                        "default": "auto",
                    },
                    "max_worker_attempts": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10,
                        "default": 3,
                    },
                    "max_lead_attempts": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "default": 5,
                    },
                    "workstream_key": {
                        "type": "string",
                        "description": "Continuity scope; defaults to the goal ID.",
                    },
                    "continuity_reason": {
                        "type": "string",
                        "description": "Why related goals may reuse a Lead thread.",
                    },
                },
                "required": ["repo_root", "objective"],
                "additionalProperties": False,
            },
        },
        {
            "name": "team_cluster_ensure",
            "description": (
                "Idempotently ensure the project has one planning/active cluster goal before "
                "non-trivial development work. Reuse an existing goal; never auto-resume a "
                "paused goal. Start the background scheduler when needed."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repo_root": repo,
                    "objective": {"type": "string", "minLength": 1},
                    "autonomy": {
                        "type": "string",
                        "enum": ["auto", "supervised"],
                        "default": "auto",
                    },
                    "max_worker_attempts": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10,
                        "default": 3,
                    },
                    "max_lead_attempts": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "default": 5,
                    },
                    "workstream_key": {
                        "type": "string",
                        "description": "Continuity scope; defaults to the goal ID.",
                    },
                    "continuity_reason": {
                        "type": "string",
                        "description": "Why related goals may reuse a Lead thread.",
                    },
                },
                "required": ["repo_root", "objective"],
                "additionalProperties": False,
            },
        },
        {
            "name": "team_cluster_status",
            "description": "Read cluster goals, Lead/Worker runs, retries, errors, and daemon status.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "goal_id": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "team_cluster_control",
            "description": "Pause, resume, cancel, archive, or restore an automated cluster goal.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "goal_id": {"type": "string"},
                    "action": {
                        "type": "string",
                        "enum": [
                            "pause",
                            "resume",
                            "cancel",
                            "archive",
                            "restore",
                        ],
                    },
                },
                "required": ["goal_id", "action"],
                "additionalProperties": False,
            },
        },
        {
            "name": "team_goal_set_workstream",
            "description": (
                "Set a cluster goal's workstream key and continuity rationale "
                "without starting or resuming the goal."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "goal_id": {"type": "string", "minLength": 1},
                    "workstream_key": {"type": "string"},
                    "continuity_reason": {"type": "string"},
                },
                "required": ["goal_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "team_goal_set_checkpoint",
            "description": (
                "Persist a compact structured workstream checkpoint for a "
                "cluster goal. Use only durable decisions, completed work, "
                "tests, rejected approaches, risks, and next action."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "goal_id": {"type": "string", "minLength": 1},
                    "summary": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 12000,
                    },
                },
                "required": ["goal_id", "summary"],
                "additionalProperties": False,
            },
        },
        {
            "name": "team_agent_list",
            "description": (
                "List persistent Agent threads, cache usage, current assignment, "
                "and reset state."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "include_archived": {
                        "type": "boolean",
                        "default": False,
                    },
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "team_agent_reset",
            "description": (
                "Request unsubscription and supersession of an idle persistent "
                "Agent thread."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "minLength": 1},
                    "actor": {"type": "string", "default": "human"},
                    "reason": {
                        "type": "string",
                        "default": "manual reset requested through MCP",
                    },
                },
                "required": ["agent_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "team_agent_register_native",
            "description": (
                "Register or refresh a native Codex subagent Thread in the shared "
                "AgentThread registry. The native session is inspected for prompt "
                "delivery, parent identity, agent path, nickname, and last-seen time. "
                "Native threads are monitored but remain same_task_only and are not "
                "eligible for managed automatic reuse."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repo_root": repo,
                    "thread_id": {"type": "string", "minLength": 1},
                    "role": {
                        "type": "string",
                        "enum": [
                            "lead",
                            "team_explorer",
                            "team_worker",
                            "team_reviewer",
                        ],
                    },
                    "workstream_key": {"type": "string", "minLength": 1},
                    "actor": {"type": "string", "minLength": 1},
                    "task_id": {"type": "string"},
                    "display_name": {"type": "string"},
                    "model": {"type": "string"},
                    "model_provider": {"type": "string"},
                },
                "required": [
                    "repo_root",
                    "thread_id",
                    "role",
                    "workstream_key",
                    "actor",
                ],
                "additionalProperties": False,
            },
        },
    ]


class TeamMcpServer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.team = TeamService(settings)
        self.worktrees = WorktreeManager(self.team)
        self.cluster = ClusterManager(
            settings,
            team=self.team,
            worktrees=self.worktrees,
        )

    def initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "protocolVersion": str(params.get("protocolVersion") or PROTOCOL_VERSION),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "codex-team-board", "version": __version__},
            "instructions": _instructions_for_surface(),
        }

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if _surface_mode() == "core" and name in CORE_TOOL_NAMES:
            name, arguments = _unwrap_core_call(name, arguments)
        if name == "help":
            return operation_help(
                _tool_definitions(),
                include_write=_access_mode() == "full",
            )
        if name == "team_init_project":
            max_threads = arguments.get("max_threads")
            return self.team.init_project(
                arguments["repo_root"],
                max_threads=int(max_threads) if max_threads is not None else None,
            )
        if name == "team_status":
            repo = arguments.get("repo_root")
            if repo:
                return self.team.board_snapshot(repo)
            return self.team.dashboard_snapshot()
        if name == "team_list_roles":
            return {"roles": self.team.list_roles()}
        if name == "team_role_prompt":
            return self.team.role_prompt(
                arguments["role"],
                arguments.get("repo_root"),
            )
        if name == "team_create_task":
            return self.team.create_task(
                arguments["repo_root"],
                title=arguments["title"],
                description=arguments.get("description", ""),
                acceptance=arguments.get("acceptance", ""),
                kind=arguments["kind"],
                owner_role=arguments["owner_role"],
                actor=arguments.get("actor", "lead"),
                priority=int(arguments.get("priority", 2)),
                depends_on=list(arguments.get("depends_on", [])),
                goal_id=arguments.get("goal_id"),
                continuity_mode=arguments.get("continuity_mode", "auto"),
            )
        if name == "team_list_tasks":
            return {
                "tasks": self.team.list_tasks(
                    arguments["repo_root"],
                    statuses=arguments.get("statuses"),
                    owner_role=arguments.get("owner_role"),
                    goal_id=arguments.get("goal_id"),
                    include_done=_optional_boolean(
                        arguments,
                        "include_done",
                        default=True,
                    ),
                    include_archived=_optional_boolean(
                        arguments,
                        "include_archived",
                        default=False,
                    ),
                )
            }
        if name == "team_get_task":
            return self.team.get_task(arguments["task_id"])
        if name == "team_get_task_events":
            return self.team.get_task_events(
                arguments["task_id"],
                limit=int(arguments.get("limit", 100)),
            )
        if name == "team_create_worktree":
            return self.worktrees.create(
                arguments["task_id"],
                repo=arguments["repo_root"],
                actor=arguments.get("actor", "lead"),
                base_ref=arguments.get("base_ref", "HEAD"),
                worktree_path=arguments.get("worktree_path"),
            )
        if name == "team_list_worktrees":
            return {"worktrees": self.worktrees.list(arguments["repo_root"])}
        if name == "team_claim_task":
            return self.team.claim_task(
                arguments["task_id"],
                actor=arguments["actor"],
                lease_minutes=int(arguments.get("lease_minutes", 30)),
            )
        if name == "team_heartbeat":
            return self.team.heartbeat(
                arguments["task_id"],
                actor=arguments["actor"],
                claim_token=arguments["claim_token"],
                lease_minutes=int(arguments.get("lease_minutes", 30)),
            )
        if name == "team_update_task":
            return self.team.update_task(
                arguments["task_id"],
                actor=arguments["actor"],
                claim_token=arguments["claim_token"],
                note=arguments["note"],
                result=arguments.get("result"),
                tests=arguments.get("tests"),
                commit_sha=arguments.get("commit_sha"),
            )
        if name == "team_submit_task":
            return self.team.submit_task(
                arguments["task_id"],
                actor=arguments["actor"],
                claim_token=arguments["claim_token"],
                result=arguments["result"],
                tests=arguments["tests"],
                commit_sha=arguments.get("commit_sha", ""),
            )
        if name == "team_block_task":
            return self.team.block_task(
                arguments["task_id"],
                actor=arguments["actor"],
                reason=arguments["reason"],
                claim_token=arguments.get("claim_token"),
            )
        if name == "team_unblock_task":
            return self.team.unblock_task(
                arguments["task_id"],
                actor=arguments["actor"],
            )
        if name == "team_cancel_task":
            return self.team.cancel_task(
                arguments["task_id"],
                actor=arguments["actor"],
                reason=arguments["reason"],
            )
        if name == "team_review_task":
            return self.team.review_task(
                arguments["task_id"],
                actor=arguments["actor"],
                approve=_required_boolean(arguments, "approve"),
                notes=arguments["notes"],
                merge_commit=arguments.get("merge_commit", ""),
            )
        if name == "team_add_message":
            return self.team.add_message(
                arguments["repo_root"],
                sender=arguments["sender"],
                body=arguments["body"],
                recipient=arguments.get("recipient"),
                task_id=arguments.get("task_id"),
            )
        if name == "team_list_messages":
            return {
                "messages": self.team.list_messages(
                    arguments["repo_root"],
                    task_id=arguments.get("task_id"),
                    limit=int(arguments.get("limit", 50)),
                )
            }
        if name == "team_cluster_start":
            goal = self.cluster.create_goal(
                arguments["repo_root"],
                objective=arguments["objective"],
                autonomy=arguments.get("autonomy", "auto"),
                max_worker_attempts=int(arguments.get("max_worker_attempts", 3)),
                max_lead_attempts=int(arguments.get("max_lead_attempts", 5)),
                workstream_key=arguments.get("workstream_key"),
                continuity_reason=arguments.get("continuity_reason", ""),
            )
            return {
                "goal": self.cluster.get_goal(goal["goal_id"]),
                "daemon": ensure_daemon(self.settings),
            }
        if name == "team_cluster_ensure":
            result = self.cluster.ensure_goal(
                arguments["repo_root"],
                objective=arguments["objective"],
                autonomy=arguments.get("autonomy", "auto"),
                max_worker_attempts=int(arguments.get("max_worker_attempts", 3)),
                max_lead_attempts=int(arguments.get("max_lead_attempts", 5)),
                workstream_key=arguments.get("workstream_key"),
                continuity_reason=arguments.get("continuity_reason", ""),
            )
            return {
                **result,
                "daemon": ensure_daemon(self.settings),
            }
        if name == "team_cluster_status":
            goal_id = arguments.get("goal_id")
            return {
                "daemon": cluster_daemon_status(self.settings),
                "goals": (
                    [self.cluster.get_goal(goal_id)]
                    if goal_id
                    else self.cluster.list_goals()
                ),
            }
        if name == "team_cluster_control":
            action = arguments["action"]
            if action not in {"pause", "resume", "cancel", "archive", "restore"}:
                raise TeamError(
                    "action must be pause, resume, cancel, archive, or restore"
                )
            reason = f"{action} requested through MCP"
            if action in {"archive", "restore"}:
                goal = self.cluster.set_goal_archived(
                    arguments["goal_id"],
                    action == "archive",
                    actor="human",
                )
            elif action == "cancel":
                goal = self.cluster.cancel_goal(
                    arguments["goal_id"],
                    reason=reason,
                )
            else:
                status = "paused" if action == "pause" else "active"
                goal = self.cluster.set_goal_status(
                    arguments["goal_id"],
                    status,
                    reason=reason,
                )
            return {
                "goal": goal,
                "daemon": (
                    ensure_daemon(self.settings)
                    if action == "resume"
                    else cluster_daemon_status(self.settings)
                ),
            }
        if name == "team_goal_set_workstream":
            return self.cluster.set_goal_workstream(
                arguments["goal_id"],
                workstream_key=arguments.get("workstream_key"),
                continuity_reason=arguments.get("continuity_reason"),
                actor="human",
            )
        if name == "team_goal_set_checkpoint":
            return self.cluster.set_goal_checkpoint(
                arguments["goal_id"],
                summary=arguments["summary"],
                actor="lead",
            )
        if name == "team_agent_list":
            return {
                "agents": self.cluster.list_agents(
                    arguments.get("project_id"),
                    include_archived=_optional_boolean(
                        arguments,
                        "include_archived",
                        default=False,
                    ),
                )
            }
        if name == "team_agent_reset":
            return self.cluster.reset_agent(
                arguments["agent_id"],
                actor=arguments.get("actor", "human"),
                reason=arguments.get(
                    "reason",
                    "manual reset requested through MCP",
                ),
            )
        if name == "team_agent_register_native":
            return self.cluster.register_native_agent(
                arguments["repo_root"],
                arguments["thread_id"],
                arguments["role"],
                arguments["workstream_key"],
                actor=arguments["actor"],
                task_id=arguments.get("task_id"),
                display_name=arguments.get("display_name"),
                model=arguments.get("model", ""),
                model_provider=arguments.get("model_provider", ""),
            )
        raise TeamError(f"unknown tool: {name}")


def _tool_result(value: Any, *, is_error: bool = False) -> dict[str, Any]:
    if value is None:
        text = "OK"
    elif isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, indent=2)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": value if isinstance(value, dict) else None,
        "isError": is_error,
    }


def _read_message(stream: Any) -> dict[str, Any] | None:
    while True:
        line = stream.readline()
        if not line:
            return None
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower().startswith(b"content-length:"):
            length = int(stripped.split(b":", 1)[1].strip())
            while True:
                header = stream.readline()
                if not header or not header.strip():
                    break
            return json.loads(stream.read(length).decode("utf-8"))
        return json.loads(stripped.decode("utf-8"))


def _write_message(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _process(server: TeamMcpServer, message: dict[str, Any]) -> dict[str, Any] | None:
    message_id = message.get("id")
    method = message.get("method")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    if message_id is None:
        return None
    try:
        if method == "initialize":
            result = server.initialize(params)
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": _visible_tool_definitions()}
        elif method == "tools/call":
            name = str(params.get("name") or "")
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                raise TeamError("tool arguments must be an object")
            normalized_name, normalized_arguments = _normalize_tool_call(
                name,
                arguments,
            )
            _validate_tool_arguments(normalized_name, normalized_arguments)
            _guard_tool_call(normalized_name, normalized_arguments)
            result = _tool_result(
                server.call_tool(normalized_name, normalized_arguments)
            )
        elif method in {"resources/list", "prompts/list"}:
            result = {"resources": []} if method == "resources/list" else {"prompts": []}
        elif method == "logging/setLevel":
            result = {}
        else:
            raise TeamError(f"method not found: {method}")
        return {"jsonrpc": "2.0", "id": message_id, "result": result}
    except (TeamError, SchemaValidationError) as exc:
        LOGGER.warning("MCP request rejected: %s", exc)
        if method == "tools/call":
            return {
                "jsonrpc": "2.0",
                "id": message_id,
                "result": _tool_result(f"Tool error: {exc}", is_error=True),
            }
        return {
            "jsonrpc": "2.0",
            "id": message_id,
            "error": {"code": -32602, "message": str(exc)},
        }
    except Exception as exc:
        LOGGER.exception("MCP request failed: %s", method)
        if method == "tools/call":
            return {
                "jsonrpc": "2.0",
                "id": message_id,
                "result": _tool_result(f"Tool error: {exc}", is_error=True),
            }
        return {
            "jsonrpc": "2.0",
            "id": message_id,
            "error": {"code": -32603, "message": str(exc)},
        }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    server = TeamMcpServer(Settings.load())
    while True:
        try:
            message = _read_message(sys.stdin.buffer)
        except (ValueError, json.JSONDecodeError) as exc:
            LOGGER.error("invalid MCP message: %s", exc)
            continue
        if message is None:
            return 0
        response = _process(server, message)
        if response is not None:
            _write_message(response)
