# Codex Team Collaboration Environment

[![Windows CI](https://github.com/RSmfOffice/codex-team-board/actions/workflows/windows-ci.yml/badge.svg)](https://github.com/RSmfOffice/codex-team-board/actions/workflows/windows-ci.yml)
[![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

最新调试结论见 [AUDIT-2026-11-07.md](AUDIT-2026-11-07.md)。

本地优先的 Codex 多 Agent 协作环境，实现 Plan B：

- SQLite 任务图与依赖关系
- 原子认领、claim token 和 lease
- 每个 `write` 任务强制使用独立 Git worktree/branch
- 主 Agent 唯一终审和完成权限
- append-only 任务事件与团队消息
- Codex MCP stdio 服务
- 无外部依赖的本地 Web Kanban

## 两个仓库如何联动

本系统由两个独立仓库组成：

- `RSmfOffice/codex-team-board`：任务数据库、MCP、自动集群、worktree 和 Web 看板。
- `RSmfOffice/codex-context-meter`：跟随 Codex 的上下文监控插件；右键菜单可以
  打开并自动拉起团队看板。

上下文插件通过以下顺序寻找面板：

1. `%LOCALAPPDATA%\CodexContextMeter\config.json` 中的 `team_board_root`。
2. 用户环境变量 `CODEX_TEAM_BOARD_ROOT`，兼容旧变量 `CODEX_TEAM_ROOT`。
3. `%USERPROFILE%\plugins\codex-team-board`。
4. `%USERPROFILE%\CodexTools\codex-team-board`。
5. 两个仓库位于同一父目录时的同级 `codex-team-board`。
6. `%LOCALAPPDATA%\CodexTeamBoard`。

面板安装器会写入 `CODEX_TEAM_BOARD_ROOT` 和 `CODEX_TEAM_DASHBOARD_URL`，因此
不要求两个仓库采用固定绝对路径。只要二者在同级目录，或插件安装时显式传入
`-TeamBoardRoot`，移动安装目录后仍能重新发现。

## 新机器完整安装

### 1. 前置条件

- Windows 10/11。
- Codex 桌面版、Codex CLI。
- Python 3.11+、PowerShell 5.1+。
- 上下文插件需要 Pillow：`python -m pip install -r requirements.txt`。
- 插件 MCP 需要 Node.js 18+。
- 自动 worktree 需要 Git。

### 2. 克隆两个仓库

推荐放在同一父目录：

```powershell
$root = Join-Path $env:USERPROFILE "CodexTools"
New-Item -ItemType Directory -Force -Path $root | Out-Null
Set-Location $root
git clone https://github.com/RSmfOffice/codex-team-board.git
git clone https://github.com/RSmfOffice/codex-context-meter.git
python -m pip install -r .\codex-context-meter\requirements.txt
```

### 3. 一条命令安装并联动

```powershell
Set-Location (Join-Path $env:USERPROFILE "CodexTools\codex-team-board")
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\install.ps1 `
  -ContextMeterRoot (Join-Path $env:USERPROFILE "CodexTools\codex-context-meter")
```

安装器会：

1. 注册 `codexTeamBoard` MCP，并显式传入稳定的 `CODEX_TEAM_HOME`。
2. 安装全局角色、项目角色模板和团队协作 AGENTS 托管区块。
3. 安装 `teamd` watchdog，自动恢复 Lead/Worker 调度。
4. 启动 `http://127.0.0.1:8765`。
5. 调用上下文插件安装器，写入 `team_board_root`、注册个人 marketplace、
   计划任务和插件。

安装完成后完全退出并重新打开 Codex。

### 4. 验证联动

```powershell
Invoke-RestMethod http://127.0.0.1:8765/api/health
codex mcp get codexTeamBoard --json
```

预期看板健康响应包含 `"service": "codex-team-board"`。在 Codex 右侧点击上下文
悬浮窗右键，选择“打开 Codex 团队看板”；看板已运行时应直接打开浏览器，未运行
时应自动启动后打开。

### 单独安装

只安装面板：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

单独安装上下文插件时，显式告知面板路径：

```powershell
Set-Location ..\codex-context-meter
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\codex-context-meter-plugin\scripts\install.ps1 `
  -TeamBoardRoot ..\codex-team-board
```

不使用团队看板时：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\codex-context-meter-plugin\scripts\install.ps1 `
  -NoTeamBoardIntegration
```

上下文插件也可以通过 GitHub marketplace 安装：

```powershell
codex plugin marketplace add RSmfOffice/codex-context-meter --ref v0.1.5
codex plugin add codex-context-meter@codex-tools
```

## 架构

```text
主 Agent / Lead
  ├─ team_create_task
  ├─ team_create_worktree
  └─ team_review_task
          ↓
codexTeamBoard MCP
  ├─ SQLite 任务图 / 租约 / 审计
  └─ 本地 Web 看板
          ↓
team_explorer / team_worker / team_reviewer
  ├─ claim task
  ├─ work only in assigned worktree
  ├─ heartbeat
  └─ submit commit + tests to review
```

任务数据库默认位于：

```text
%LOCALAPPDATA%\CodexTeam\team.sqlite
```

可通过 `CODEX_TEAM_HOME` 覆盖。

在 Windows 打包版 Codex 中，`%LOCALAPPDATA%` 可能被重定向到应用包缓存。
安装脚本会把实际解析后的 `CODEX_TEAM_HOME` 写入用户环境，并显式传给 CLI、
watchdog 和计划任务，确保它们始终连接同一个正式数据库。

## 初始化项目

只支持 Git 仓库。命令会注册项目、安装三个项目级角色，并向
`AGENTS.md` 写入托管协作规则，不覆盖原有内容。

```powershell
.\scripts\team.ps1 init --repo "D:\path\to\repo"
```

当前安装的 Codex `0.157.1` 提供原生 `multi_agent`，但尚未启用自动角色 TOML
加载。环境因此同时安装两层角色：

- `~/.codex/agents/*.toml`：面向后续 Codex 版本的角色配置。
- `.codex/team-roles/*.md`：当前版本使用的便携 Prompt 包。

主 Agent 先调用 `team_role_prompt`，再把返回内容作为原生子 Agent 的 spawn
prompt。这样不依赖当前版本尚未生效的 `multi_agent_v2`。

## 主 Agent 工作流

创建只读调研任务：

```powershell
.\scripts\team.ps1 task create `
  --repo "D:\path\to\repo" `
  --title "梳理认证流程" `
  --description "定位入口、状态变化和风险" `
  --acceptance "返回文件、符号和调用链证据" `
  --kind read `
  --owner-role team_explorer
```

创建写作任务：

```powershell
.\scripts\team.ps1 task create `
  --repo "D:\path\to\repo" `
  --title "实现登录修复" `
  --description "只在认证模块内修改" `
  --acceptance "回归测试通过且无未提交文件" `
  --kind write `
  --owner-role team_worker
```

为写作任务建立隔离工作区：

```powershell
.\scripts\team.ps1 worktree create <task-id> --repo "D:\path\to\repo"
```

在此之前主 checkout 必须干净并位于命名分支。`team init` 生成的 `AGENTS.md`
和 `.codex` 文件应先由用户提交，保证每个 worktree 都包含团队规范。

Worker 认领：

```powershell
.\scripts\team.ps1 task claim <task-id> --actor worker-auth
```

认领结果中的 `claim_token` 不能写入 Git、日志、任务描述或最终回复。Worker 在
指定 worktree 中完成修改、提交和测试后：

```powershell
.\scripts\team.ps1 task submit <task-id> `
  --actor worker-auth `
  --token "<claim-token>" `
  --commit "<worker-commit>" `
  --result "修复了 token 刷新竞态" `
  --tests "pytest tests/auth -q: 42 passed"
```

自动集群的 Codex JSONL 日志会实时把 `claim_token` 字段替换为
`[REDACTED]`；作业结束时还会再次扫描日志和最终输出来清除遗漏值。

`result` 使用固定交接格式，避免信息遗漏：

```text
行为：
变更文件：
验证：
风险：
Review 重点：
待决问题：
```

查看任务交接事件：

```powershell
.\scripts\team.ps1 task events <task-id>
```

主 Agent 合并分支、运行集成测试后：

```powershell
.\scripts\team.ps1 task review <task-id> `
  --actor lead `
  --approve `
  --notes "Review 和集成测试通过" `
  --merge-commit "<main-merge-commit>"
```

`write` 任务审批时系统会检查：

- worktree 仍然存在；
- worktree 没有未提交文件；
- `merge_commit` 能在主仓库解析；
- `merge_commit` 已包含在创建任务时固定下来的目标分支中，不受当前 checkout
  切换影响。

## 可视化看板

```powershell
.\scripts\dashboard.ps1
```

浏览器打开 `http://127.0.0.1:8765`。看板每 2 秒刷新，只读展示：Ready、
Running、Review、Blocked、Done 泳道，以及依赖、租约、分支、worktree、审计结果、
交接事件和团队消息。所有写入仍通过 MCP 或 CLI 完成。

也可以双击根目录的 `启动Codex团队看板.cmd`。

看板只显示已经通过 `codexTeamBoard` MCP 或 `team.ps1 task create`/
`cluster start` 写入任务图的任务。普通 Codex 对话和原生子 Agent 不会自动变成
看板任务；如果预期有任务但页面为空，先检查 `/api/board` 的 `tasks` 数量和
`CODEX_TEAM_HOME` 是否指向同一数据库。

### 项目与任务归档

归档按钮会根据当前上下文切换目标：

- 没有打开任务详情时：归档或恢复当前项目。
- 打开任务详情时：归档或恢复当前任务。
- 先关闭任务详情，按钮才会切回项目归档。

项目和任务都采用软归档：

- 默认的“所有活动项目”视图会隐藏归档项目、归档任务及其消息。
- 勾选“显示已归档”后可以重新查看，按钮会切换为“恢复当前项目”或
  “恢复当前任务”。
- 归档不会删除数据库记录、任务事件、worktree、分支或合并提交。
- 有未结束的集群目标、Running 任务或活动 Agent 进程时禁止归档。
- 已归档任务不能 claim；已归档项目不能创建任务、cluster goal 或 worktree，
  恢复后解除限制。

## MCP 工具

- `team_init_project`
- `team_status`
- `team_list_roles`
- `team_role_prompt`
- `team_create_task`
- `team_list_tasks`
- `team_get_task`
- `team_get_task_events`
- `team_create_worktree`
- `team_list_worktrees`
- `team_claim_task`
- `team_heartbeat`
- `team_update_task`
- `team_submit_task`
- `team_block_task`
- `team_unblock_task`
- `team_cancel_task`
- `team_review_task`
- `team_add_message`
- `team_list_messages`

## 状态规则

```text
ready → running → review → done
   ↘ blocked ↗        ↘ ready（拒绝）
ready/review/blocked ──取消──→ canceled
```

- 有未完成依赖时不能 claim。
- 一个任务同一时间只有一个有效 claim。
- claim 超过 lease 后可被重新领取。
- `write` 任务没有 worktree 时不能 claim。
- Worker 不能将任务直接标记 `done`。
- 只有 `lead`、`main` 或 `human` 可以终审。
- 非 Running 的失效任务可以由 Lead 取消；取消原因和事件永久保留。

## 集群可靠性约束

- `team_create_task.priority` 范围固定为 `0..3`，其中 `0` 最高、`3` 最低。
- 子 Agent 的第一个团队动作必须是 claim；结束时必须 submit 或 block。
- Lead 在子 Agent 返回后检查 `team_get_task_events`；没有 claim 和
  submit/block 事件的回答不能视为完成。
- 已完成 Agent 不复用为下一任务；新材料和新审查创建新 thread。
- 任务 ready 后 60 秒未 claim 显示黄色提醒，5 分钟显示红色异常。
- 跨模块共享字段必须声明名称、单位、合法区间、枚举、更新者和消费方，并验证
  真实生产者到真实消费者的一体化路径。
- 团队角色模板在初始化时同步到项目；managed 文件固定使用 UTF-8/LF，避免
  Windows checkout 因 CRLF 产生整文件伪 diff。

## 自动 Agent 集群

启动一个持续目标：

```powershell
.\scripts\team.ps1 cluster start `
  --repo "D:\path\to\repo" `
  --objective "实现并验证目标，自动拆分、执行、测试和验收" `
  --autonomy auto
```

常驻 `teamd` 会：

- 首次启动受管 Lead 线程，由 Lead 拆任务。
- 自动为 `ready` 任务启动 Worker，并创建 write worktree。
- Worker 异常退出后回收任务并按上限重试。
- Lead 进程中断后优先 resume 同一 thread；连续失败后新建 Lead generation，
  用 board 状态恢复。
- 所有 task 完成后自动关闭 goal。
- `supervised` 模式只自动执行到 Review，不自动合并。

Codex 的 `workspace-write` 沙箱会把 `.git/` 递归设为只读，即使工作区其他位置
可写。因此：

- `write` Worker 和 `auto` 模式 Lead 使用 `danger-full-access`，以便执行
  `git commit`、合并和验收。
- 只读调研与普通 Review Worker 继续使用 `workspace-write`。
- 这只应运行在可信的本地项目和由 `teamd` 分配的隔离 worktree 中。
- 项目初始化是幂等的；未显式传入 `--max-threads` 时保留已有配置，不会因为
  Lead 再次确认项目而把干净 checkout 改脏。

相关官方说明：
[Protected paths in writable roots](https://learn.chatgpt.com/docs/agent-approvals-security#protected-paths-in-writable-roots)。

查看和停止：

```powershell
.\scripts\team.ps1 cluster list
.\scripts\team.ps1 cluster status <goal-id>
.\scripts\team.ps1 cluster pause <goal-id>
.\scripts\team.ps1 cluster resume <goal-id>
.\scripts\team.ps1 cluster cancel <goal-id>
```

优先注册 Windows 计划任务 `Codex Team Cluster`，每 5 分钟检查一次 daemon。
如果计划任务注册被系统策略拒绝，安装器会退回到用户级启动 watchdog：

```text
HKCU\Software\Microsoft\Windows\CurrentVersion\Run\CodexTeamClusterWatchdog
```

watchdog 每分钟调用 `ensure-cluster-daemon.ps1`，并通过命名互斥量防止重复实例。
它不需要管理员权限；Windows 询问是否允许 PowerShell 开机启动时，这一项就是
自动恢复组件。拒绝启动不会破坏当前会话，但登录后不会自动拉起 `teamd`。

## 测试

```powershell
$env:PYTHONPATH = ".\src"
python -m unittest discover -s tests -v
```

## 发布包

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\build-release.ps1
```

输出位于 `dist\codex-team-board-v0.2.0.zip`，同时生成 SHA256 文件。GitHub
Release 应附带 ZIP 与 `.sha256` 两个资产。
