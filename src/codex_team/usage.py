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
    return all(
        normalized[field] is not None
        for field in (
            "input_tokens",
            "cached_input_tokens",
            "uncached_input_tokens",
            "output_tokens",
        )
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
        }
    return {
        **final_usage,
        "first_usage": first_usage or normalize_usage(None),
        "usage_source": (
            "turn.completed.usage"
            if any(value is not None for value in final_usage.values())
            else None
        ),
    }
