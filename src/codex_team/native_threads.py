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


def _read_session_metadata(path: Path) -> Mapping[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                if not line.startswith("{"):
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") != "session_meta":
                    continue
                payload = event.get("payload")
                return payload if isinstance(payload, Mapping) else None
    except OSError:
        return None
    return None


def _collect_native_session_data(
    path: Path,
    *,
    thread_id: str,
    history_start_ordinal: int,
    expected_task_id: str = "",
) -> dict[str, Any]:
    total_usage: dict[str, int] | None = None
    last_usage: dict[str, int] | None = None
    model_context_window: int | None = None
    turn_ids: set[str] = set()
    last_turn_id: str | None = None
    usage_events = 0
    task_prompt_texts: dict[str, str] = {}

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
                ordinal = _optional_non_negative_int(event.get("ordinal"))
                if (
                    ordinal is not None
                    and history_start_ordinal > 0
                    and ordinal < history_start_ordinal
                ):
                    continue

                prompt = _user_prompt_text(event)
                if prompt:
                    for task_id in TASK_ID_RE.findall(prompt):
                        normalized_task = task_id.upper()
                        task_prompt_texts.setdefault(normalized_task, prompt)

                event_type = str(event.get("type") or "")
                payload = event.get("payload")
                if not isinstance(payload, Mapping):
                    continue
                payload_thread_id = _optional_text(payload.get("thread_id"))
                if payload_thread_id and payload_thread_id != thread_id:
                    continue
                payload_type = str(payload.get("type") or "")

                if event_type == "token_usage_record":
                    total_usage = _parse_usage_counts(
                        payload.get("thread_token_usage")
                        or payload.get("total_token_usage")
                    ) or total_usage
                    last_usage = _parse_usage_counts(
                        payload.get("turn_token_usage")
                        or payload.get("usage")
                    ) or last_usage
                    turn_id = _optional_text(payload.get("turn_id"))
                    if turn_id:
                        turn_ids.add(turn_id)
                        last_turn_id = turn_id
                    if total_usage is not None or last_usage is not None:
                        usage_events += 1
                    continue

                if event_type != "event_msg" or payload_type != "token_count":
                    continue
                info = payload.get("info")
                if not isinstance(info, Mapping):
                    continue
                parsed_total = _parse_usage_counts(
                    info.get("total_token_usage")
                )
                parsed_last = _parse_usage_counts(
                    info.get("last_token_usage")
                )
                total_usage = parsed_total or total_usage
                last_usage = parsed_last or last_usage
                window = _optional_non_negative_int(
                    info.get("model_context_window")
                )
                if window is not None and window > 0:
                    model_context_window = window
                if parsed_total is not None or parsed_last is not None:
                    usage_events += 1
    except OSError:
        return {
            "task_ids": [],
            "task_prompt_text": "",
            "usage": _unknown_native_usage(),
        }

    task_ids = list(task_prompt_texts)
    available = total_usage is not None or last_usage is not None
    if total_usage is None:
        total_usage = last_usage
    if last_usage is None:
        last_usage = total_usage
    context_tokens = (
        int(last_usage["input_tokens"])
        if last_usage is not None
        else None
    )
    usage = {
        "available": available,
        "last_input_tokens": (
            int(last_usage["input_tokens"])
            if last_usage is not None
            else None
        ),
        "last_cached_input_tokens": (
            int(last_usage["cached_input_tokens"])
            if last_usage is not None
            else None
        ),
        "last_uncached_input_tokens": (
            int(last_usage["uncached_input_tokens"])
            if last_usage is not None
            else None
        ),
        "total_input_tokens": (
            int(total_usage["input_tokens"])
            if total_usage is not None
            else None
        ),
        "total_cached_input_tokens": (
            int(total_usage["cached_input_tokens"])
            if total_usage is not None
            else None
        ),
        "total_uncached_input_tokens": (
            int(total_usage["uncached_input_tokens"])
            if total_usage is not None
            else None
        ),
        "output_tokens": (
            int(total_usage["output_tokens"])
            if total_usage is not None
            else None
        ),
        "context_tokens": context_tokens,
        "model_context_window": model_context_window,
        "turn_count": max(
            len(turn_ids),
            1 if usage_events and not turn_ids else 0,
        ),
        "last_turn_id": last_turn_id,
    }
    return {
        "task_ids": task_ids,
        "task_prompt_text": task_prompt_texts.get(
            expected_task_id,
            "",
        ),
        "usage": usage,
    }


def _parse_usage_counts(value: Any) -> dict[str, int] | None:
    if not isinstance(value, Mapping):
        return None
    input_tokens = _optional_non_negative_int(
        value.get("input_tokens", value.get("inputTokens"))
    )
    cached_input_tokens = _optional_non_negative_int(
        value.get(
            "cached_input_tokens",
            value.get("cached_tokens", value.get("cachedInputTokens")),
        )
    )
    output_tokens = _optional_non_negative_int(
        value.get("output_tokens", value.get("outputTokens"))
    )
    if (
        input_tokens is None
        and cached_input_tokens is None
        and output_tokens is None
    ):
        return None
    input_tokens = input_tokens or 0
    cached_input_tokens = min(cached_input_tokens or 0, input_tokens)
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "uncached_input_tokens": input_tokens - cached_input_tokens,
        "output_tokens": output_tokens or 0,
    }


def _optional_non_negative_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _unknown_native_usage(
    model_context_window: int | None = None,
) -> dict[str, Any]:
    return {
        "available": False,
        "last_input_tokens": None,
        "last_cached_input_tokens": None,
        "last_uncached_input_tokens": None,
        "total_input_tokens": None,
        "total_cached_input_tokens": None,
        "total_uncached_input_tokens": None,
        "output_tokens": None,
        "context_tokens": None,
        "model_context_window": model_context_window,
        "turn_count": 0,
        "last_turn_id": None,
    }


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
            "usage": _unknown_native_usage(),
        }

    metadata = _read_session_metadata(path) or {}
    history_start_ordinal = (
        _optional_non_negative_int(
            metadata.get("subagent_history_start_ordinal")
        )
        or 0
    )
    parent_thread_id: str | None = None
    agent_path: str | None = None
    agent_nickname: str | None = None
    agent_role: str | None = None
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
    if isinstance(spawn, Mapping):
        parent_thread_id = _optional_text(
            spawn.get("parent_thread_id")
        )
        agent_path = _optional_text(spawn.get("agent_path"))
        agent_nickname = _optional_text(
            spawn.get("agent_nickname")
        )
        agent_role = _optional_text(spawn.get("agent_role"))
    agent_path = agent_path or _optional_text(metadata.get("agent_path"))
    agent_nickname = agent_nickname or _optional_text(
        metadata.get("agent_nickname")
    )
    agent_role = agent_role or _optional_text(metadata.get("agent_role"))
    parent_thread_id = parent_thread_id or _optional_text(
        metadata.get("parent_thread_id")
    )

    collected = _collect_native_session_data(
        path,
        thread_id=normalized,
        history_start_ordinal=history_start_ordinal,
        expected_task_id=expected_task,
    )
    task_ids = collected["task_ids"]
    task_present = bool(expected_task and expected_task in task_ids)
    task_prompt_text = str(collected.get("task_prompt_text") or "")
    usage = dict(collected["usage"])
    metadata_window = _optional_non_negative_int(
        metadata.get("context_window")
    )
    if usage.get("model_context_window") is None and metadata_window:
        usage["model_context_window"] = metadata_window

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
        "history_start_ordinal": history_start_ordinal,
        "task_ids": task_ids,
        "usage": usage,
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
    metadata = _read_session_metadata(path)
    if not isinstance(metadata, Mapping):
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
    history_start_ordinal = (
        _optional_non_negative_int(
            metadata.get("subagent_history_start_ordinal")
        )
        or 0
    )
    collected = _collect_native_session_data(
        path,
        thread_id=thread_id,
        history_start_ordinal=history_start_ordinal,
    )
    usage = dict(collected["usage"])
    metadata_window = _optional_non_negative_int(
        metadata.get("context_window")
    )
    if usage.get("model_context_window") is None and metadata_window:
        usage["model_context_window"] = metadata_window
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
        "task_ids": collected["task_ids"],
        "model_provider": _optional_text(metadata.get("model_provider")) or "",
        "usage": usage,
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
