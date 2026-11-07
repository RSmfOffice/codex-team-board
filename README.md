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
- 可在 Web 看板创建、暂停、恢复和取消自动集群目标
- 集群健康摘要、下一步动作和需要关注的异常提示
- 跨项目多集群的全局 Jobs 容量、每 goal Worker 容量和公平排队
- 默认 2 小时 Worker、45 分钟 Lead 的长任务超时与心跳诊断
- WebUI 全局槽位、排队、等待时长和 heartbeat 健康指标

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

### 升级已有安装

先完全退出 Codex，并停止仍在运行的旧看板进程，再更新本仓库并重新运行安装器：

```powershell
Set-Location "D:\path\to\codex-team-board"
git pull --ff-only
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\install.ps1
```

如果仍需联动上下文插件，追加
`-ContextMeterRoot "D:\path\to\codex-context-meter"`。安装完成后重新打开 Codex，
并确认健康接口返回 `"version": "0.4.0"`：

```powershell
Invoke-RestMethod http://127.0.0.1:8765/api/health
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

浏览器打开 `http://127.0.0.1:8765`。看板展示 Ready、Running、Review、
Blocked、Done 泳道，以及依赖、租约、分支、worktree、审计结果、交接事件、
团队消息和自动集群目标。任务状态仍通过 MCP 或 CLI 写入；集群目标和归档操作
可以直接在 WebUI 完成。

也可以双击根目录的 `启动Codex团队看板.cmd`。

看板只显示已经通过 `codexTeamBoard` MCP 或 `team.ps1 task create`/
`cluster start` 写入任务图的任务。普通 Codex 对话和原生子 Agent 不会自动变成
看板任务；如果预期有任务但页面为空，先检查 `/api/board` 的 `tasks` 数量和
`CODEX_TEAM_HOME` 是否指向同一数据库。

### WebUI 集群控制

看板顶部可以直接选择项目、填写目标并创建集群。`auto` 模式会持续调度到目标和
任务完成；`supervised` 模式在执行到 Review 时暂停，等待人工决定。创建请求会
同时确保 `teamd` 已启动。

集群卡提供以下操作：

- Active/Planning：暂停或取消。
- Paused：恢复或取消；恢复会把目标改为 Active 并确保 `teamd` 已启动。
- Completed/Failed：不显示操作按钮。

取消在 Web API 中通过目标状态 `failed` 落板，具体原因保存在 `last_error`，不会
删除目标、任务、事件或 Git worktree。

### 集群健康与 attention

`/api/board` 的 `cluster_summary` 汇总目标、任务、活动进程和 attention：

```text
goals_total
goals_active
goals_paused
goals_failed
ready_tasks
running_tasks
review_tasks
blocked_tasks
active_jobs
global_jobs
global_capacity
queued_goals
heartbeat_overdue
oldest_heartbeat_age_seconds
fairness_wait_count
attention_count
oldest_claim_wait_seconds
```

每个 `cluster_goals` 项还包含 `health`、`next_action` 和
`attention_task_count`，以及 `active_job_count`、`heartbeat_age_seconds`、
`heartbeat_state`、`queue_reason` 和 `estimated_wait_seconds`。WebUI 顶部显示
`JOBS`（活动全局 job/容量）、`QUEUED`（排队目标及公平等待数）和
`HB OVERDUE`（超过 warning 阈值的 job 数及最老心跳年龄）；集群卡显示每个
目标的队列原因、`当前等待`时长、活动 Lead/Worker 和心跳状态。

`queue_reason` 是供 UI 和自动化消费的稳定枚举：

| 值 | WebUI 标签/含义 |
| --- | --- |
| `global_capacity` | 全局容量已满 |
| `goal_capacity` | 目标容量已满 |
| `review_pending` | 等待 Review |
| `blocked` | 等待处理阻塞 |
| `ready` | 等待调度 |
| `dependency_wait` | 等待依赖完成；存在 ready 任务，但它们仍有未完成依赖 |
| `running` | 活动 job 或任务正在运行 |
| `waiting_for_lead` | 等待 Lead 规划 |
| `none` | 无排队 |

`estimated_wait_seconds` 是当前队列关注任务已等待的时长，不代表调度器承诺的
剩余等待时间。因此 WebUI 使用“当前等待”，而不是“预计等待”。

`health` 按以下优先级计算：

| 值 | 条件 |
| --- | --- |
| `critical` | 目标 failed；存在 blocked 任务；或 ready 任务等待认领至少 300 秒 |
| `warning` | 目标 paused；存在 review 任务；或 ready 任务等待认领 60–299 秒 |
| `active` | 存在 starting/running Agent job 或 running 任务 |
| `planning` | 目标仍处于 planning |
| `healthy` | 其他状态 |

attention 的计数口径如下：

- blocked、review 和等待认领至少 300 秒的 ready 任务各计一个 attention 任务。
- review 被视为需要人工处理，因此也会让目标进入 warning。
- ready 等待 60–299 秒只把目标标为 warning，并更新
  `oldest_claim_wait_seconds`，不单独增加 `attention_count`。
- paused 或 failed 目标各增加一个 `attention_count`，但不计入目标的
  `attention_task_count`；后者只统计该目标下的任务。
- 同一目标可有多个任务分别计数，目标自身也可再计一次。因此
  `attention_count` 表示待处理信号数量，不是去重后的目标数。

`next_action` 是供 UI 和自动化消费的稳定枚举，可能为
`inspect_failed_goal`、`resume_or_cancel_goal`、`resolve_blocked_tasks`、
`assign_ready_tasks`、`review_completed_tasks`、`monitor_active_jobs`、
`monitor_running_tasks`、`schedule_ready_tasks`、`wait_for_lead`、`none` 或
`monitor_goal`。

### 集群 API 示例

创建目标需要项目 ID、目标和自治模式。重试次数是可选字段：
`max_worker_attempts` 的范围是 `1..10`，默认 3；`max_lead_attempts` 的范围是
`1..20`，默认 5。

```powershell
$base = "http://127.0.0.1:8765"
$projectId = (Invoke-RestMethod "$base/api/board").projects[0].project_id
$body = @{
  project_id = $projectId
  objective = "完成发版前的实现、测试和文档"
  autonomy = "supervised"
  max_worker_attempts = 3
  max_lead_attempts = 5
} | ConvertTo-Json

$created = Invoke-RestMethod `
  -Uri "$base/api/clusters" `
  -Method Post `
  -ContentType "application/json" `
  -Body $body
$created.goal.goal_id
$created.daemon
```

暂停、恢复或取消使用同一个控制端点：

```powershell
$goalId = $created.goal.goal_id
$control = @{
  goal_id = $goalId
  action = "pause"
} | ConvertTo-Json

Invoke-RestMethod `
  -Uri "$base/api/clusters/control" `
  -Method Post `
  -ContentType "application/json" `
  -Body $control
```

`action` 只接受 `pause`、`resume`、`cancel`。成功响应的形状为
`{"goal": {...}, "daemon": {...}}`；参数错误返回 400，状态或项目冲突返回 409，
错误响应包含 `error`。

### 轮询效率

页面首次加载会读取一次 `/api/projects`，之后常规轮询只请求 `/api/board`。
项目归档状态或“显示已归档”筛选变化时才重新加载项目列表。

看板默认每 2 秒轮询一次，并有以下保护：

- 同一时间只有一个 board 请求在途；队列中的刷新会合并。
- 页面隐藏时停止定时器并取消在途请求，返回页面时立即刷新。
- 请求失败后按 2、4、8、16、30 秒退避，成功响应后恢复 2 秒间隔。
- 只有 board 内容签名变化时才重绘；正常轮询中的 claim 年龄和
  `generated_at` 更新不会单独触发表格重绘。

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
- 取消自动集群目标会终止其活动 Agent job，并把未完成任务保留为 blocked，
  便于后续恢复、重派或审计。

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

### 多集群容量与公平调度

`teamd` 可以同时调度不同 Git 项目中的目标，但以下容量和隔离规则仍然生效：

- 全局 Jobs 容量默认是 `4`。它统计所有项目中状态为 `starting` 或 `running`
  的 Lead 和 Worker，因此 Lead 也占用全局槽位。
- 每个 goal 的 Worker 容量默认是 `3`。Lead 不占每 goal 的 Worker 配额，但仍占
  一个全局 Jobs 槽位；全局容量始终是更外层的上限。
- 同一项目仍只允许一个 `planning` 或 `active` goal。需要并行多个集群时，应为
  不同项目分别创建目标。
- 调度器优先选择最久未启动任务的 goal，再比较该 goal 当前活动 job 数和 goal ID；
  goal 内则按任务 priority、创建时间选择 Worker。这是持续填充全局槽位的公平
  轮转，不保证每个 goal 获得固定槽位数。
- 全局容量已满且某个 goal 仍有可派发 Worker 时，会记录
  `fairness_deferred` 事件，数据包含 `reason: "global capacity full"`、
  `ready_tasks` 和 `capacity`。相同 goal、相同原因的事件最多每 60 秒记录一次；
  该事件只是排队审计信号，不会改变任务状态。

不同项目的目标可以独立创建。例如：

```powershell
.\scripts\team.ps1 cluster start `
  --repo "D:\work\project-a" `
  --objective "完成 project-a 的实现与验证" `
  --autonomy auto

.\scripts\team.ps1 cluster start `
  --repo "D:\work\project-b" `
  --objective "完成 project-b 的实现与验证" `
  --autonomy supervised
```

如果第二个目标指向第一目标所在的项目，会返回
`project already has active goal`。

### 调度配置

以下环境变量由 `teamd` 和相关管理命令读取。四个调度/超时变量控制实际行为，
两个 heartbeat 变量是可选诊断阈值：

| 环境变量 | 默认值 | 输入范围 | 实际调度值/作用 |
| --- | ---: | ---: | --- |
| `CODEX_TEAM_CLUSTER_MAX_JOBS` | `4` | `1..86400` | 调度器再硬限制为最多 `16`；统计所有项目 Lead+Worker 的活动 job |
| `CODEX_TEAM_CLUSTER_MAX_WORKERS` | `3` | `1..86400` | 调度器再硬限制为最多 `8`；限制每个 goal 的活动 Worker |
| `CODEX_TEAM_WORKER_TIMEOUT_SECONDS` | `7200`（2 小时） | `1..86400` | Worker job 从启动到强制超时的时间 |
| `CODEX_TEAM_LEAD_TIMEOUT_SECONDS` | `2700`（45 分钟） | `1..86400` | Lead job 从启动到强制超时的时间 |
| `CODEX_TEAM_HEARTBEAT_WARNING_SECONDS` | `300`（5 分钟） | `1..86400` | Worker 心跳超过该时长后进入 warning |
| `CODEX_TEAM_HEARTBEAT_CRITICAL_SECONDS` | `900`（15 分钟） | `1..86400` | Worker 心跳超过该时长后进入 critical；有效值不会低于 warning |

空值或非整数使用默认值，超出范围的值会被限制到合法边界。配置在 `teamd`、
管理命令和看板进程启动时读取；修改后先停止旧 daemon，再在同一个 PowerShell
会话中设置变量并重新启动 daemon：

```powershell
.\scripts\team.ps1 cluster stop-daemon

$env:CODEX_TEAM_CLUSTER_MAX_JOBS = "6"
$env:CODEX_TEAM_CLUSTER_MAX_WORKERS = "2"
$env:CODEX_TEAM_WORKER_TIMEOUT_SECONDS = "7200"
$env:CODEX_TEAM_LEAD_TIMEOUT_SECONDS = "2700"
$env:CODEX_TEAM_HEARTBEAT_WARNING_SECONDS = "300"
$env:CODEX_TEAM_HEARTBEAT_CRITICAL_SECONDS = "900"

powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\ensure-cluster-daemon.ps1
.\scripts\team.ps1 cluster daemon-status
```

容量变量虽然接受 `1..86400` 的整数输入，但有效调度上限固定为全局 Jobs `16`、
每 goal Workers `8`。例如输入 `999` 时，`teamd` 实际分别按 `16` 和 `8`
调度。看板进程直接读取该进程环境中的整数配置；为避免显示值与实际调度不一致，
应把变量设置在有效上限以内，并让 `teamd` 与看板使用相同环境。

若 critical 阈值小于或等于 warning，调度器和看板都会把有效 critical 提升到
warning，因此 critical 始终不早于 warning。两项心跳变量均可省略。

WebUI 的 `global_capacity` 来自看板进程自身的环境。修改容量后还应停止并重新
启动旧看板，使显示值、队列判断与 `teamd` 的实际调度保持一致。

如果全局上限小于多个 goal 的 Worker 配额之和，全局上限优先。提高每 goal
配额不会突破全局 Jobs 容量。

### 长任务与心跳

- Worker 最近一次 `claimed`、`heartbeat` 或 `progress` 事件超过 5 分钟后，记录
  `worker_heartbeat_warning`。
- 超过 15 分钟后记录 `worker_heartbeat_critical`。心跳告警只写入审计事件和
  WebUI 健康指标，不会直接终止进程。
- Worker 默认运行超过 2 小时会由 `teamd` 终止进程树并标记为失败；任务按
  `max_worker_attempts` 恢复或最终 blocked。Lead 默认运行超过 45 分钟时执行
  相同的超时处理，并按 Lead 重试上限处理。
- 因此，心跳负责提示“可能失去进展”，超时负责实际回收失控 job。长任务应定期
  调用 `team_heartbeat` 或写入 progress，同时确保其总运行时间留在配置范围内。

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
- `read` 任务 Worker 与 Review Worker 使用 `read-only`。
- `supervised` 模式 Lead 使用 `workspace-write`，不会获得自动合并和终审所需
  的完整写权限。
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

输出位于 `dist\codex-team-board-v0.4.0.zip`，同时生成 SHA256 文件。GitHub
Release 应附带 ZIP 与 `.sha256` 两个资产。
