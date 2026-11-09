from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from codex_team.config import Settings
from codex_team.service import TeamService


def emit(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False), flush=True)


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def main() -> int:
    # Consume the prompt exactly as the real Codex CLI does.
    sys.stdin.read()
    team = TeamService(Settings.load())
    role = os.environ.get("CODEX_TEAM_ROLE", "")
    goal_id = os.environ["CODEX_TEAM_GOAL_ID"]
    job_id = os.environ["CODEX_TEAM_JOB_ID"]
    repo = Path(os.environ["CODEX_TEAM_REPO_ROOT"])
    resumed = "resume" in sys.argv
    thread_id = f"fake-{job_id}"
    emit({"type": "thread.started", "thread_id": thread_id})
    marker_dir = team.settings.home / "fake-agent"
    marker_dir.mkdir(parents=True, exist_ok=True)

    if role == "lead":
        fail_marker = marker_dir / "lead-failed-once"
        if not fail_marker.exists():
            fail_marker.write_text("1", encoding="utf-8")
            emit({"type": "turn.failed", "error": {"message": "simulated lead interruption"}})
            return 7
        tasks = team.list_tasks(repo, goal_id=goal_id, include_done=True)
        if not tasks:
            team.create_task(
                repo,
                title="Create automatic greeting",
                description="Create greeting.txt containing hello.",
                acceptance="greeting.txt contains hello and is committed",
                kind="write",
                owner_role="team_worker",
                actor="lead",
                goal_id=goal_id,
            )
        else:
            review = next((item for item in tasks if item["status"] == "review"), None)
            if review:
                if review["kind"] == "write":
                    git(
                        repo,
                        "merge",
                        "--no-ff",
                        review["branch"],
                        "-m",
                        f"merge {review['task_id']}",
                    )
                    merge_commit = git(repo, "rev-parse", "HEAD")
                else:
                    merge_commit = ""
                team.review_task(
                    review["task_id"],
                    actor="lead",
                    approve=True,
                    notes="automatic cluster review passed",
                    merge_commit=merge_commit,
                )
        emit({"type": "turn.completed", "resumed": resumed})
        return 0

    task_id = os.environ["CODEX_TEAM_TASK_ID"]
    task = team.get_task(task_id)
    actor = os.environ.get("CODEX_TEAM_ACTOR") or f"fake-worker-{job_id}"
    claim = team.claim_task(task_id, actor=actor)
    fail_marker = marker_dir / f"worker-failed-once-{task_id}"
    if not fail_marker.exists():
        fail_marker.write_text("1", encoding="utf-8")
        emit({"type": "turn.failed", "error": {"message": "simulated worker interruption"}})
        return 8
    worktree = Path(task["worktree_path"])
    (worktree / "greeting.txt").write_text("hello\n", encoding="utf-8")
    git(worktree, "add", "greeting.txt")
    git(worktree, "commit", "-m", "create greeting")
    commit = git(worktree, "rev-parse", "HEAD")
    team.submit_task(
        task_id,
        actor=actor,
        claim_token=claim["claim_token"],
        result=(
            "行为：创建 greeting.txt\n"
            "变更文件：greeting.txt\n"
            "验证：内容检查通过\n"
            "风险：无\n"
            "Review 重点：确认文件内容\n"
            "待决问题：无"
        ),
        tests="manual content check: passed",
        commit_sha=commit,
    )
    emit({"type": "turn.completed", "resumed": resumed})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
