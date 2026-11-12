from __future__ import annotations

import os
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator


class RepositoryLockTimeout(TimeoutError):
    """Raised when a repository-scoped operation cannot acquire its lock."""


_PROCESS_LOCKS: dict[str, threading.Lock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


def _process_lock(key: str) -> threading.Lock:
    with _PROCESS_LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.Lock())


def _git_common_dir(root: Path) -> Path:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"could not locate Git metadata for {root}: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip() or f"could not locate Git metadata for {root}"
        )
    common = Path(result.stdout.strip())
    if not common.is_absolute():
        common = root / common
    common = common.resolve()
    common.mkdir(parents=True, exist_ok=True)
    return common


def _try_os_lock(handle: BinaryIO) -> bool:
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock_os(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def repository_lock(
    root: str | Path,
    *,
    label: str,
    timeout: float = 30.0,
) -> Iterator[None]:
    """Serialize Git topology changes for one repository across threads/processes."""

    repository = Path(root).resolve()
    lock_path = _git_common_dir(repository) / "codex-team-repository.lock"
    key = str(lock_path).casefold() if os.name == "nt" else str(lock_path)
    local_lock = _process_lock(key)
    deadline = time.monotonic() + max(0.0, timeout)
    if not local_lock.acquire(timeout=max(0.0, timeout)):
        raise RepositoryLockTimeout(
            f"timed out waiting for repository lock during {label}"
        )

    handle: BinaryIO | None = None
    locked = False
    try:
        handle = open(lock_path, "a+b", buffering=0)
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"\0")
        while True:
            if _try_os_lock(handle):
                locked = True
                break
            if time.monotonic() >= deadline:
                raise RepositoryLockTimeout(
                    f"timed out waiting for repository lock during {label}"
                )
            time.sleep(0.05)
        yield
    finally:
        if locked and handle is not None:
            _unlock_os(handle)
        if handle is not None:
            handle.close()
        local_lock.release()
