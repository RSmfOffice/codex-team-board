from __future__ import annotations

import argparse
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .cluster import (
    ClusterManager,
    daemon_identity,
    daemon_lock_state,
    daemon_paths,
)
from .config import Settings
from .db import utc_now
from .processes import (
    current_process_identity,
    identity_record,
    process_identity_matches,
)
from .redaction import redact_sensitive_text


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
    owner_identity = current_process_identity()
    if not owner_identity:
        raise RuntimeError("could not verify cluster daemon process identity")
    for _ in range(2):
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            lock = daemon_lock_state(settings)
            if not lock["exists"]:
                continue
            if lock["process_identity_verified"]:
                pid = lock["pid"]
                raise RuntimeError(f"cluster daemon already running (pid {pid})")
            if lock["unverified_live_process"]:
                raise RuntimeError(
                    "cluster daemon lock contains an unverified live process "
                    f"(pid {lock['pid']}); stop the legacy daemon before "
                    "upgrading"
                )
            if not lock["state_valid"]:
                raise RuntimeError(
                    "cluster daemon lock is corrupt and cannot be verified; "
                    f"confirm no daemon is running and remove {lock_path}"
                )
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
    if descriptor is None:
        raise RuntimeError("could not acquire cluster daemon lock")
    try:
        os.write(
            descriptor,
            json.dumps(
                {
                    "pid": os.getpid(),
                    "process_identity": owner_identity,
                    "started_at": utc_now(),
                }
            ).encode("utf-8"),
        )
    except Exception:
        os.close(descriptor)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
        raise
    else:
        os.close(descriptor)
    try:
        yield
    finally:
        try:
            data = json.loads(lock_path.read_text(encoding="utf-8"))
            recorded = identity_record(data.get("process_identity"))
            if process_identity_matches(recorded, owner_identity):
                lock_path.unlink()
        except (FileNotFoundError, OSError, ValueError, TypeError):
            pass


def run(interval: float = 5.0, once: bool = False) -> int:
    settings = Settings.load()
    settings.ensure()
    _, state_path, _ = daemon_paths(settings)
    manager = ClusterManager(settings)
    identity = daemon_identity(settings)
    process_identity = current_process_identity()
    try:
        with acquire_lock(settings):
            while True:
                try:
                    result = manager.tick()
                    state = {
                        "ok": True,
                        **result,
                        "pid": os.getpid(),
                        "process_identity": process_identity,
                        "identity": identity,
                    }
                except Exception as exc:
                    error = redact_sensitive_text(str(exc))
                    state = {
                        "ok": False,
                        "tick_at": utc_now(),
                        "failed_phase": "tick",
                        "failed_phases": ["tick"],
                        "degraded": True,
                        "degraded_phases": ["tick"],
                        "skipped_phases": [],
                        "error_code": "daemon_tick_failure",
                        "error": error,
                        "health": {
                            "ok": False,
                            "degraded": True,
                            "failed_phases": ["tick"],
                            "degraded_phases": ["tick"],
                            "skipped_phases": [],
                            "errors": [
                                {
                                    "phase": "tick",
                                    "source": "clusterd",
                                    "error_code": "daemon_tick_failure",
                                    "redacted_error": error,
                                }
                            ],
                        },
                        "pid": os.getpid(),
                        "process_identity": process_identity,
                        "identity": identity,
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
