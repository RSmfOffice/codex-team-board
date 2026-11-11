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
IDENTITY_SOURCES = frozenset(
    {
        "explicit_session_metadata",
        "agent_path_inference",
        "registration",
        "registry",
        "missing",
        "unknown",
    }
)
IDENTITY_CONFIDENCES = frozenset({"high", "medium", "low", "unknown"})
ASSIGNMENT_SOURCES = frozenset(
    {
        "explicit_session_metadata",
        "unique_session_candidate",
        "registration",
        "registry",
        "ambiguous",
        "none",
    }
)
ASSIGNMENT_CONFIDENCES = IDENTITY_CONFIDENCES
NATIVE_LIFECYCLES = frozenset(
    {"active", "idle", "completed", "aborted", "unknown"}
)
NATIVE_USAGE_STATUSES = frozenset(
    {"available", "partial", "unavailable", "stale"}
)
DEFAULT_NATIVE_STALE_SECONDS = 300


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
    total_present: set[str] = set()
    last_present: set[str] = set()
    model_context_window: int | None = None
    turn_ids: set[str] = set()
    last_turn_id: str | None = None
    task_prompt_texts: dict[str, str] = {}
    lifecycle = "unknown"
    lifecycle_source = "unknown"

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

                lifecycle_signal = _lifecycle_signal(event)
                if lifecycle_signal is not None:
                    lifecycle = lifecycle_signal
                    lifecycle_source = "session_events"

                if event_type == "token_usage_record":
                    parsed_total, parsed_total_present = (
                        _parse_usage_counts_with_presence(
                            payload.get("thread_token_usage")
                            or payload.get("total_token_usage")
                        )
                    )
                    parsed_last, parsed_last_present = (
                        _parse_usage_counts_with_presence(
                            payload.get("turn_token_usage")
                            or payload.get("usage")
                        )
                    )
                    if parsed_total is not None:
                        total_usage = parsed_total
                        total_present = parsed_total_present
                    if parsed_last is not None:
                        last_usage = parsed_last
                        last_present = parsed_last_present
                    turn_id = _optional_text(payload.get("turn_id"))
                    if turn_id:
                        turn_ids.add(turn_id)
                        last_turn_id = turn_id
                    continue

                if event_type != "event_msg" or payload_type != "token_count":
                    continue
                info = payload.get("info")
                if not isinstance(info, Mapping):
                    continue
                parsed_total, parsed_total_present = (
                    _parse_usage_counts_with_presence(
                        info.get("total_token_usage")
                    )
                )
                parsed_last, parsed_last_present = (
                    _parse_usage_counts_with_presence(
                        info.get("last_token_usage")
                    )
                )
                if parsed_total is not None:
                    total_usage = parsed_total
                    total_present = parsed_total_present
                if parsed_last is not None:
                    last_usage = parsed_last
                    last_present = parsed_last_present
                window = _optional_non_negative_int(
                    info.get("model_context_window")
                )
                if window is not None and window > 0:
                    model_context_window = window
    except OSError:
        return {
            "task_ids": [],
            "task_prompt_text": "",
            "usage": _unknown_native_usage(),
            "lifecycle": "unknown",
            "lifecycle_source": "unknown",
        }

    task_ids = sorted(task_prompt_texts)
    available = total_usage is not None or last_usage is not None
    context_tokens = (
        int(last_usage["input_tokens"])
        if last_usage is not None
        else None
    )
    turn_count = len(turn_ids) if turn_ids else None
    missing_fields: set[str] = set()
    if last_usage is None or "input" not in last_present:
        missing_fields.add("last_input_tokens")
    if last_usage is None or "cached" not in last_present:
        missing_fields.add("last_cached_input_tokens")
    if (
        last_usage is None
        or "input" not in last_present
        or "cached" not in last_present
    ):
        missing_fields.add("last_uncached_input_tokens")
    if total_usage is None or "input" not in total_present:
        missing_fields.add("total_input_tokens")
    if total_usage is None or "cached" not in total_present:
        missing_fields.add("total_cached_input_tokens")
    if (
        total_usage is None
        or "input" not in total_present
        or "cached" not in total_present
    ):
        missing_fields.add("total_uncached_input_tokens")
    if total_usage is None or "output" not in total_present:
        missing_fields.add("output_tokens")
    if context_tokens is None:
        missing_fields.add("context_tokens")
    if turn_count is None:
        missing_fields.add("turn_count")
    if last_turn_id is None:
        missing_fields.add("last_turn_id")
    if model_context_window is None:
        missing_fields.add("model_context_window")
    usage = {
        "available": available,
        "status": (
            "unavailable"
            if not available
            else "partial"
            if missing_fields
            else "available"
        ),
        "missing_fields": sorted(missing_fields),
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
        "turn_count": turn_count,
        "last_turn_id": last_turn_id,
    }
    return {
        "task_ids": task_ids,
        "task_prompt_text": task_prompt_texts.get(
            expected_task_id,
            "",
        ),
        "usage": usage,
        "lifecycle": lifecycle if lifecycle != "unknown" else "idle",
        "lifecycle_source": (
            lifecycle_source
            if lifecycle_source != "unknown"
            else "session_metadata"
        ),
    }


def _parse_usage_counts_with_presence(
    value: Any,
) -> tuple[dict[str, int] | None, set[str]]:
    parsed = _parse_usage_counts(value)
    if parsed is None or not isinstance(value, Mapping):
        return parsed, set()
    present: set[str] = set()
    if _optional_non_negative_int(
        value.get("input_tokens", value.get("inputTokens"))
    ) is not None:
        present.add("input")
    if _optional_non_negative_int(
        value.get(
            "cached_input_tokens",
            value.get("cached_tokens", value.get("cachedInputTokens")),
        )
    ) is not None:
        present.add("cached")
    if _optional_non_negative_int(
        value.get("output_tokens", value.get("outputTokens"))
    ) is not None:
        present.add("output")
    return parsed, present


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
        "status": "unavailable",
        "missing_fields": [
            "last_input_tokens",
            "last_cached_input_tokens",
            "last_uncached_input_tokens",
            "total_input_tokens",
            "total_cached_input_tokens",
            "total_uncached_input_tokens",
            "output_tokens",
            "context_tokens",
            "turn_count",
            "last_turn_id",
            "model_context_window",
        ],
        "last_input_tokens": None,
        "last_cached_input_tokens": None,
        "last_uncached_input_tokens": None,
        "total_input_tokens": None,
        "total_cached_input_tokens": None,
        "total_uncached_input_tokens": None,
        "output_tokens": None,
        "context_tokens": None,
        "model_context_window": model_context_window,
        "turn_count": None,
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
    selected, _source, _count = _select_native_session(
        thread_id,
        root=root,
    )
    return selected


def _select_native_session(
    thread_id: str,
    *,
    root: str | Path | None = None,
) -> tuple[Path | None, str, int]:
    normalized = str(thread_id or "").strip()
    if not THREAD_ID_RE.fullmatch(normalized):
        return None, "none", 0
    sessions_root = (
        Path(root).expanduser().resolve()
        if root is not None
        else native_sessions_root()
    )
    if not sessions_root.is_dir():
        return None, "none", 0
    matches = list(sessions_root.glob(f"**/*{normalized}.jsonl"))
    if not matches:
        return None, "none", 0

    verified: list[tuple[float, str, Path]] = []
    fallback: list[tuple[float, str, Path]] = []
    for path in matches:
        try:
            modified = path.stat().st_mtime
        except OSError:
            continue
        metadata = _read_session_metadata(path)
        metadata_id = (
            _optional_text(metadata.get("id"))
            if isinstance(metadata, Mapping)
            else None
        )
        item = (modified, str(path), path)
        if metadata_id == normalized:
            verified.append(item)
        elif metadata_id is None:
            fallback.append(item)

    if verified:
        selected = max(verified, key=lambda item: (item[0], item[1]))
        return selected[2], "metadata_and_mtime", len(matches)
    if len(fallback) == 1:
        return fallback[0][2], "unique_filename_fallback", len(matches)
    return None, "ambiguous_unverified", len(matches)


def inspect_native_session(
    thread_id: str,
    *,
    task_id: str | None = None,
    root: str | Path | None = None,
    stale_after_seconds: int = DEFAULT_NATIVE_STALE_SECONDS,
) -> dict[str, Any]:
    normalized = str(thread_id or "").strip()
    expected_task = str(task_id or "").strip()
    path, selection_source, candidate_count = _select_native_session(
        normalized,
        root=root,
    )
    if path is None:
        return {
            "thread_id": normalized,
            "session_found": False,
            "session_path": None,
            "session_candidate_count": candidate_count,
            "session_selection_source": selection_source,
            "parent_thread_id": None,
            "agent_path": None,
            "agent_nickname": None,
            "agent_role": None,
            "identity_source": "missing",
            "identity_confidence": "unknown",
            "lifecycle": "unknown",
            "lifecycle_source": "unknown",
            "session_stale": True,
            "task_id_present": False,
            "prompt_status": "missing" if expected_task else "unknown",
            "prompt_hash": "",
            "last_seen_at": None,
            "task_ids": [],
            "task_candidates": [],
            "assignment_task_id": None,
            "assignment_source": "none",
            "assignment_confidence": "unknown",
            "task_assignment_ambiguous": False,
            "usage": _unknown_native_usage(),
        }

    metadata = _read_session_metadata(path) or {}
    identity = _session_identity(metadata)
    history_start_ordinal = (
        _optional_non_negative_int(
            metadata.get("subagent_history_start_ordinal")
        )
        or 0
    )
    collected = _collect_native_session_data(
        path,
        thread_id=normalized,
        history_start_ordinal=history_start_ordinal,
        expected_task_id=expected_task,
    )
    task_candidates = sorted(set(collected["task_ids"]))
    explicit_tasks = _explicit_task_ids(metadata)
    (
        assignment_task_id,
        assignment_source,
        assignment_confidence,
        assignment_ambiguous,
    ) = _resolve_assignment(task_candidates, explicit_tasks)
    task_present = bool(
        expected_task and expected_task in task_candidates
    )
    task_prompt_text = str(collected.get("task_prompt_text") or "")
    usage = dict(collected["usage"])
    metadata_window = _optional_non_negative_int(
        metadata.get("context_window")
    )
    if usage.get("model_context_window") is None and metadata_window:
        usage["model_context_window"] = metadata_window
        usage["missing_fields"] = [
            field
            for field in usage.get("missing_fields", [])
            if field != "model_context_window"
        ]
        if usage.get("available") and not usage["missing_fields"]:
            usage["status"] = "available"

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
    try:
        modified_epoch = path.stat().st_mtime
    except OSError:
        modified_epoch = 0
    session_stale = (
        not bool(metadata)
        or modified_epoch <= 0
        or time.time() - modified_epoch
        > max(1, int(stale_after_seconds))
    )
    lifecycle = str(collected.get("lifecycle") or "unknown")
    lifecycle_source = str(
        collected.get("lifecycle_source") or "unknown"
    )
    if lifecycle == "active" and session_stale:
        lifecycle = "idle"
        lifecycle_source = "mtime"
    modified = datetime.fromtimestamp(
        modified_epoch,
        tz=UTC,
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return {
        "thread_id": normalized,
        "session_found": True,
        "session_path": str(path),
        "session_candidate_count": candidate_count,
        "session_selection_source": selection_source,
        "parent_thread_id": identity["parent_thread_id"],
        "agent_path": identity["agent_path"],
        "agent_nickname": identity["agent_nickname"],
        "agent_role": identity["agent_role"],
        "identity_source": identity["identity_source"],
        "identity_confidence": identity["identity_confidence"],
        "lifecycle": lifecycle,
        "lifecycle_source": lifecycle_source,
        "session_stale": session_stale,
        "task_id_present": task_present,
        "prompt_status": prompt_status,
        "prompt_hash": prompt_hash,
        "last_seen_at": modified,
        "history_start_ordinal": history_start_ordinal,
        "task_ids": task_candidates,
        "task_candidates": task_candidates,
        "assignment_task_id": assignment_task_id,
        "assignment_source": assignment_source,
        "assignment_confidence": assignment_confidence,
        "task_assignment_ambiguous": assignment_ambiguous,
        "usage": usage,
    }


def _session_identity(metadata: Mapping[str, Any]) -> dict[str, Any]:
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
    spawn_mapping = spawn if isinstance(spawn, Mapping) else {}
    parent_thread_id = (
        _optional_text(spawn_mapping.get("parent_thread_id"))
        or _optional_text(metadata.get("parent_thread_id"))
    )
    agent_path = (
        _optional_text(spawn_mapping.get("agent_path"))
        or _optional_text(metadata.get("agent_path"))
    )
    agent_nickname = (
        _optional_text(spawn_mapping.get("agent_nickname"))
        or _optional_text(metadata.get("agent_nickname"))
    )
    explicit_role = (
        _optional_text(spawn_mapping.get("agent_role"))
        or _optional_text(metadata.get("agent_role"))
    )
    role, identity_source, confidence = _resolve_native_role(
        explicit_role,
        agent_path,
    )
    return {
        "parent_thread_id": parent_thread_id,
        "agent_path": agent_path,
        "agent_nickname": agent_nickname,
        "agent_role": role,
        "identity_source": identity_source,
        "identity_confidence": confidence,
    }


def _explicit_task_ids(metadata: Mapping[str, Any]) -> list[str]:
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
    containers: list[Mapping[str, Any]] = [metadata]
    if isinstance(spawn, Mapping):
        containers.append(spawn)
        assignment = spawn.get("assignment")
        if isinstance(assignment, Mapping):
            containers.append(assignment)
    assignment = metadata.get("assignment")
    if isinstance(assignment, Mapping):
        containers.append(assignment)

    candidates: set[str] = set()
    for container in containers:
        for field in (
            "assignment_task_id",
            "task_id",
            "bound_task_id",
        ):
            value = _optional_text(container.get(field))
            if value and TASK_ID_RE.fullmatch(value):
                candidates.add(value.upper())
    return sorted(candidates)


def _resolve_assignment(
    candidates: list[str],
    explicit_tasks: list[str],
) -> tuple[str | None, str, str, bool]:
    normalized_candidates = sorted(
        {str(value).upper() for value in candidates if value}
    )
    normalized_explicit = sorted(
        {str(value).upper() for value in explicit_tasks if value}
    )
    if len(normalized_explicit) == 1:
        return (
            normalized_explicit[0],
            "explicit_session_metadata",
            "high",
            False,
        )
    if len(normalized_explicit) > 1:
        return None, "ambiguous", "unknown", True
    if len(normalized_candidates) == 1:
        return (
            normalized_candidates[0],
            "unique_session_candidate",
            "medium",
            False,
        )
    if len(normalized_candidates) > 1:
        return None, "ambiguous", "unknown", True
    return None, "none", "unknown", False


def _lifecycle_signal(event: Mapping[str, Any]) -> str | None:
    event_type = str(event.get("type") or "").strip()
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        return None
    payload_type = str(payload.get("type") or "").strip()
    if event_type == "event_msg":
        if payload_type in {"task_complete", "turn_complete", "turn_completed"}:
            return "completed"
        if payload_type in {"task_aborted", "turn_aborted", "thread_aborted"}:
            return "aborted"
        if payload_type in {"task_started", "turn_started", "user_message"}:
            return "active"
        return None
    if event_type == "response_item":
        if payload_type in {
            "message",
            "reasoning",
            "function_call",
            "function_call_output",
            "tool_search_call",
            "tool_search_output",
            "agent_message",
        }:
            return "active"
    return None


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
    metadata_id = _optional_text(metadata.get("id"))
    filename_id = _thread_id_from_filename(path)
    if metadata_id and filename_id and metadata_id != filename_id:
        return None
    thread_id = metadata_id or filename_id
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
        usage["missing_fields"] = [
            field
            for field in usage.get("missing_fields", [])
            if field != "model_context_window"
        ]
        if usage.get("available") and not usage["missing_fields"]:
            usage["status"] = "available"
    identity = _session_identity(metadata)
    task_candidates = sorted(set(collected["task_ids"]))
    (
        assignment_task_id,
        assignment_source,
        assignment_confidence,
        assignment_ambiguous,
    ) = _resolve_assignment(
        task_candidates,
        _explicit_task_ids(metadata),
    )
    session_stale = time.time() - modified > DEFAULT_NATIVE_STALE_SECONDS
    lifecycle = str(collected.get("lifecycle") or "unknown")
    lifecycle_source = str(
        collected.get("lifecycle_source") or "unknown"
    )
    if lifecycle == "active" and session_stale:
        lifecycle = "idle"
        lifecycle_source = "mtime"
    return {
        "thread_id": thread_id,
        "session_path": str(path),
        "cwd": _optional_text(metadata.get("cwd")),
        "parent_thread_id": identity["parent_thread_id"],
        "agent_path": identity["agent_path"],
        "agent_nickname": identity["agent_nickname"],
        "agent_role": identity["agent_role"],
        "identity_source": identity["identity_source"],
        "identity_confidence": identity["identity_confidence"],
        "lifecycle": lifecycle,
        "lifecycle_source": lifecycle_source,
        "session_stale": session_stale,
        "task_ids": task_candidates,
        "task_candidates": task_candidates,
        "assignment_task_id": assignment_task_id,
        "assignment_source": assignment_source,
        "assignment_confidence": assignment_confidence,
        "task_assignment_ambiguous": assignment_ambiguous,
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


def _resolve_native_role(
    explicit_role: str | None,
    agent_path: str | None,
) -> tuple[str | None, str, str]:
    normalized_role = str(explicit_role or "").strip().lower()
    if normalized_role in NATIVE_ROLES:
        return normalized_role, "explicit_session_metadata", "high"
    hint = str(agent_path or "").strip().lower()
    if "review" in hint or "verif" in hint:
        return "team_reviewer", "agent_path_inference", "low"
    if "worker" in hint or "implement" in hint or "fixer" in hint:
        return "team_worker", "agent_path_inference", "low"
    if "lead" in hint:
        return "lead", "agent_path_inference", "low"
    return None, "missing", "unknown"


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
