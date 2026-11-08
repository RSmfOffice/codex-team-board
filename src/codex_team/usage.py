from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any


RAW_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "uncached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "context_tokens",
    "model_context_window",
)

CORE_COST_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
)

COST_EVIDENCE_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "uncached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)

JOB_USAGE_FIELDS = RAW_USAGE_FIELDS + (
    "first_input_tokens",
    "first_cached_input_tokens",
    "first_uncached_input_tokens",
)

_ALIASES = {
    "input_tokens": ("input_tokens", "input", "inputTokens"),
    "cached_input_tokens": (
        "cached_input_tokens",
        "cached",
        "cachedInputTokens",
    ),
    "uncached_input_tokens": (
        "uncached_input_tokens",
        "uncached",
        "uncachedInputTokens",
    ),
    "cache_write_input_tokens": (
        "cache_write_input_tokens",
        "cache_write",
        "cacheWriteInputTokens",
    ),
    "output_tokens": ("output_tokens", "output", "outputTokens"),
    "reasoning_output_tokens": (
        "reasoning_output_tokens",
        "reasoning",
        "reasoningOutputTokens",
    ),
    "context_tokens": (
        "context_tokens",
        "context",
        "contextTokens",
        "last_context_tokens",
    ),
    "model_context_window": (
        "model_context_window",
        "modelContextWindow",
        "context_window",
        "contextWindow",
    ),
}


def _integer(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return max(0, number)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def normalize_usage(value: Mapping[str, Any] | None) -> dict[str, int | None]:
    """Normalize one usage snapshot without inventing missing values."""

    source = _mapping(value)
    nested = "total" in source or "last" in source
    selected: Mapping[str, Any] = source
    if nested:
        selected = _mapping(source.get("last")) or _mapping(source.get("total"))

    normalized: dict[str, int | None] = {}
    for field, aliases in _ALIASES.items():
        raw = next(
            (
                selected.get(alias)
                for alias in aliases
                if selected.get(alias) is not None
            ),
            None,
        )
        if raw is None and nested and field == "model_context_window":
            raw = next(
                (
                    source.get(alias)
                    for alias in aliases
                    if source.get(alias) is not None
                ),
                None,
            )
        if raw is None and nested:
            for section_name in ("last", "total"):
                section = _mapping(source.get(section_name))
                raw = next(
                    (
                        section.get(alias)
                        for alias in aliases
                        if section.get(alias) is not None
                    ),
                    None,
                )
                if raw is not None:
                    break
        normalized[field] = _integer(raw)

    if normalized["uncached_input_tokens"] is None:
        input_tokens = normalized["input_tokens"]
        cached_tokens = normalized["cached_input_tokens"]
        if input_tokens is not None and cached_tokens is not None:
            normalized["uncached_input_tokens"] = max(
                0, input_tokens - cached_tokens
            )
    if (
        normalized["input_tokens"] is None
        and normalized["uncached_input_tokens"] is not None
        and normalized["cached_input_tokens"] is not None
    ):
        normalized["input_tokens"] = (
            normalized["uncached_input_tokens"]
            + normalized["cached_input_tokens"]
        )
    return normalized


def usage_has_cost_evidence(usage: Mapping[str, Any] | None) -> bool:
    """Return whether a snapshot contains any billable token evidence."""

    normalized = normalize_usage(usage)
    return any(
        normalized[field] is not None
        for field in COST_EVIDENCE_USAGE_FIELDS
    )


def usage_has_any_evidence(usage: Mapping[str, Any] | None) -> bool:
    """Return whether a snapshot contains token or context metadata."""

    normalized = normalize_usage(usage)
    return any(value is not None for value in normalized.values())


def usage_field_is_present(
    usage: Mapping[str, Any] | None,
    field: str,
) -> bool:
    """Return whether an alias for a field exists with a non-null value."""

    if field not in _ALIASES:
        return False
    source = _mapping(usage)

    def present_in(candidate: Mapping[str, Any]) -> bool:
        return any(
            _integer(candidate.get(alias)) is not None
            for alias in _ALIASES[field]
        )

    if present_in(source):
        return True
    if "total" not in source and "last" not in source:
        return False
    return any(
        present_in(_mapping(source.get(section_name)))
        for section_name in ("last", "total")
    )


def usage_missing_fields(
    usage: Mapping[str, Any] | None,
    fields: tuple[str, ...] = RAW_USAGE_FIELDS,
) -> list[str]:
    """List usage fields absent from a normalized snapshot."""

    normalized = normalize_usage(usage)
    missing: list[str] = []
    for field in fields:
        if (
            field == "uncached_input_tokens"
            and normalized.get(field) is not None
        ):
            continue
        if not usage_field_is_present(usage, field):
            missing.append(field)
    return missing


def merge_usage(
    *values: Mapping[str, Any] | None,
) -> dict[str, int | None]:
    normalized = [normalize_usage(value) for value in values]
    return {
        field: next(
            (
                item[field]
                for item in normalized
                if item.get(field) is not None
            ),
            None,
        )
        for field in RAW_USAGE_FIELDS
    }


def cache_hit_rate(usage: Mapping[str, Any] | None) -> float | None:
    normalized = normalize_usage(usage)
    input_tokens = normalized["input_tokens"]
    cached_tokens = normalized["cached_input_tokens"]
    uncached_tokens = normalized["uncached_input_tokens"]
    denominator = input_tokens
    if denominator is None and cached_tokens is not None and uncached_tokens is not None:
        denominator = cached_tokens + uncached_tokens
    if not denominator or cached_tokens is None:
        return None
    return max(0.0, min(1.0, cached_tokens / denominator))


def usage_is_cost_complete(usage: Mapping[str, Any] | None) -> bool:
    normalized = normalize_usage(usage)
    return (
        all(
            normalized[field] is not None
            and usage_field_is_present(usage, field)
            for field in CORE_COST_USAGE_FIELDS
        )
        and normalized["uncached_input_tokens"] is not None
    )


def usage_from_exec_log(path: str | Path) -> dict[str, Any]:
    """Read the final turn usage from a Codex exec JSONL log."""

    final_usage: dict[str, int | None] = normalize_usage(None)
    first_usage: dict[str, int | None] | None = None
    log_path = Path(path)
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                if not line.startswith("{"):
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, Mapping):
                    continue
                if event.get("type") == "turn.completed":
                    snapshot = normalize_usage(_mapping(event.get("usage")))
                    final_usage = merge_usage(snapshot, final_usage)
                    if first_usage is None:
                        first_usage = snapshot
    except OSError:
        return {
            **normalize_usage(None),
            "first_usage": normalize_usage(None),
            "usage_source": None,
            "usage_missing_fields": list(RAW_USAGE_FIELDS),
        }
    return {
        **final_usage,
        "first_usage": first_usage or normalize_usage(None),
        "usage_source": (
            "turn.completed.usage"
            if usage_has_cost_evidence(final_usage)
            else None
        ),
        "usage_missing_fields": usage_missing_fields(final_usage),
    }
