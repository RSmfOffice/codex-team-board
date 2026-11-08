from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings

AGENT_CODEX_FILES = ("config.toml", "auth.json", "models.json")


@dataclass(frozen=True, slots=True)
class AgentCodexHome:
    source: Path
    destination: Path
    copied: tuple[str, ...]
    unchanged: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": str(self.source),
            "destination": str(self.destination),
            "copied": list(self.copied),
            "unchanged": list(self.unchanged),
        }


def user_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME", "").strip()
    return (
        Path(configured).expanduser().resolve()
        if configured
        else (Path.home() / ".codex").resolve()
    )


def service_codex_home(settings: Settings) -> Path:
    configured = os.environ.get(
        "CODEX_TEAM_AGENT_CODEX_HOME",
        "",
    ).strip()
    return (
        Path(configured).expanduser().resolve()
        if configured
        else (settings.home / "agent-codex-home").resolve()
    )


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def sync_agent_codex_home(
    settings: Settings,
    *,
    source: Path | None = None,
) -> AgentCodexHome:
    source_home = (source or user_codex_home()).expanduser().resolve()
    destination = service_codex_home(settings)
    destination.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    unchanged: list[str] = []
    for filename in AGENT_CODEX_FILES:
        source_file = source_home / filename
        if not source_file.is_file():
            continue
        destination_file = destination / filename
        if (
            destination_file.is_file()
            and destination_file.stat().st_size == source_file.stat().st_size
            and _digest(destination_file) == _digest(source_file)
        ):
            unchanged.append(filename)
            continue
        shutil.copy2(source_file, destination_file)
        copied.append(filename)
    return AgentCodexHome(
        source=source_home,
        destination=destination,
        copied=tuple(copied),
        unchanged=tuple(unchanged),
    )
