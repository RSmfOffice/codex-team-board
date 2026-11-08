from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_team.agent_home import service_codex_home, sync_agent_codex_home
from codex_team.cluster import ClusterManager
from codex_team.config import Settings


class AgentCodexHomeTests(unittest.TestCase):
    def test_sync_copies_only_configuration_whitelist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            source.mkdir()
            (source / "config.toml").write_text("model='test'\n", encoding="utf-8")
            (source / "auth.json").write_text('{"token":"secret"}', encoding="utf-8")
            (source / "models.json").write_text('{"models":[]}', encoding="utf-8")
            (source / "history.jsonl").write_text("must-not-copy\n", encoding="utf-8")
            (source / "sessions").mkdir()
            (source / "sessions" / "old.jsonl").write_text(
                "must-not-copy\n",
                encoding="utf-8",
            )
            settings = Settings(
                project_root=base,
                home=base / "team-home",
                db_path=base / "team-home" / "team.sqlite",
                logs_dir=base / "team-home" / "logs",
            )
            first = sync_agent_codex_home(settings, source=source)
            self.assertEqual(
                set(first.copied),
                {"config.toml", "auth.json", "models.json"},
            )
            destination = service_codex_home(settings)
            self.assertTrue((destination / "config.toml").is_file())
            self.assertFalse((destination / "history.jsonl").exists())
            self.assertFalse((destination / "sessions").exists())
            (source / "config.toml").write_text("model='updated'\n", encoding="utf-8")
            second = sync_agent_codex_home(settings, source=source)
            self.assertEqual(second.copied, ("config.toml",))
            self.assertIn("auth.json", second.unchanged)

    def test_cluster_app_server_uses_service_codex_home(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            source.mkdir()
            (source / "config.toml").write_text("model='test'\n", encoding="utf-8")
            settings = Settings(
                project_root=base,
                home=base / "team-home",
                db_path=base / "team-home" / "team.sqlite",
                logs_dir=base / "team-home" / "logs",
            )
            with patch.dict(
                os.environ,
                {
                    "CODEX_HOME": str(source),
                    "CODEX_TEAM_AGENT_RUNTIME": "app_server",
                },
                clear=False,
            ):
                manager = ClusterManager(
                    settings,
                    agent_command=[sys.executable, "-c", "pass"],
                )
            self.assertIsNotNone(manager.runtime)
            self.assertEqual(
                manager.runtime._extra_env["CODEX_HOME"],
                str(service_codex_home(settings)),
            )
            self.assertEqual(manager.runtime._extra_env["CODEX_HOME"], str(manager.agent_codex_home))
            manager.close()


if __name__ == "__main__":
    unittest.main()
