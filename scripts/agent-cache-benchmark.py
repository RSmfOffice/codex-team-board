#!/usr/bin/env python3
"""Aggregate fresh/reuse Agent cache runs into JSON and Markdown reports.

The benchmark consumes recorded App Server or cluster job observations. It does
not launch agents itself, so identical tasks can be replayed in both variants
without changing the scheduler or leaking benchmark-only behavior into teamd.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Mapping

METRIC_NAMES = (
    "last_input_tokens",
    "last_cached_input_tokens",
    "last_uncached_input_tokens",
    "last_output_tokens",
    "last_context_tokens",
    "total_input_tokens",
    "total_cached_input_tokens",
    "total_uncached_input_tokens",
    "total_output_tokens",
    "total_context_tokens",
)


@dataclass(frozen=True)
class Observation:
    variant: str
    metrics: dict[str, float]
    first_turn_latency_ms: float | None
    task_success: bool


def load_observations(
    path: Path,
    *,
    default_variant: str | None = None,
) -> list[Observation]:
    text = path.read_text(encoding="utf-8")
    records: list[dict[str, Any]] = []
    stripped = text.strip()
    if not stripped:
        return []
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            item = json.loads(line)
            if not isinstance(item, dict):
                raise ValueError(
                    f"{path}:{line_number}: JSONL record must be an object"
                )
            records.append(item)
    else:
        if isinstance(value, list):
            records = [item for item in value if isinstance(item, dict)]
        elif isinstance(value, dict):
            nested = value.get("runs", value.get("observations", value.get("records")))
            records = (
                [item for item in nested if isinstance(item, dict)]
                if isinstance(nested, list)
                else [value]
            )
        else:
            raise ValueError(f"{path}: expected an object or list")
    return [
        normalize_observation(record, default_variant=default_variant)
        for record in records
    ]


def _number(value: Any) -> float:
    if isinstance(value, bool):
        return float(value)
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _first(mapping: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    return 0


def normalize_observation(
    record: Mapping[str, Any],
    *,
    default_variant: str | None = None,
) -> Observation:
    variant = str(record.get("variant") or record.get("mode") or default_variant or "")
    variant = variant.strip().lower()
    if variant not in {"fresh", "reuse"}:
        raise ValueError(f"record variant must be fresh or reuse, got {variant!r}")

    usage = record.get("usage")
    usage = usage if isinstance(usage, Mapping) else {}
    total = usage.get("total")
    total = total if isinstance(total, Mapping) else usage
    last = usage.get("last")
    last = last if isinstance(last, Mapping) else usage
    metrics = {
        "last_input_tokens": _number(
            _first(last, "input", "inputTokens", "input_tokens")
        ),
        "last_cached_input_tokens": _number(
            _first(last, "cached", "cachedInputTokens", "cached_input_tokens")
        ),
        "last_uncached_input_tokens": _number(
            _first(last, "uncached", "uncachedInputTokens", "uncached_input_tokens")
        ),
        "last_output_tokens": _number(
            _first(last, "output", "outputTokens", "output_tokens")
        ),
        "last_context_tokens": _number(
            _first(last, "context", "contextTokens", "context_tokens")
        ),
        "total_input_tokens": _number(
            _first(total, "input", "inputTokens", "input_tokens")
        ),
        "total_cached_input_tokens": _number(
            _first(total, "cached", "cachedInputTokens", "cached_input_tokens")
        ),
        "total_uncached_input_tokens": _number(
            _first(total, "uncached", "uncachedInputTokens", "uncached_input_tokens")
        ),
        "total_output_tokens": _number(
            _first(total, "output", "outputTokens", "output_tokens")
        ),
        "total_context_tokens": _number(
            _first(total, "context", "contextTokens", "context_tokens")
        ),
    }
    for name in METRIC_NAMES:
        flat = record.get(name)
        if flat is not None:
            metrics[name] = _number(flat)
    for prefix in ("last_", "total_"):
        input_name = f"{prefix}input_tokens"
        cached_name = f"{prefix}cached_input_tokens"
        uncached_name = f"{prefix}uncached_input_tokens"
        if cached_name not in record and uncached_name not in record:
            metrics[uncached_name] = max(
                0.0,
                metrics[input_name] - metrics[cached_name],
            )

    latency_value = record.get("first_turn_latency_ms")
    if latency_value is None:
        seconds = record.get("first_turn_latency_seconds")
        latency = _number(seconds) * 1000 if seconds is not None else None
    else:
        latency = _number(latency_value)
    success_value = record.get("task_success", record.get("success", True))
    if isinstance(success_value, str):
        success = success_value.strip().lower() in {"1", "true", "yes", "passed", "ok"}
    else:
        success = bool(success_value)
    return Observation(
        variant=variant,
        metrics=metrics,
        first_turn_latency_ms=latency,
        task_success=success,
    )


def summarize(
    observations: Iterable[Observation],
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[Observation]] = {"fresh": [], "reuse": []}
    for observation in observations:
        grouped[observation.variant].append(observation)
    result: dict[str, dict[str, Any]] = {}
    for variant, items in grouped.items():
        successful = [item for item in items if item.task_success]
        metrics = {
            name: (
                median([item.metrics[name] for item in successful])
                if successful
                else None
            )
            for name in METRIC_NAMES
        }
        latencies = [
            item.first_turn_latency_ms
            for item in successful
            if item.first_turn_latency_ms is not None
        ]
        total_input = metrics["total_input_tokens"] or 0
        total_cached = metrics["total_cached_input_tokens"] or 0
        result[variant] = {
            **metrics,
            **{
                f"median_{name}": value
                for name, value in metrics.items()
            },
            "median": metrics,
            "metric_medians": metrics,
            "metrics_basis": "successful_tasks",
            "runs": len(items),
            "sample_count": len(items),
            "successful_sample_count": len(successful),
            "first_turn_latency_sample_count": len(latencies),
            "first_turn_latency_ms": median(latencies) if latencies else None,
            "median_first_turn_latency_ms": (
                median(latencies) if latencies else None
            ),
            "task_success_rate": (
                sum(1 for item in items if item.task_success) / len(items)
                if items
                else None
            ),
            "cache_hit_rate": (
                total_cached / total_input if total_input > 0 else None
            ),
        }
    return result


def compare(fresh: Mapping[str, Any], reuse: Mapping[str, Any]) -> dict[str, Any]:
    fresh_uncached = fresh.get("median_last_uncached_input_tokens")
    reuse_uncached = reuse.get("median_last_uncached_input_tokens")
    fresh_total_uncached = fresh.get("median_total_uncached_input_tokens")
    reuse_total_uncached = reuse.get("median_total_uncached_input_tokens")
    fresh_latency = fresh.get("first_turn_latency_ms")
    reuse_latency = reuse.get("first_turn_latency_ms")
    fresh_success = fresh.get("task_success_rate")
    reuse_success = reuse.get("task_success_rate")
    uncached_change = (
        (float(reuse_uncached) - float(fresh_uncached)) / float(fresh_uncached)
        if fresh_uncached not in {None, 0} and reuse_uncached is not None
        else None
    )
    latency_change = (
        (float(reuse_latency) - float(fresh_latency)) / float(fresh_latency)
        if fresh_latency not in {None, 0} and reuse_latency is not None
        else None
    )
    total_uncached_change = (
        (float(reuse_total_uncached) - float(fresh_total_uncached))
        / float(fresh_total_uncached)
        if (
            fresh_total_uncached not in {None, 0}
            and reuse_total_uncached is not None
        )
        else None
    )
    uncached_gate = bool(uncached_change is not None and uncached_change <= -0.10)
    latency_gate = bool(latency_change is not None and latency_change <= -0.20)
    success_gate = bool(
        fresh_success is not None
        and reuse_success is not None
        and float(reuse_success) >= float(fresh_success)
    )
    return {
        "primary_metric": "median last_uncached_input_tokens of successful tasks",
        "uncached_input_change": uncached_change,
        "last_uncached_input_change": uncached_change,
        "total_uncached_input_change_diagnostic": total_uncached_change,
        "first_turn_latency_change": latency_change,
        "success_rate_change": (
            float(reuse_success) - float(fresh_success)
            if fresh_success is not None and reuse_success is not None
            else None
        ),
        "thresholds": {
            "uncached_input_reduction": 0.10,
            "first_turn_latency_reduction": 0.20,
            "success_rate_must_not_decrease": True,
        },
        "uncached_gate": uncached_gate,
        "latency_gate": latency_gate,
        "success_gate": success_gate,
        "meets_gate": bool((uncached_gate or latency_gate) and success_gate),
    }


def _format_number(value: Any, *, percent: bool = False) -> str:
    if value is None:
        return "n/a"
    if percent:
        return f"{float(value) * 100:.1f}%"
    return f"{float(value):,.1f}"


def render_markdown(report: Mapping[str, Any]) -> str:
    fresh = report["variants"]["fresh"]
    reuse = report["variants"]["reuse"]
    comparison = report["comparison"]
    lines = [
        "# Agent Cache A/B Benchmark",
        "",
        f"Generated: {report['generated_at']}",
        "",
        "Token and latency metrics are medians across successful tasks. "
        "`last_*` is the primary single-turn cost; `total_*` is diagnostic "
        "thread history and does not drive the gate.",
        "",
        "| Successful-task metric | Fresh | Reuse | Reuse delta |",
        "| --- | ---: | ---: | ---: |",
    ]
    rows = (
        ("Sample count", "sample_count", False),
        ("Successful samples", "successful_sample_count", False),
        ("Task success rate", "task_success_rate", True),
        ("Median first-turn latency (ms)", "first_turn_latency_ms", False),
        ("Cache hit rate", "cache_hit_rate", True),
        *(
            (
                f"Median {name}" + (
                    " (diagnostic)" if name.startswith("total_") else ""
                ),
                f"median_{name}",
                False,
            )
            for name in METRIC_NAMES
        ),
    )
    for label, key, percent in rows:
        left = fresh.get(key)
        right = reuse.get(key)
        delta = (
            float(right) - float(left)
            if left is not None and right is not None
            else None
        )
        lines.append(
            "| "
            + " | ".join(
                (
                    label,
                    _format_number(left, percent=percent),
                    _format_number(right, percent=percent),
                    _format_number(delta, percent=percent),
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Gate",
            "",
            "Across successful tasks, reuse passes only when task success does "
            "not decrease and either median `last_uncached_input_tokens` falls "
            "by at least 10% or median first-turn latency falls by at least "
            "20%. Median total tokens are retained only as diagnostics.",
            "",
            f"- Primary last uncached input change: {_format_number(comparison['last_uncached_input_change'], percent=True)}",
            f"- Diagnostic total uncached input change: {_format_number(comparison['total_uncached_input_change_diagnostic'], percent=True)}",
            f"- First-turn latency change: {_format_number(comparison['first_turn_latency_change'], percent=True)}",
            f"- Success rate change: {_format_number(comparison['success_rate_change'], percent=True)}",
            f"- Result: {'PASS' if comparison['meets_gate'] else 'FAIL'}",
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare fresh and reused Agent thread cache observations."
    )
    parser.add_argument(
        "--fresh",
        type=Path,
        help="JSON/JSONL file containing fresh-thread observations.",
    )
    parser.add_argument(
        "--reuse",
        type=Path,
        help="JSON/JSONL file containing reused-thread observations.",
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="Combined JSON/JSONL file with a variant/mode field per record.",
    )
    parser.add_argument("--json", type=Path, help="Write the JSON report here.")
    parser.add_argument("--markdown", type=Path, help="Write the Markdown report here.")
    args = parser.parse_args(argv)
    observations: list[Observation] = []
    if args.input:
        observations.extend(load_observations(args.input))
    if args.fresh:
        observations.extend(load_observations(args.fresh, default_variant="fresh"))
    if args.reuse:
        observations.extend(load_observations(args.reuse, default_variant="reuse"))
    if not observations:
        parser.error("provide --input and/or both --fresh and --reuse")
    if not args.input and (not args.fresh or not args.reuse):
        parser.error("--fresh and --reuse must be provided together")

    variants = summarize(observations)
    report = {
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "variants": variants,
        "comparison": compare(variants["fresh"], variants["reuse"]),
    }
    json_text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    markdown_text = render_markdown(report)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json_text, encoding="utf-8")
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(markdown_text, encoding="utf-8")
    if not args.json and not args.markdown:
        print(json_text, end="")
    elif args.json:
        print(args.json)
    if args.markdown:
        print(args.markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
