from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    project_root: Path
    home: Path
    db_path: Path
    logs_dir: Path

    @classmethod
    def load(cls) -> "Settings":
        project_root = Path(__file__).resolve().parents[2]
        default_home = Path(os.environ.get("LOCALAPPDATA", project_root)) / "CodexTeam"
        home = Path(os.environ.get("CODEX_TEAM_HOME", default_home)).resolve()
        return cls(
            project_root=project_root,
            home=home,
            db_path=home / "team.sqlite",
            logs_dir=home / "logs",
        )

    def ensure(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
