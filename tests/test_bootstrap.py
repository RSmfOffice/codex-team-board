from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_team.bootstrap import install_global_integration
from codex_team.config import Settings


class BootstrapTests(unittest.TestCase):
    def test_mcp_and_agents_configuration_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex_home = root / ".codex"
            codex_home.mkdir()
            (codex_home / "config.toml").write_text(
                """model = "test-provider"

[mcp_servers.codexTeamBoard]
command = "python.exe"
args = ["server.py"]

[mcp_servers.codexTeamBoard.env]
PYTHONUTF8 = "1"
""",
                encoding="utf-8",
            )
            (codex_home / "AGENTS.md").write_text(
                "# Existing instructions\n\nKeep this content.\n",
                encoding="utf-8",
            )
            settings = Settings(
                project_root=Path(__file__).resolve().parents[1],
                home=root / "team-home",
                db_path=root / "team-home" / "team.sqlite",
                logs_dir=root / "team-home" / "logs",
            )
            first = install_global_integration(settings, codex_home=codex_home)
            self.assertTrue(first["config_changed"])
            self.assertTrue(first["agents_changed"])
            config = (codex_home / "config.toml").read_text(encoding="utf-8")
            self.assertIn("startup_timeout_sec = 20", config)
            self.assertIn("tool_timeout_sec = 120", config)
            self.assertIn('default_tools_approval_mode = "approve"', config)
            agents = (codex_home / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("Keep this content.", agents)
            self.assertEqual(agents.count("CODEX_TEAM_BOARD_GLOBAL:START"), 1)
            self.assertEqual(agents.count("CODEX_TEAM_BOARD_GLOBAL:END"), 1)
            self.assertIn(
                'team_write(operation="team_cluster_ensure"',
                agents,
            )
            self.assertIn(
                'team_read(operation="team_list_tasks"',
                agents,
            )
            self.assertIn("非平凡开发任务", agents)

            second = install_global_integration(settings, codex_home=codex_home)
            self.assertFalse(second["config_changed"])
            self.assertFalse(second["agents_changed"])


if __name__ == "__main__":
    unittest.main()
