from __future__ import annotations

import ctypes
import json
import os
import socket
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from codex_team import __version__
from codex_team.db import SCHEMA_VERSION


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install.ps1"
POWERSHELL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / (
    "System32\\WindowsPowerShell\\v1.0\\powershell.exe"
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


def _terminate_process(pid: int) -> None:
    if not pid or not _pid_exists(pid):
        return
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


@unittest.skipUnless(os.name == "nt", "install.ps1 is Windows-only")
class InstallTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.codex_home = self.root / "codex-home"
        self.codex_home.mkdir()
        self.team_home = self.root / "team-home"
        self.user = self.root / "user"
        self.local = self.root / "local"
        self.state = self.root / "state"
        self.context = self.root / "context-meter"
        for path in (self.user, self.local, self.state, self.context):
            path.mkdir(parents=True, exist_ok=True)

        self.initial_config = (
            'model = "test-provider"\n\n'
            "[mcp_servers.legacy]\n"
            'command = "legacy.exe"\n'
        )
        self.initial_agents = "# Existing global instructions\n\nKeep this.\n"
        (self.codex_home / "config.toml").write_text(
            self.initial_config, encoding="utf-8"
        )
        (self.codex_home / "AGENTS.md").write_text(
            self.initial_agents, encoding="utf-8"
        )

        agent_dir = self.user / ".codex" / "agents"
        role_dir = self.user / ".codex" / "team-roles"
        agent_dir.mkdir(parents=True)
        role_dir.mkdir(parents=True)
        (agent_dir / "team-worker.toml").write_text(
            'name = "old-worker"\n', encoding="utf-8"
        )
        (role_dir / "team-worker.md").write_text(
            "# old worker\n", encoding="utf-8"
        )

        self.initial_environment = {
            "CODEX_TEAM_HOME": str(self.root / "old-team-home"),
            "CODEX_TEAM_BOARD_ROOT": str(self.root / "old-board"),
            "CODEX_TEAM_DASHBOARD_URL": "http://127.0.0.1:1",
        }
        (self.state / "user-environment.json").write_text(
            json.dumps(self.initial_environment, indent=2) + "\n",
            encoding="utf-8",
        )
        (self.state / "scheduled-tasks.json").write_text(
            '{"tasks":["baseline"]}\n', encoding="utf-8"
        )
        (self.state / "startup-run.json").write_text(
            '{"values":{"Baseline":"keep"}}\n', encoding="utf-8"
        )
        context_state = self.local / "CodexContextMeter"
        context_state.mkdir()
        self.initial_context = '{"team_board_root":"baseline"}\n'
        (context_state / "config.json").write_text(
            self.initial_context, encoding="utf-8"
        )

        self.fixtures = self.root / "fixtures"
        self.fixtures.mkdir()
        self.cluster_script = self.fixtures / "cluster.ps1"
        self.dashboard_script = self.fixtures / "dashboard.ps1"
        self.context_installer = self.fixtures / "context.ps1"
        self.sleep_script = self.fixtures / "sleep.ps1"
        self._write_fixtures()

    def tearDown(self) -> None:
        for pid in sorted(self._started_pids(), reverse=True):
            _terminate_process(pid)
        self.temporary.cleanup()

    def _write_fixtures(self) -> None:
        self.sleep_script.write_text(
            "param([int]$Seconds = 60)\nStart-Sleep -Seconds $Seconds\n",
            encoding="utf-8",
        )
        self.cluster_script.write_text(
            textwrap.dedent(
                r"""
                $ErrorActionPreference = "Stop"
                $stateRoot = Join-Path $env:CODEX_TEAM_INSTALL_TEST_ROOT "state"
                [IO.File]::WriteAllText(
                    (Join-Path $stateRoot "scheduled-tasks.json"),
                    '{"tasks":["registered"]}' + "`n",
                    [Text.UTF8Encoding]::new($false)
                )
                [IO.File]::WriteAllText(
                    (Join-Path $stateRoot "startup-run.json"),
                    '{"values":{"CodexTeamClusterWatchdog":"registered"}}' + "`n",
                    [Text.UTF8Encoding]::new($false)
                )
                $process = Start-Process `
                    -FilePath (Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe") `
                    -ArgumentList @(
                        "-NoProfile",
                        "-ExecutionPolicy", "Bypass",
                        "-File", ('"' + $env:CODEX_TEAM_INSTALL_SLEEP_SCRIPT + '"')
                    ) `
                    -WindowStyle Hidden `
                    -PassThru
                Add-Content `
                    -LiteralPath (Join-Path $stateRoot "started-pids.txt") `
                    -Value ([string]$process.Id)
                @{
                    ok = $true
                    registration = "scheduled_task"
                    processId = $process.Id
                    processCreationTime = $process.StartTime.ToUniversalTime().ToString("o")
                } | ConvertTo-Json -Compress
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        self.context_installer.write_text(
            textwrap.dedent(
                r"""
                param(
                    [string]$TeamBoardRoot,
                    [string]$DashboardUrl
                )
                $ErrorActionPreference = "Stop"
                $target = Join-Path $env:CODEX_TEAM_INSTALL_TEST_ROOT "local\CodexContextMeter\config.json"
                New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
                [IO.File]::WriteAllText(
                    $target,
                    (@{
                        team_board_root = $TeamBoardRoot
                        dashboard_url = $DashboardUrl
                    } | ConvertTo-Json -Compress) + "`n",
                    [Text.UTF8Encoding]::new($false)
                )
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        self.dashboard_script.write_text(
            textwrap.dedent(
                r"""
                param(
                    [int]$Port,
                    [switch]$NoBrowser
                )
                $ErrorActionPreference = "Stop"
                $listener = [System.Net.Sockets.TcpListener]::new(
                    [System.Net.IPAddress]::Loopback,
                    $Port
                )
                try {
                    $listener.Start()
                    while ($true) {
                        $client = $listener.AcceptTcpClient()
                        try {
                            $stream = $client.GetStream()
                            $reader = [IO.StreamReader]::new(
                                $stream,
                                [Text.Encoding]::ASCII,
                                $false,
                                1024,
                                $true
                            )
                            while ($true) {
                                $line = $reader.ReadLine()
                                if ($null -eq $line -or $line -eq "") {
                                    break
                                }
                            }
                            $countPath = Join-Path $env:CODEX_TEAM_INSTALL_TEST_ROOT "state\health-count.txt"
                            Add-Content -LiteralPath $countPath -Value "1"
                            $teamHome = $env:CODEX_TEAM_HOME
                            $body = @{
                                ok = $true
                                service = "codex-team-board"
                                version = $env:CODEX_TEAM_INSTALL_EXPECTED_VERSION
                                team_home = $teamHome
                                database = (Join-Path $teamHome "team.sqlite")
                                database_id = "test-database"
                                schema_version = [int]$env:CODEX_TEAM_INSTALL_EXPECTED_SCHEMA
                                cluster_runtime = @{
                                    runtime = "app_server"
                                    thread_reuse = $true
                                    running = $false
                                    config_mismatch = $false
                                    version = $env:CODEX_TEAM_INSTALL_EXPECTED_VERSION
                                }
                                generated_at = [DateTime]::UtcNow.ToString("o")
                            } | ConvertTo-Json -Depth 6 -Compress
                            $bytes = [Text.Encoding]::UTF8.GetBytes($body)
                            $header = (
                                "HTTP/1.1 200 OK`r`n" +
                                "Content-Type: application/json; charset=utf-8`r`n" +
                                "Content-Length: " + $bytes.Length + "`r`n" +
                                "Connection: close`r`n`r`n"
                            )
                            $headerBytes = [Text.Encoding]::ASCII.GetBytes($header)
                            $stream.Write($headerBytes, 0, $headerBytes.Length)
                            $stream.Write($bytes, 0, $bytes.Length)
                            $stream.Flush()
                        }
                        finally {
                            $client.Dispose()
                        }
                    }
                }
                finally {
                    $listener.Stop()
                }
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )

    def _environment(self, fault: str | None = None) -> dict[str, str]:
        environment = os.environ.copy()
        environment.update(
            {
                "CODEX_TEAM_INSTALL_TEST_ROOT": str(self.root),
                "CODEX_TEAM_HOME": str(self.team_home),
                "CODEX_TEAM_INSTALL_CLUSTER_SCRIPT": str(self.cluster_script),
                "CODEX_TEAM_INSTALL_DASHBOARD_SCRIPT": str(self.dashboard_script),
                "CODEX_TEAM_INSTALL_CONTEXT_INSTALLER": str(
                    self.context_installer
                ),
                "CODEX_TEAM_INSTALL_SLEEP_SCRIPT": str(self.sleep_script),
                "CODEX_TEAM_INSTALL_HEALTH_TIMEOUT_SECONDS": "2",
                "CODEX_TEAM_INSTALL_EXPECTED_VERSION": __version__,
                "CODEX_TEAM_INSTALL_EXPECTED_SCHEMA": str(SCHEMA_VERSION),
                "PYTHONPATH": str(ROOT / "src"),
            }
        )
        environment.pop("CODEX_TEAM_INSTALL_TEST_FAULT", None)
        if fault:
            environment["CODEX_TEAM_INSTALL_TEST_FAULT"] = fault
        return environment

    def _run_install(
        self,
        *,
        port: int | None = None,
        context_root: Path | None = None,
        fault: str | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, object], Path]:
        port = port or _free_port()
        result_path = self.state / f"result-{port}-{fault or 'success'}.json"
        command = [
            str(POWERSHELL),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(INSTALLER),
            "-DashboardUrl",
            f"http://127.0.0.1:{port}",
            "-ResultPath",
            str(result_path),
        ]
        if context_root is not None:
            command.extend(["-ContextMeterRoot", str(context_root)])
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=self._environment(fault),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            check=False,
        )
        if not result_path.is_file():
            self.fail(
                "installer did not write a result file\n"
                f"stdout:\n{completed.stdout}\n"
                f"stderr:\n{completed.stderr}"
            )
        result = json.loads(result_path.read_text(encoding="utf-8"))
        return completed, result, result_path

    def _started_pids(self) -> set[int]:
        pids: set[int] = set()
        started = self.state / "started-pids.txt"
        if started.is_file():
            for line in started.read_text(encoding="utf-8").splitlines():
                if line.strip().isdigit():
                    pids.add(int(line.strip()))
        for result_path in self.state.glob("result-*.json"):
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            dashboard = result.get("dashboard")
            if isinstance(dashboard, dict) and dashboard.get("process_pid"):
                pids.add(int(dashboard["process_pid"]))
        return pids

    def _assert_initial_state(self) -> None:
        self.assertEqual(
            (self.codex_home / "config.toml").read_text(encoding="utf-8"),
            self.initial_config,
        )
        self.assertEqual(
            (self.codex_home / "AGENTS.md").read_text(encoding="utf-8"),
            self.initial_agents,
        )
        self.assertEqual(
            json.loads(
                (self.state / "user-environment.json").read_text(encoding="utf-8")
            ),
            self.initial_environment,
        )
        self.assertEqual(
            (self.state / "scheduled-tasks.json").read_text(encoding="utf-8"),
            '{"tasks":["baseline"]}\n',
        )
        self.assertEqual(
            (self.state / "startup-run.json").read_text(encoding="utf-8"),
            '{"values":{"Baseline":"keep"}}\n',
        )
        self.assertEqual(
            (
                self.local / "CodexContextMeter" / "config.json"
            ).read_text(encoding="utf-8"),
            self.initial_context,
        )
        self.assertEqual(
            (
                self.user / ".codex" / "agents" / "team-worker.toml"
            ).read_text(encoding="utf-8"),
            'name = "old-worker"\n',
        )
        self.assertEqual(
            (
                self.user / ".codex" / "team-roles" / "team-worker.md"
            ).read_text(encoding="utf-8"),
            "# old worker\n",
        )
        self.assertEqual(list((self.user / ".codex" / "agents").glob("*.toml")), [
            self.user / ".codex" / "agents" / "team-worker.toml"
        ])
        self.assertEqual(list((self.user / ".codex" / "team-roles").glob("*.md")), [
            self.user / ".codex" / "team-roles" / "team-worker.md"
        ])

    def test_preflight_failure_has_no_side_effects(self) -> None:
        completed, result, _ = self._run_install(
            context_root=self.root / "missing-context-meter"
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_stage"], "preflight")
        self.assertFalse(result["rollback"])
        self._assert_initial_state()
        self.assertFalse((self.state / "health-count.txt").exists())
        self.assertFalse((self.team_home / "logs" / "clusterd.lock").exists())
        role_files = list((self.user / ".codex").rglob("*.toml"))
        self.assertEqual(
            [path.name for path in role_files],
            ["team-worker.toml"],
        )

    def _assert_mid_install_failure(self, fault: str) -> None:
        completed, result, _ = self._run_install(
            context_root=self.context,
            fault=fault,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            result["failure_stage"],
            "staging" if fault == "bootstrap" else fault,
            msg=(
                f"installer error: {result.get('error')}\n"
                f"stdout:\n{completed.stdout}\n"
                f"stderr:\n{completed.stderr}"
            ),
        )
        self.assertEqual(result["status"], "failed")
        self._assert_initial_state()
        if fault == "bootstrap":
            self.assertFalse(result["rollback"])
        else:
            self.assertTrue(result["rollback"]["attempted"])
            self.assertTrue(result["rollback"]["ok"])
            stopped = result["rollback"]["stopped_processes"]
            for pid in stopped:
                self.assertFalse(_pid_exists(int(pid)))
        staging_root = self.local / "CodexTeam"
        if staging_root.exists():
            self.assertFalse(any(staging_root.glob("install-staging-*")))

    def test_bootstrap_failure_has_no_live_side_effects(self) -> None:
        self._assert_mid_install_failure("bootstrap")

    def test_watchdog_failure_rolls_back_config_task_and_process(self) -> None:
        self._assert_mid_install_failure("watchdog")

    def test_dashboard_failure_rolls_back_started_process(self) -> None:
        self._assert_mid_install_failure("dashboard")

    def test_context_failure_rolls_back_context_and_core_state(self) -> None:
        self._assert_mid_install_failure("context")

    def test_success_is_idempotent_and_health_contract_is_repeated(self) -> None:
        port = _free_port()
        completed, first, _ = self._run_install(
            port=port,
            context_root=self.context,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )
        self.assertTrue(first["ok"])
        self.assertTrue(first["installed"]["requires_new_codex_session"])
        self.assertEqual(first["verification"]["mcp_switch_count"], 1)
        self.assertEqual(first["verification"]["mcp_registration_count"], 1)
        self.assertGreaterEqual(first["dashboard"]["health_attempts"], 2)
        self.assertGreaterEqual(
            first["verification"]["dashboard_health_attempts"], 2
        )

        config = (self.codex_home / "config.toml").read_text(encoding="utf-8")
        self.assertEqual(
            config.count("[mcp_servers.codexTeamBoard]"),
            1,
        )
        self.assertIn('model = "test-provider"', config)
        self.assertIn("[mcp_servers.legacy]", config)
        self.assertIn("CODEX_TEAM_BOARD_GLOBAL:START", (
            self.codex_home / "AGENTS.md"
        ).read_text(encoding="utf-8"))
        environment = json.loads(
            (self.state / "user-environment.json").read_text(encoding="utf-8")
        )
        self.assertEqual(environment["CODEX_TEAM_HOME"], str(self.team_home))
        self.assertEqual(
            environment["CODEX_TEAM_DASHBOARD_URL"],
            f"http://127.0.0.1:{port}",
        )
        for name in (
            "team-explorer.toml",
            "team-worker.toml",
            "team-reviewer.toml",
        ):
            self.assertEqual(
                (
                    self.user / ".codex" / "agents" / name
                ).read_bytes(),
                (ROOT / "templates" / "agents" / name).read_bytes(),
            )
        for name in (
            "team-explorer.md",
            "team-worker.md",
            "team-reviewer.md",
        ):
            self.assertEqual(
                (
                    self.user / ".codex" / "team-roles" / name
                ).read_bytes(),
                (ROOT / "templates" / "roles" / name).read_bytes(),
            )
        self.assertGreaterEqual(
            int((self.state / "health-count.txt").read_text().count("1")),
            4,
        )
        context_config = json.loads(
            (
                self.local / "CodexContextMeter" / "config.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(context_config["team_board_root"], str(ROOT))

        completed, second, _ = self._run_install(
            port=port,
            context_root=self.context,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )
        self.assertTrue(second["ok"])
        self.assertFalse(second["dashboard"]["started_by_installer"])
        self.assertEqual(second["verification"]["mcp_switch_count"], 1)
        self.assertEqual(
            (
                self.codex_home / "config.toml"
            ).read_text(encoding="utf-8").count(
                "[mcp_servers.codexTeamBoard]"
            ),
            1,
        )


if __name__ == "__main__":
    unittest.main()
