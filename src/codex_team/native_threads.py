from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping


THREAD_ID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")
TASK_ID_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(T-\d{8}-[A-Za-z0-9]{6})(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
NATIVE_ROLES = frozenset(
    {"lead", "team_explorer", "team_worker", "team_reviewer"}
)


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


def discover_native_sessions(
    *,
    root: str | Path | None = None,
    limit: int = 200,
    max_age_seconds: int | None = None,
) -> list[dict[str, Any]]:
    """Discover Codex subagent sessions without storing message bodies."""

    sessions_root = (
        Path(root).expanduser().resolve()
        if root is not None
        else native_sessions_root()
    )
    if not sessions_root.is_dir():
        return []
    limit = max(1, min(int(limit), 1000))
    cutoff = (
        time.time() - max(1, int(max_age_seconds))
        if max_age_seconds is not None
        else None
    )
    candidates: list[tuple[float, Path]] = []
    for path in sessions_root.rglob("*.jsonl"):
        try:
            modified = path.stat().st_mtime
        except OSError:
            continue
        if cutoff is not None and modified < cutoff:
            continue
        candidates.append((modified, path))
    candidates.sort(key=lambda item: (item[0], str(item[1])), reverse=True)

    discovered: list[dict[str, Any]] = []
    seen_thread_ids: set[str] = set()
    for modified, path in candidates[:limit]:
        item = _discover_session_file(path, modified)
        if item is None:
            continue
        thread_id = str(item["thread_id"])
        if thread_id in seen_thread_ids:
            continue
        seen_thread_ids.add(thread_id)
        discovered.append(item)
    return discovered


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


def _discover_session_file(
    path: Path,
    modified: float,
) -> dict[str, Any] | None:
    metadata: Mapping[str, Any] | None = None
    task_ids: list[str] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for index, line in enumerate(stream):
                if index >= 200:
                    break
                if not line.startswith("{"):
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, Mapping):
                    continue
                if metadata is None and event.get("type") == "session_meta":
                    payload = event.get("payload")
                    if not isinstance(payload, Mapping):
                        return None
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
                    if not isinstance(spawn, Mapping):
                        return None
                    metadata = payload
                if metadata is not None and not task_ids:
                    prompt = _user_prompt_text(event)
                    if prompt:
                        task_ids = list(
                            dict.fromkeys(
                                match.upper()
                                for match in TASK_ID_RE.findall(prompt)
                            )
                        )
                if metadata is not None and task_ids:
                    break
    except OSError:
        return None
    if metadata is None:
        return None

    source = metadata.get("source")
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
    if not isinstance(spawn, Mapping):
        return None
    thread_id = (
        _optional_text(metadata.get("id"))
        or _thread_id_from_filename(path)
    )
    if not thread_id or not THREAD_ID_RE.fullmatch(thread_id):
        return None
    parent_thread_id = _optional_text(spawn.get("parent_thread_id")) or (
        _optional_text(metadata.get("parent_thread_id"))
    )
    agent_path = _optional_text(spawn.get("agent_path")) or _optional_text(
        metadata.get("agent_path")
    )
    agent_nickname = _optional_text(
        spawn.get("agent_nickname")
    ) or _optional_text(metadata.get("agent_nickname"))
    explicit_role = _optional_text(spawn.get("agent_role")) or _optional_text(
        metadata.get("agent_role")
    )
    return {
        "thread_id": thread_id,
        "session_path": str(path),
        "cwd": _optional_text(metadata.get("cwd")),
        "parent_thread_id": parent_thread_id,
        "agent_path": agent_path,
        "agent_nickname": agent_nickname,
        "agent_role": _infer_native_role(explicit_role, agent_path),
        "task_ids": task_ids,
        "model_provider": _optional_text(metadata.get("model_provider")) or "",
        "last_seen_at": datetime.fromtimestamp(
            modified,
            tz=UTC,
        ).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }


def _thread_id_from_filename(path: Path) -> str | None:
    match = re.search(
        r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
        path.stem,
    )
    return match.group(1) if match else None


def _infer_native_role(
    explicit_role: str | None,
    agent_path: str | None,
) -> str:
    normalized_role = str(explicit_role or "").strip().lower()
    if normalized_role in NATIVE_ROLES:
        return normalized_role
    hint = str(agent_path or "").strip().lower()
    if "review" in hint or "verif" in hint:
        return "team_reviewer"
    if "worker" in hint or "implement" in hint or "fixer" in hint:
        return "team_worker"
    if "lead" in hint:
        return "lead"
    return "team_explorer"


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
