from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


def bundled_manifest_path() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "templates"
        / "prompt-contract.json"
    )


def _load_bundled_manifest() -> dict[str, Any]:
    try:
        loaded = json.loads(
            bundled_manifest_path().read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"invalid Prompt Contract manifest: {exc}"
        ) from exc
    if not isinstance(loaded, dict):
        raise RuntimeError("Prompt Contract manifest must be a JSON object")
    return loaded


_MANIFEST = _load_bundled_manifest()
PROMPT_CONTRACT_VERSION = str(_MANIFEST["contract_version"])
ACCESS_MODES = frozenset(
    str(value) for value in _MANIFEST["access_modes"]
)
SURFACES = frozenset(str(value) for value in _MANIFEST["surfaces"])


def _manifest_instruction(name: str) -> str:
    instructions = _MANIFEST["mcp_instructions"]
    if not isinstance(instructions, dict):
        raise RuntimeError(
            "Prompt Contract manifest mcp_instructions must be an object"
        )
    value = instructions.get(name)
    if not isinstance(value, str) or not value:
        raise RuntimeError(
            f"Prompt Contract manifest instruction is missing: {name}"
        )
    return value


INSTRUCTIONS = _manifest_instruction("admin")
CORE_INSTRUCTIONS = _manifest_instruction("core_full")
READ_ONLY_CORE_INSTRUCTIONS = _manifest_instruction("core_read_only")
DISABLED_INSTRUCTIONS = _manifest_instruction("disabled")


def configured_access_mode() -> str:
    mode = os.environ.get("CODEX_TEAM_MCP_ACCESS", "full").strip().lower()
    if mode not in ACCESS_MODES:
        raise ValueError(
            "CODEX_TEAM_MCP_ACCESS must be full, read_only, or off"
        )
    return mode


def instructions_for_surface(
    surface: str,
    access_mode: str | None = None,
) -> str:
    selected_surface = str(surface or "core").strip().lower()
    selected_access = str(
        access_mode or configured_access_mode()
    ).strip().lower()
    if selected_access not in ACCESS_MODES:
        raise ValueError(
            "CODEX_TEAM_MCP_ACCESS must be full, read_only, or off"
        )
    if selected_surface not in SURFACES:
        raise ValueError(
            "CODEX_TEAM_MCP_SURFACE must be core, admin, or legacy"
        )
    if selected_access == "off":
        return DISABLED_INSTRUCTIONS
    if selected_surface == "core":
        return (
            READ_ONLY_CORE_INSTRUCTIONS
            if selected_access == "read_only"
            else CORE_INSTRUCTIONS
        )
    return INSTRUCTIONS


def bundled_manifest_hash() -> str:
    path = bundled_manifest_path()
    content = path.read_bytes() if path.is_file() else b""
    return hashlib.sha256(content).hexdigest()
