"""Differential tests: the single-pass exec log scan must produce exactly the
same result as an independent two-pass reference (usage pass + lifecycle pass).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from codex_team.cluster import _parse_codex_log
from codex_team.usage import (
    RAW_USAGE_FIELDS,
    merge_usage,
    normalize_usage,
    usage_field_is_present,
    usage_has_cost_evidence,
)


def _reference_two_pass(path: Path) -> dict[str, Any]:
    """Independent re-implementation of the original two-pass parsing."""
    # Pass 1: usage (mirrors the old usage_from_exec_log).
    final_usage = normalize_usage(None)
    first_usage = None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                if not line.startswith("{"):
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") == "turn.completed":
                    snapshot = normalize_usage(event.get("usage") or {})
                    final_usage = merge_usage(snapshot, final_usage)
                    if first_usage is None:
                        first_usage = snapshot
    except OSError:
        return {
            **normalize_usage(None),
            "first_usage": normalize_usage(None),
            "usage_source": None,
        }
    usage_record = {
        **final_usage,
        "first_usage": first_usage or normalize_usage(None),
        "usage_source": (
            "turn.completed.usage" if usage_has_cost_evidence(final_usage) else None
        ),
    }
    # Pass 2: lifecycle + presence (mirrors the old _parse_codex_log scan).
    usage = {field: usage_record.get(field) for field in RAW_USAGE_FIELDS}
    first = usage_record["first_usage"]
    result: dict[str, Any] = {
        "thread_id": None,
        "completed": False,
        "error": "",
        **usage,
        "first_input_tokens": first.get("input_tokens"),
        "first_cached_input_tokens": first.get("cached_input_tokens"),
        "first_uncached_input_tokens": first.get("uncached_input_tokens"),
        "usage_source": usage_record["usage_source"],
    }
    present: set[str] = set()
    first_present: set[str] | None = None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                if not line.startswith("{"):
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                event_type = item.get("type")
                if event_type == "thread.started":
                    result["thread_id"] = item.get("thread_id")
                elif event_type == "turn.completed":
                    result["completed"] = True
                    event_usage = item.get("usage")
                    if isinstance(event_usage, dict):
                        fields = {
                            f
                            for f in RAW_USAGE_FIELDS
                            if usage_field_is_present(event_usage, f)
                        }
                        present.update(fields)
                        if first_present is None:
                            first_present = set(fields)
                elif event_type in {"turn.failed", "error"}:
                    error = item.get("error") or item.get("message")
                    if isinstance(error, dict):
                        error = error.get("message") or json.dumps(error, ensure_ascii=False)
                    result["error"] = str(error or event_type)
    except OSError:
        result["error"] = "log unreadable"
    if "uncached_input_tokens" in present or {"input_tokens", "cached_input_tokens"}.issubset(present):
        present.add("uncached_input_tokens")
    output_present = set(present)
    first_present = first_present or set()
    if "uncached_input_tokens" in first_present or {"input_tokens", "cached_input_tokens"}.issubset(first_present):
        first_present.add("uncached_input_tokens")
    for final_field, snap_field in (
        ("first_input_tokens", "input_tokens"),
        ("first_cached_input_tokens", "cached_input_tokens"),
        ("first_uncached_input_tokens", "uncached_input_tokens"),
    ):
        if snap_field in first_present:
            output_present.add(final_field)
    result["usage_present_fields"] = tuple(sorted(output_present))
    result["telemetry_complete"] = all(
        f in output_present and result.get(f) is not None
        for f in ("input_tokens", "cached_input_tokens", "uncached_input_tokens", "output_tokens")
    )
    return result


SAMPLES: list[list[dict[str, Any]]] = [
    [],
    [{"type": "thread.started", "thread_id": "th-1"}],
    [
        {"type": "thread.started", "thread_id": "th-1"},
        {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 10}},
    ],
    [
        {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 10}},
        {"type": "turn.completed", "usage": {"input_tokens": 200, "cached_input_tokens": 150, "output_tokens": 20}},
    ],
    [
        {"type": "turn.completed", "usage": {"cached_input_tokens": 60, "output_tokens": 10}},
    ],
    [
        {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 10}},
    ],
    [
        {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 10}},
        {"type": "turn.failed", "error": {"message": "boom"}},
    ],
    [
        {"type": "error", "message": "context window exceeded"},
    ],
    [
        {"type": "turn.completed", "usage": {"input": 100, "cached": 60, "output": 10, "context": 40, "model_context_window": 128000}},
    ],
]


class ExecLogDifferentialTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write(self, events: list[dict[str, Any]]) -> Path:
        path = self.base / "log.jsonl"
        path.write_text(
            "\n".join(json.dumps(e) for e in events) + ("\n" if events else ""),
            encoding="utf-8",
        )
        return path

    def test_single_pass_matches_two_pass_reference(self) -> None:
        for index, events in enumerate(SAMPLES):
            with self.subTest(sample=index):
                path = self._write(events)
                actual = _parse_codex_log(path)
                expected = _reference_two_pass(path)
                for key in expected:
                    self.assertEqual(actual.get(key), expected[key], f"sample {index} key {key}")

    def test_unreadable_log(self) -> None:
        missing = self.base / "does-not-exist.jsonl"
        actual = _parse_codex_log(missing)
        self.assertEqual(actual["error"], "log unreadable")
        self.assertFalse(actual["completed"])


if __name__ == "__main__":
    unittest.main()