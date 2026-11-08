from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import Settings
from .db import connect
from .usage import usage_from_exec_log, usage_is_cost_complete


def backfill_exec_usage(
    settings: Settings,
    *,
    dry_run: bool = True,
    project_id: str | None = None,
    goal_id: str | None = None,
    job_id: str | None = None,
) -> dict[str, Any]:
    where = ["j.runtime = 'exec'"]
    parameters: list[Any] = []
    if project_id:
        where.append("g.project_id = ?")
        parameters.append(project_id)
    if goal_id:
        where.append("j.goal_id = ?")
        parameters.append(goal_id)
    if job_id:
        where.append("j.job_id = ?")
        parameters.append(job_id)

    updated: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    connection = connect(settings.db_path)
    try:
        rows = connection.execute(
            f"""
            SELECT j.*, g.project_id
            FROM cluster_jobs j
            JOIN cluster_goals g ON g.goal_id = j.goal_id
            WHERE {" AND ".join(where)}
            ORDER BY j.started_at, j.job_id
            """,
            parameters,
        ).fetchall()
        for row in rows:
            job = dict(row)
            current = {
                "input_tokens": job.get("input_tokens"),
                "cached_input_tokens": job.get("cached_input_tokens"),
                "uncached_input_tokens": job.get("uncached_input_tokens"),
                "output_tokens": job.get("output_tokens"),
            }
            if any(int(value or 0) > 0 for value in current.values()):
                skipped.append(
                    {
                        "job_id": job["job_id"],
                        "reason": "existing_nonzero_usage",
                    }
                )
                continue
            log_path = Path(str(job.get("log_path") or ""))
            if not log_path.is_file():
                failed.append(
                    {
                        "job_id": job["job_id"],
                        "reason": "log_missing",
                        "log_path": str(log_path),
                    }
                )
                continue
            parsed = usage_from_exec_log(log_path)
            if not usage_is_cost_complete(parsed):
                failed.append(
                    {
                        "job_id": job["job_id"],
                        "reason": "usage_missing",
                        "log_path": str(log_path),
                    }
                )
                continue
            first_usage = parsed.get("first_usage")
            first = first_usage if isinstance(first_usage, dict) else parsed
            record = {
                "job_id": job["job_id"],
                "project_id": job["project_id"],
                "goal_id": job["goal_id"],
                "input_tokens": parsed["input_tokens"],
                "cached_input_tokens": parsed["cached_input_tokens"],
                "uncached_input_tokens": parsed["uncached_input_tokens"],
                "output_tokens": parsed["output_tokens"],
                "first_input_tokens": first.get("input_tokens"),
                "first_cached_input_tokens": first.get("cached_input_tokens"),
                "first_uncached_input_tokens": first.get(
                    "uncached_input_tokens"
                ),
                "cache_write_input_tokens": parsed.get(
                    "cache_write_input_tokens"
                ),
                "reasoning_output_tokens": parsed.get(
                    "reasoning_output_tokens"
                ),
                "context_tokens": parsed.get("context_tokens"),
                "model_context_window": parsed.get("model_context_window"),
                "usage_source": parsed.get("usage_source"),
            }
            updated.append(record)
            if dry_run:
                continue
            connection.execute(
                """
                UPDATE cluster_jobs
                SET input_tokens = ?, cached_input_tokens = ?,
                    uncached_input_tokens = ?, output_tokens = ?,
                    first_input_tokens = ?, first_cached_input_tokens = ?,
                    first_uncached_input_tokens = ?,
                    cache_write_input_tokens = ?, reasoning_output_tokens = ?,
                    context_tokens = COALESCE(?, 0),
                    model_context_window = ?, usage_source = ?,
                    telemetry_complete = 1
                WHERE job_id = ?
                """,
                (
                    record["input_tokens"],
                    record["cached_input_tokens"],
                    record["uncached_input_tokens"],
                    record["output_tokens"],
                    record["first_input_tokens"] or 0,
                    record["first_cached_input_tokens"] or 0,
                    record["first_uncached_input_tokens"] or 0,
                    record["cache_write_input_tokens"],
                    record["reasoning_output_tokens"],
                    record["context_tokens"],
                    record["model_context_window"],
                    record["usage_source"],
                    record["job_id"],
                ),
            )
        if not dry_run:
            connection.commit()
    finally:
        connection.close()

    return {
        "dry_run": dry_run,
        "filters": {
            "project_id": project_id,
            "goal_id": goal_id,
            "job_id": job_id,
        },
        "updated_count": 0 if dry_run else len(updated),
        "would_update_count": len(updated) if dry_run else 0,
        "updated": updated,
        "skipped": skipped,
        "failed": failed,
    }
