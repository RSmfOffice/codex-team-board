from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping


THREAD_ID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")


def native_sessions_root() -> Path:
    configured = os.environ.get("CODEX_NATIVE_SESSIONS_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    codex_home = os.environ.get("CODEX_HOME", "").strip()
    base = (
        Path(codex_home).expanduser()
        if codex_home
        else Path.home() / ".codex"
    )
    return (base / "sessions").resolve()


def find_native_session(
    thread_id: str,
    *,
    root: str | Path | None = None,
) -> Path | None:
    normalized = str(thread_id or "").strip()
    if not THREAD_ID_RE.fullmatch(normalized):
        return None
    sessions_root = (
        Path(root).expanduser().resolve()
        if root is not None
        else native_sessions_root()
    )
    if not sessions_root.is_dir():
        return None
    matches = sorted(sessions_root.glob(f"**/*{normalized}.jsonl"))
    return matches[-1] if matches else None


def inspect_native_session(
    thread_id: str,
    *,
    task_id: str | None = None,
    root: str | Path | None = None,
) -> dict[str, Any]:
    normalized = str(thread_id or "").strip()
    expected_task = str(task_id or "").strip()
    path = find_native_session(normalized, root=root)
    if path is None:
        return {
            "thread_id": normalized,
            "session_found": False,
            "session_path": None,
            "parent_thread_id": None,
            "agent_path": None,
            "agent_nickname": None,
            "agent_role": None,
            "task_id_present": False,
            "prompt_status": "missing" if expected_task else "unknown",
            "prompt_hash": "",
            "last_seen_at": None,
        }

    parent_thread_id: str | None = None
    agent_path: str | None = None
    agent_nickname: str | None = None
    agent_role: str | None = None
    task_present = False
    task_prompt_text = ""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                if not line.startswith("{"):
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, Mapping):
                    continue
                user_prompt_text = _user_prompt_text(event)
                if (
                    expected_task
                    and not task_present
                    and _contains_task_id(user_prompt_text, expected_task)
                ):
                    task_present = True
                    task_prompt_text = user_prompt_text
                if event.get("type") != "session_meta":
                    continue
                payload = event.get("payload")
                if not isinstance(payload, Mapping):
                    continue
                source = payload.get("source")
                subagent = (
                    source.get("subagent")
                    if isinstance(source, Mapping)
                    else None
                )
                spawn = (
                    subagent.get("thread_spawn")
                    if isinstance(subagent, Mapping)
                    else None
                )
                if isinstance(spawn, Mapping):
                    parent_thread_id = _optional_text(
                        spawn.get("parent_thread_id")
                    )
                    agent_path = _optional_text(spawn.get("agent_path"))
                    agent_nickname = _optional_text(
                        spawn.get("agent_nickname")
                    )
                    agent_role = _optional_text(spawn.get("agent_role"))
                agent_path = agent_path or _optional_text(
                    payload.get("agent_path")
                )
                agent_nickname = agent_nickname or _optional_text(
                    payload.get("agent_nickname")
                )
                agent_role = agent_role or _optional_text(
                    payload.get("agent_role")
                )
                parent_thread_id = parent_thread_id or _optional_text(
                    payload.get("parent_thread_id")
                )
    except OSError:
        return {
            "thread_id": normalized,
            "session_found": False,
            "session_path": str(path),
            "parent_thread_id": parent_thread_id,
            "agent_path": agent_path,
            "agent_nickname": agent_nickname,
            "agent_role": agent_role,
            "task_id_present": False,
            "prompt_status": "missing" if expected_task else "unknown",
            "prompt_hash": "",
            "last_seen_at": None,
        }

    prompt_status = (
        "delivered"
        if task_present
        else "missing"
        if expected_task
        else "unknown"
    )
    prompt_hash = (
        hashlib.sha256(task_prompt_text.encode("utf-8")).hexdigest()
        if task_present
        else ""
    )
    modified = datetime.fromtimestamp(
        path.stat().st_mtime,
        tz=UTC,
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return {
        "thread_id": normalized,
        "session_found": True,
        "session_path": str(path),
        "parent_thread_id": parent_thread_id,
        "agent_path": agent_path,
        "agent_nickname": agent_nickname,
        "agent_role": agent_role,
        "task_id_present": task_present,
        "prompt_status": prompt_status,
        "prompt_hash": prompt_hash,
        "last_seen_at": modified,
    }


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _user_prompt_text(event: Mapping[str, Any]) -> str:
    """Return text only from a real user message, never logs or tool output."""

    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        return ""
    event_type = str(event.get("type") or "").strip()
    payload_type = str(payload.get("type") or "").strip()
    if event_type == "response_item":
        if payload_type != "message" or payload.get("role") != "user":
            return ""
    elif event_type == "event_msg":
        if payload_type != "user_message":
            return ""
    else:
        return ""

    content = payload.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        message = payload.get("message")
        return message if isinstance(message, str) else ""

    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
            continue
        if not isinstance(item, Mapping):
            continue
        if item.get("type") not in {"input_text", "text"}:
            continue
        text = item.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _contains_task_id(text: str, task_id: str) -> bool:
    if not text:
        return False
    pattern = rf"(?<![A-Za-z0-9_-]){re.escape(task_id)}(?![A-Za-z0-9_-])"
    return re.search(pattern, text) is not None
