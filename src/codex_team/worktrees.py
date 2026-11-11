from __future__ import annotations

import json
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any, Mapping

from .db import utc_now
from .repo_lock import RepositoryLockTimeout, repository_lock
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
        if task["status"] not in {"ready", "backlog"}:
            raise TeamError("write task must be ready or backlog before creating a worktree")
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
        try:
            with repository_lock(
                root,
                label=f"worktree creation for {task_id}",
            ):
                return self._create_locked(
                    task,
                    task_id=task_id,
                    root=root,
                    actor=actor,
                    base_ref=base_ref,
                    worktree_path=worktree_path,
                )
        except RepositoryLockTimeout as exc:
            raise TeamError(str(exc)) from exc

    def _create_locked(
        self,
        task: dict[str, Any],
        *,
        task_id: str,
        root: Path,
        actor: str,
        base_ref: str,
        worktree_path: str | Path | None,
    ) -> dict[str, Any]:
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
        operation_id = self._reserve_intent(
            task_id=task_id,
            goal_id=task.get("goal_id"),
            repo_root=root,
            worktree_path=target,
            branch=branch,
            target_branch=target_branch,
            base_ref=base_ref,
            base_commit=base_commit,
            actor=actor,
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        add = self._git(root, "worktree", "add", "-b", branch, str(target), base_commit)
        if add.returncode != 0:
            error = add.stderr.strip() or add.stdout.strip() or "git worktree add failed"
            self._fail_intent(operation_id, error)
            raise TeamError(error)
        try:
            result = self._finalize_intent(operation_id)
            result["worktree_operation_id"] = operation_id
            return result
        except Exception as exc:
            raise TeamError(
                "worktree was created but its database intent could not be finalized; "
                f"recovery is required ({operation_id}): {exc}"
            ) from exc

    @staticmethod
    def _operation_id() -> str:
        return f"OP-{uuid.uuid4().hex[:16].upper()}"

    @staticmethod
    def _json(value: Mapping[str, Any]) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def _reserve_intent(
        self,
        *,
        task_id: str,
        goal_id: str | None,
        repo_root: Path,
        worktree_path: Path,
        branch: str,
        target_branch: str,
        base_ref: str,
        base_commit: str,
        actor: str,
    ) -> str:
        operation_id = self._operation_id()
        now = utc_now()
        payload = {
            "repo_root": str(repo_root),
            "worktree_path": str(worktree_path),
            "branch": branch,
            "target_branch": target_branch,
            "base_ref": base_ref,
            "base_commit": base_commit,
        }
        with self.team.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = connection.execute(
                """
                SELECT t.status, t.kind, t.worktree_path, t.archived_at,
                       p.archived_at AS project_archived_at
                FROM tasks t JOIN projects p ON p.project_id = t.project_id
                WHERE t.task_id = ?
                """,
                (task_id,),
            ).fetchone()
            if task is None:
                raise TeamError(f"task not found: {task_id}")
            if task["kind"] != "write":
                raise TeamError("only write tasks can own a worktree")
            if task["status"] not in {"ready", "backlog"}:
                raise TeamError(
                    "write task must be ready or backlog before creating a worktree"
                )
            if task["archived_at"] or task["project_archived_at"]:
                raise TeamError("archived task or project cannot create a worktree")
            if task["worktree_path"]:
                raise TeamError(f"task already has worktree: {task['worktree_path']}")
            active = connection.execute(
                """
                SELECT operation_id
                FROM external_operations
                WHERE operation_type = 'worktree_create'
                  AND task_id = ?
                  AND status IN ('pending', 'running')
                LIMIT 1
                """,
                (task_id,),
            ).fetchone()
            if active is not None:
                raise TeamError(
                    f"worktree creation already in progress: {active['operation_id']}"
                )
            connection.execute(
                """
                INSERT INTO external_operations(
                    operation_id, operation_type, status, goal_id, task_id,
                    actor, payload_json, created_at, updated_at
                ) VALUES (
                    ?, 'worktree_create', 'running', ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    operation_id,
                    goal_id,
                    task_id,
                    actor,
                    self._json(payload),
                    now,
                    now,
                ),
            )
        return operation_id

    def _finalize_intent(self, operation_id: str) -> dict[str, Any]:
        now = utc_now()
        with self.team.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            operation = connection.execute(
                """
                SELECT * FROM external_operations
                WHERE operation_id = ? AND operation_type = 'worktree_create'
                """,
                (operation_id,),
            ).fetchone()
            if operation is None:
                raise TeamError(f"worktree operation not found: {operation_id}")
            if operation["status"] == "failed":
                raise TeamError(
                    f"worktree operation failed: {operation['error']}"
                )
            payload = json.loads(operation["payload_json"])
            task_id = str(operation["task_id"])
            task = connection.execute(
                """
                SELECT t.status, t.worktree_path, t.archived_at,
                       p.archived_at AS project_archived_at
                FROM tasks t JOIN projects p ON p.project_id = t.project_id
                WHERE t.task_id = ?
                """,
                (task_id,),
            ).fetchone()
            if task is None:
                raise TeamError(f"task not found: {task_id}")
            if task["archived_at"] or task["project_archived_at"]:
                raise TeamError("archived task or project cannot finalize a worktree")
            if task["status"] not in {"ready", "backlog"}:
                raise TeamError(
                    f"task status changed before worktree finalization: {task['status']}"
                )
            worktree_path = str(payload["worktree_path"])
            if task["worktree_path"]:
                if str(task["worktree_path"]) != worktree_path:
                    raise TeamError("task already points to a different worktree")
                if operation["status"] != "completed":
                    connection.execute(
                        """
                        UPDATE external_operations
                        SET status = 'completed', completed_at = ?, updated_at = ?,
                            result_json = ?
                        WHERE operation_id = ?
                        """,
                        (
                            now,
                            now,
                            self._json({"worktree_path": worktree_path}),
                            operation_id,
                        ),
                    )
            else:
                updated = connection.execute(
                    """
                    UPDATE tasks
                    SET worktree_path = ?, branch = ?, target_branch = ?,
                        base_ref = ?, base_commit = ?,
                        updated_at = ?, version = version + 1
                    WHERE task_id = ? AND worktree_path IS NULL
                    """,
                    (
                        worktree_path,
                        str(payload["branch"]),
                        str(payload["target_branch"]),
                        str(payload["base_ref"]),
                        str(payload["base_commit"]),
                        now,
                        task_id,
                    ),
                )
                if updated.rowcount != 1:
                    raise TeamError("task changed while finalizing worktree intent")
                self.team._event(
                    connection,
                    task_id,
                    "worktree_created",
                    str(operation["actor"]),
                    {
                        **payload,
                        "operation_id": operation_id,
                    },
                )
                connection.execute(
                    """
                    UPDATE external_operations
                    SET status = 'completed', completed_at = ?, updated_at = ?,
                        result_json = ?
                    WHERE operation_id = ?
                    """,
                    (
                        now,
                        now,
                        self._json({"worktree_path": worktree_path}),
                        operation_id,
                    ),
                )
        return self.team.get_task(task_id)

    def _fail_intent(self, operation_id: str, error: str) -> None:
        now = utc_now()
        with self.team.connection() as connection:
            connection.execute(
                """
                UPDATE external_operations
                SET status = 'failed', error = ?, completed_at = ?, updated_at = ?
                WHERE operation_id = ? AND status IN ('pending', 'running')
                """,
                (error, now, now, operation_id),
            )

    def recover_pending(self) -> list[dict[str, Any]]:
        with self.team.connection(write=False) as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM external_operations
                    WHERE operation_type = 'worktree_create'
                      AND status IN ('pending', 'running')
                    ORDER BY created_at, operation_id
                    """
                ).fetchall()
            ]
        recovered: list[dict[str, Any]] = []
        for operation in rows:
            operation_id = str(operation["operation_id"])
            try:
                recovered.append(self._recover_operation(operation))
            except RepositoryLockTimeout as exc:
                recovered.append(
                    {
                        "operation_id": operation_id,
                        "status": "deferred",
                        "error": str(exc),
                    }
                )
            except Exception as exc:
                error = str(exc)
                try:
                    self._fail_intent(operation_id, error)
                except Exception as fail_error:
                    error = f"{error}; failed to record operation failure: {fail_error}"
                recovered.append(
                    {
                        "operation_id": operation_id,
                        "status": "failed",
                        "error": error,
                    }
                )
        return recovered

    def _recover_operation(self, operation: dict[str, Any]) -> dict[str, Any]:
        operation_id = str(operation["operation_id"])
        payload = json.loads(operation["payload_json"])
        root = Path(str(payload["repo_root"])).resolve()
        target = Path(str(payload["worktree_path"])).resolve()
        branch = str(payload["branch"])
        if not root.is_dir():
            raise TeamError(f"repository root is missing: {root}")
        with repository_lock(
            root,
            label=f"worktree recovery for {operation_id}",
        ):
            if target.is_dir():
                top = self._git(target, "rev-parse", "--show-toplevel")
                current_branch = self._git(
                    target,
                    "symbolic-ref",
                    "--short",
                    "HEAD",
                )
                if (
                    top.returncode != 0
                    or Path(top.stdout.strip()).resolve() != target
                    or current_branch.returncode != 0
                    or current_branch.stdout.strip() != branch
                ):
                    raise TeamError(
                        "worktree target exists but branch/root do not match intent"
                    )
                self._finalize_intent(operation_id)
            else:
                branch_exists = self._git(
                    root,
                    "show-ref",
                    "--verify",
                    f"refs/heads/{branch}",
                )
                if branch_exists.returncode == 0:
                    raise TeamError(
                        "branch exists but worktree target is missing"
                    )
                raise TeamError("worktree creation did not complete")
        return {"operation_id": operation_id, "status": "completed"}

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
