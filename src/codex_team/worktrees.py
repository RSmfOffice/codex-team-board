from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from .db import utc_now
from .service import TeamError, TeamService


class WorktreeManager:
    def __init__(self, team: TeamService) -> None:
        self.team = team

    @staticmethod
    def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    def create(
        self,
        task_id: str,
        *,
        repo: str | Path,
        actor: str = "lead",
        base_ref: str = "HEAD",
        worktree_path: str | Path | None = None,
    ) -> dict[str, Any]:
        task = self.team.get_task(task_id)
        if task["kind"] != "write":
            raise TeamError("only write tasks can own a worktree")
        self.team._ensure_project_active(
            self.team.get_project(task["project_id"])
        )
        if task.get("archived_at"):
            raise TeamError("task is archived; restore it before creating a worktree")
        if task["worktree_path"]:
            raise TeamError(f"task already has worktree: {task['worktree_path']}")
        root = self.team._root(repo)
        if Path(task["root_path"]).resolve() != root:
            raise TeamError("task belongs to a different repository")
        target_branch_result = self._git(root, "symbolic-ref", "--short", "HEAD")
        if target_branch_result.returncode != 0 or not target_branch_result.stdout.strip():
            raise TeamError("main checkout must be on a named branch when creating a worktree")
        target_branch = target_branch_result.stdout.strip()
        dirty = self._git(root, "status", "--porcelain")
        if dirty.returncode != 0:
            raise TeamError(dirty.stderr.strip() or "could not inspect main checkout")
        if dirty.stdout.strip():
            raise TeamError(
                "main checkout has uncommitted changes; commit or stash them before "
                "creating an isolated worktree"
            )
        head = self._git(root, "rev-parse", base_ref)
        if head.returncode != 0:
            raise TeamError(f"invalid base ref: {base_ref}")
        base_commit = head.stdout.strip()
        slug = re.sub(r"[^a-z0-9]+", "-", task["title"].lower()).strip("-")[:36] or "task"
        branch = f"codex/team/{task_id.lower()}-{slug}"
        if self._git(root, "show-ref", "--verify", f"refs/heads/{branch}").returncode == 0:
            raise TeamError(f"branch already exists: {branch}")
        target = (
            Path(worktree_path).expanduser().resolve()
            if worktree_path
            else root.parent / ".codex-team-worktrees" / f"{root.name}-{task_id.lower()}"
        )
        if target.exists():
            raise TeamError(f"worktree target already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        add = self._git(root, "worktree", "add", "-b", branch, str(target), base_commit)
        if add.returncode != 0:
            raise TeamError(add.stderr.strip() or add.stdout.strip() or "git worktree add failed")
        with self.team.connection() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET worktree_path = ?, branch = ?, target_branch = ?,
                    base_ref = ?, base_commit = ?,
                    updated_at = ?, version = version + 1
                WHERE task_id = ?
                """,
                (
                    str(target),
                    branch,
                    target_branch,
                    base_ref,
                    base_commit,
                    utc_now(),
                    task_id,
                ),
            )
            self.team._event(
                connection,
                task_id,
                "worktree_created",
                actor,
                {
                    "worktree_path": str(target),
                    "branch": branch,
                    "target_branch": target_branch,
                    "base_ref": base_ref,
                    "base_commit": base_commit,
                },
            )
        result = self.team.get_task(task_id)
        return result

    def list(self, repo: str | Path) -> list[dict[str, Any]]:
        root = self.team._root(repo)
        result = self._git(root, "worktree", "list", "--porcelain")
        if result.returncode != 0:
            raise TeamError(result.stderr.strip() or "git worktree list failed")
        entries: list[dict[str, Any]] = []
        current: dict[str, Any] = {}
        for line in result.stdout.splitlines():
            if not line.strip():
                if current:
                    entries.append(current)
                    current = {}
                continue
            key, _, value = line.partition(" ")
            current[key] = value or True
        if current:
            entries.append(current)
        return entries
