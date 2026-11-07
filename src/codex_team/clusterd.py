from __future__ import annotations

import argparse
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .cluster import ClusterManager, daemon_paths
from .config import Settings
from .db import utc_now
from .processes import pid_is_running


def write_json_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


@contextmanager
def acquire_lock(settings: Settings) -> Iterator[None]:
    lock_path, _, _ = daemon_paths(settings)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor: int | None = None
    for _ in range(2):
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                pid = int(json.loads(lock_path.read_text(encoding="utf-8")).get("pid", 0))
            except (OSError, ValueError, TypeError):
                pid = 0
            if pid_is_running(pid):
                raise RuntimeError(f"cluster daemon already running (pid {pid})")
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
    if descriptor is None:
        raise RuntimeError("could not acquire cluster daemon lock")
    os.write(
        descriptor,
        json.dumps({"pid": os.getpid(), "started_at": utc_now()}).encode("utf-8"),
    )
    os.close(descriptor)
    try:
        yield
    finally:
        try:
            data = json.loads(lock_path.read_text(encoding="utf-8"))
            if int(data.get("pid", -1)) == os.getpid():
                lock_path.unlink()
        except (FileNotFoundError, OSError, ValueError, TypeError):
            pass


def run(interval: float = 5.0, once: bool = False) -> int:
    settings = Settings.load()
    settings.ensure()
    _, state_path, _ = daemon_paths(settings)
    manager = ClusterManager(settings)
    try:
        with acquire_lock(settings):
            while True:
                try:
                    result = manager.tick()
                    state = {
                        "ok": True,
                        **result,
                        "pid": os.getpid(),
                    }
                except Exception as exc:
                    state = {
                        "ok": False,
                        "tick_at": utc_now(),
                        "pid": os.getpid(),
                        "error": str(exc),
                    }
                write_json_atomic(state_path, state)
                if once:
                    return 0 if state.get("ok") else 1
                time.sleep(max(1.0, interval))
    finally:
        manager.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="codex-team-clusterd")
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    return run(interval=args.interval, once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
