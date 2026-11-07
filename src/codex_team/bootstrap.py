from __future__ import annotations

import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path

from .config import Settings

SERVER_HEADER = "[mcp_servers.codexTeamBoard]"
AGENTS_START = "<!-- CODEX_TEAM_BOARD_GLOBAL:START -->"
AGENTS_END = "<!-- CODEX_TEAM_BOARD_GLOBAL:END -->"


def _backup(path: Path, stamp: str) -> Path | None:
    if not path.is_file():
        return None
    backup = path.with_name(f"{path.name}.bak-codex-team-board-{stamp}")
    shutil.copy2(path, backup)
    return backup


def _configure_mcp(path: Path, project_root: Path) -> bool:
    if not path.is_file():
        raise FileNotFoundError(f"Codex config not found: {path}")
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    start = next(
        (index for index, line in enumerate(lines) if line.strip() == SERVER_HEADER),
        None,
    )
    if start is None:
        raise ValueError(f"{SERVER_HEADER} is not registered in {path}")
    end = start + 1
    while end < len(lines) and not lines[end].lstrip().startswith("["):
        end += 1

    desired = {
        "cwd": f"cwd = {json.dumps(str(project_root), ensure_ascii=False)}",
        "startup_timeout_sec": "startup_timeout_sec = 20",
        "tool_timeout_sec": "tool_timeout_sec = 120",
        "default_tools_approval_mode": 'default_tools_approval_mode = "approve"',
        "enabled": "enabled = true",
    }
    found: set[str] = set()
    for index in range(start + 1, end):
        match = re.match(r"^\s*([A-Za-z0-9_]+)\s*=", lines[index])
        if match and match.group(1) in desired:
            key = match.group(1)
            lines[index] = desired[key] + "\n"
            found.add(key)
    additions = [value + "\n" for key, value in desired.items() if key not in found]
    if additions:
        lines[end:end] = additions
    updated = "".join(lines)
    if updated != text:
        path.write_bytes(updated.encode("utf-8"))
        return True
    return False


def _agents_block(project_root: Path) -> str:
    return f"""{AGENTS_START}
## Codex 团队协作环境

- 环境位于 `{project_root}`。仅当用户明确要求多 Agent 团队协作，或项目根存在
  `.codex/team-project.json` 时启用本流程。
- 首次接入 Git 项目时，主 Agent 调用 `codexTeamBoard` 的
  `team_init_project`；后续以 `team_list_tasks` / `team_get_task` 为任务状态源。
- 主 Agent 是唯一 Lead，负责创建任务、worktree、最终 Review、合并和关闭任务。
- 处理团队项目时先调用 `team_list_tasks` 检查现有任务、依赖和 Review 状态；
  能恢复已有任务时不得重复创建。
- 子 Agent 返回后必须检查 `team_get_task` 和 `team_get_task_events`；没有 claim
  和 submit/block 事件的回复不能视为完成。
- 已完成子 Agent 不得复用为另一个任务的 follow-up；新材料和新审查必须创建新
  thread。60 秒未 claim 提醒，5 分钟无 heartbeat 标记异常。
- 当前 Codex 不能自动加载角色 TOML 时，调用 `team_role_prompt` 并传入
  `repo_root`，把返回的 Prompt 作为原生子 Agent 的 spawn prompt。
- 默认最多同时运行 4 个 agent thread。只并行文件范围互不重叠的任务。
- 所有 `write` 任务必须先创建 worktree，再认领。Worker 只允许修改自己的 worktree，
  不得切回主 checkout 或修改其他任务的 worktree。
- 创建 worktree 前主 checkout 必须干净并位于命名分支；初始化产生的
  `AGENTS.md` 和 `.codex` 文件应先提交。
- `claim_token` 不写入代码、任务描述、日志或最终回复。长任务必须 heartbeat。
- Worker 提交前必须 commit、清理 worktree，并在 `team_submit_task` 中提供 commit
  SHA、测试命令与结果、变更文件、风险和待决问题。
- Worker 无权标记 `done`。Lead 必须在真实合并并运行集成测试后提供
  `merge_commit`，再调用 `team_review_task`。
- 多模块共享字段必须声明单位、区间、枚举、更新者和消费方，并验证真实生产者到
  真实消费者的一体化路径。
- 接手或验收任务前使用 `team_get_task_events` 检查认领、进度、提交和 Review
  事件，不能只看最终状态。
- 在团队项目中，凡是非平凡开发任务（会修改文件、跨多个步骤或预计超过 5 分钟），
  主 Agent 必须在第一次编辑或创建 worktree 前调用 `team_cluster_ensure`，
  确保项目已有但仅有一个 planning/active cluster goal。已有目标时复用，不重复
  创建；paused goal 不得自动恢复。纯问答和无需修改文件的单次只读检查可跳过。
- `team_cluster_start` 只用于明确新建目标。由常驻 `teamd` 自动启动/恢复 Lead
  和 Worker；不得手工重复 spawn 同一 goal 的 Agent。
- `teamd` 会 resume 中断的 Lead 线程、重建连续失败的 Lead generation、回收过期
  Worker lease，并按 `max_worker_attempts` 自动重试。主 Agent 通过
  `team_cluster_status` 查看状态，不直接管理 Worker 进程。
- `auto` 模式允许受管 Lead 在测试通过后合并并验收；`supervised` 模式在 Review
  阶段自动暂停，等待人工决定。
- 看板地址：`http://127.0.0.1:8765`。启动：
  `powershell -NoProfile -ExecutionPolicy Bypass -File "{project_root / "scripts" / "dashboard.ps1"}"`。
- MCP 工具不可见时的后备入口：
  `powershell -NoProfile -ExecutionPolicy Bypass -File "{project_root / "scripts" / "team.ps1"}" status`。
{AGENTS_END}"""


def _install_agents_block(path: Path, block: str) -> bool:
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    pattern = re.compile(
        re.escape(AGENTS_START) + r".*?" + re.escape(AGENTS_END),
        re.DOTALL,
    )
    if pattern.search(existing):
        updated = pattern.sub(lambda _match: block, existing, count=1)
    elif existing.strip():
        updated = existing.rstrip() + "\n\n" + block + "\n"
    else:
        updated = block + "\n"
    if updated != existing:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(updated.encode("utf-8"))
        return True
    return False


def install_global_integration(
    settings: Settings | None = None,
    *,
    codex_home: Path | None = None,
) -> dict:
    settings = settings or Settings.load()
    codex_home = codex_home or Path.home() / ".codex"
    config_path = codex_home / "config.toml"
    agents_path = codex_home / "AGENTS.md"
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    config_backup = _backup(config_path, stamp)
    agents_backup = _backup(agents_path, stamp)
    config_changed = _configure_mcp(config_path, settings.project_root)
    agents_changed = _install_agents_block(
        agents_path,
        _agents_block(settings.project_root),
    )
    return {
        "config_changed": config_changed,
        "agents_changed": agents_changed,
        "config_backup": str(config_backup) if config_backup else None,
        "agents_backup": str(agents_backup) if agents_backup else None,
    }


if __name__ == "__main__":
    print(json.dumps(install_global_integration(), ensure_ascii=False, indent=2))
