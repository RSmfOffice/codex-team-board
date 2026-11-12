"""Differential tests proving AgentPool usage normalization matches the legacy
contract exactly (including the ``cached_tokens`` alias), and that the newer
shared-normalize behaviors (nested total/last, input back-derivation) are NOT
introduced into the pool path.
"""

from __future__ import annotations

import unittest
from typing import Any, Mapping

from codex_team.agent_pool import AgentPool


def _legacy_usage_value(usage: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in usage:
            return usage[name]
    return 0


def legacy_normalize_usage(usage: Mapping[str, Any] | None) -> dict[str, int | None]:
    """Independent copy of the pre-refactor pool normalization (reference)."""
    values = usage or {}
    input_tokens = max(
        0, int(_legacy_usage_value(values, "input", "input_tokens", "inputTokens") or 0)
    )
    cached_input_tokens = max(
        0,
        int(
            _legacy_usage_value(
                values, "cached", "cached_tokens", "cached_input_tokens", "cachedInputTokens"
            )
            or 0
        ),
    )
    cached_input_tokens = min(cached_input_tokens, input_tokens)
    output_tokens = max(
        0, int(_legacy_usage_value(values, "output", "output_tokens", "outputTokens") or 0)
    )
    context_tokens = max(
        0, int(_legacy_usage_value(values, "context", "context_tokens", "contextTokens") or 0)
    )
    raw_window = _legacy_usage_value(values, "model_context_window", "modelContextWindow")
    model_context_window = (
        max(0, int(raw_window)) if raw_window not in {None, 0} else None
    )
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "uncached_input_tokens": input_tokens - cached_input_tokens,
        "output_tokens": output_tokens,
        "context_tokens": context_tokens,
        "model_context_window": model_context_window,
    }


# Every alias the legacy pool accepted, exercised one at a time.
ALIAS_CASES: list[dict[str, Any]] = [
    {},
    {"input": 100, "cached_tokens": 60, "output": 10, "context": 40},
    {"input": 100, "cached": 60},
    {"input": 100, "cached_input_tokens": 60},
    {"input": 100, "cachedInputTokens": 60},
    {"input_tokens": 100, "cached_tokens": 60},
    {"inputTokens": 100, "cachedInputTokens": 60},
    {"input": 100, "cached_tokens": 200},  # cached clamped to input
    {"input": 0, "cached_tokens": 50},
    {"output": 7, "output_tokens": 9},  # first alias wins
    {"outputTokens": 7},
    {"context": 40},
    {"context_tokens": 40},
    {"contextTokens": 40},
    {"model_context_window": 128000},
    {"modelContextWindow": 128000},
    {"model_context_window": 0},  # explicit zero window -> unknown
    {"modelContextWindow": 0},
    {"input": 100, "cached_tokens": 60, "model_context_window": 0},
    {"input": -5, "cached_tokens": -3, "output": -1},  # negatives clamp to 0
    {"input": "100", "cached_tokens": "60"},  # numeric strings
    {"input": None, "cached_tokens": None},
    {"input": 100, "uncached_input_tokens": 30},  # legacy ignores uncached alias
    {"input": 100, "last_context_tokens": 40},  # legacy ignores this alias
]


class AgentUsageCompatTests(unittest.TestCase):
    def test_all_legacy_aliases_match_reference(self) -> None:
        for case in ALIAS_CASES:
            with self.subTest(case=case):
                self.assertEqual(
                    AgentPool._normalize_usage(case),
                    legacy_normalize_usage(case),
                )

    def test_cached_tokens_alias_specifically(self) -> None:
        result = AgentPool._normalize_usage(
            {"input": 100, "cached_tokens": 60, "output": 10, "context": 40}
        )
        self.assertEqual(result["cached_input_tokens"], 60)
        self.assertEqual(result["uncached_input_tokens"], 40)

    def test_nested_usage_is_not_introduced(self) -> None:
        # Legacy reads flat keys only; nested total/last must be ignored.
        nested = {"total": {"input": 100, "cached": 60}, "last": {"input": 50}}
        result = AgentPool._normalize_usage(nested)
        self.assertEqual(result, legacy_normalize_usage(nested))
        self.assertEqual(result["input_tokens"], 0)
        self.assertEqual(result["cached_input_tokens"], 0)

    def test_input_back_derivation_is_not_introduced(self) -> None:
        # Legacy does not derive input from cached + uncached.
        case = {"cached": 60, "uncached": 40}
        result = AgentPool._normalize_usage(case)
        self.assertEqual(result, legacy_normalize_usage(case))
        self.assertEqual(result["input_tokens"], 0)

    def test_none_input(self) -> None:
        self.assertEqual(
            AgentPool._normalize_usage(None), legacy_normalize_usage(None)
        )


if __name__ == "__main__":
    unittest.main()