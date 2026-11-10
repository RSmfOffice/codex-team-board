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
- 可在 Web 看板创建、暂停、恢复、取消和归档自动集群目标
- 集群健康摘要、下一步动作和需要关注的异常提示
- 每个 active goal 独立保留 1 个 Lead 和最多 3 个 Worker；系统另有进程安全上限
- 默认 2 小时 Worker、45 分钟 Lead 的长任务超时与心跳诊断
- WebUI Worker 槽位、Lead 数、排队、等待时长和 heartbeat 健康指标
- Prompt Contract 与完整 cache profile，稳定前缀变化时禁止旧 Thread 复用
- managed 与 native Thread 统一登记；native 仅允许同任务 follow-up

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

预期看板健康响应包含 `"service": "codex-team-board"`、`team_home`、`database`、
`database_id` 和 `schema_version`。插件会拒绝打开数据库身份不匹配的看板。在 Codex 右侧点击上下文
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
并确认健康接口返回 `"version": "0.5.0"`：

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
  ├─ team_write: team_create_task
  ├─ team_write: team_create_worktree
  └─ team_write: team_review_task
          ↓
codexTeamBoard MCP
  ├─ SQLite 任务图 / 租约 / 审计
  └─ 本地 Web 看板
          ↓
team_explorer / team_worker / team_reviewer
  ├─ team_write: team_agent_register_native（原生子 Agent）
  ├─ team_write: team_claim_task
  ├─ work only in assigned worktree
  ├─ team_write: team_heartbeat
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

主 Agent 先调用
`team_read(operation="team_role_prompt", arguments={...})`，再把返回内容作为
原生子 Agent 的 spawn prompt。这样不依赖当前版本尚未生效的
`multi_agent_v2`。

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
worktree 创建会先写入 `external_operations` durable intent，再执行 Git 操作并
回写 task。`teamd` 每个 tick 会恢复已存在但未完成数据库落板的 worktree；若
Git 结果缺失、branch/target 不匹配或仓库已不可用，intent 会标记 failed 供审计。

Worker 认领：

```powershell
.\scripts\team.ps1 task claim <task-id> --actor worker-auth
```

`write` 任务 claim 前会验证 worktree 的完整归属：目录必须是独立 Git
worktree，实际 checkout branch 必须等于 task 记录，路径和 branch 不能属于
另一个 task，且 worktree 必须干净。submit 和 approve 时重复检查同一门禁，
避免旧任务目录、旧分支或手工篡改把新任务带入错误工作区。

Goal、Task 和每次 cluster job 还绑定 `workflow_epoch`。该 epoch 由稳定的
Prompt Contract、AGENTS、角色 prompts、Core schema、access/surface、运行时和
模型配置派生。新 Goal/Task 只接受当前 epoch；旧 ready Task 会被阻断，旧 Goal
不会自动 resume 或执行 Lead。需要升级旧 cohort 时必须显式迁移：

```powershell
.\scripts\team.ps1 cluster migrate-workflow <goal-id> `
  --actor human `
  --reason "reviewed current stable prefix"
```

有活动 job 或 running task 时迁移会被拒绝，避免把正在运行的旧工作流切换到
新前缀。存在未完成任务时必须先写入 durable checkpoint，再执行显式迁移。
Dashboard/API 会为 Goal、Task 和最近 job 显示 `workflow_epoch`。

`WORKFLOW_EPOCH_VERSION` 只在状态机、任务字段、执行契约或 prefix 结构发生
语义变化时升级；普通实现重构不得每个 commit 都升级，否则会无谓切断仍在兼容
范围内的旧队列。

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
- task 记录的 Worker commit 已包含在 `merge_commit` 中；
- `merge_commit` 能在主仓库解析；
- `merge_commit` 已包含在创建任务时固定下来的目标分支中，不受当前 checkout
  切换影响。
- task/project 未归档，且 Review 期间 task version 没有变化。

## 可视化看板

```powershell
.\scripts\dashboard.ps1
```

浏览器打开 `http://127.0.0.1:8765`。看板展示 Ready、Running、Review、
Blocked、Done 泳道，以及依赖、租约、分支、worktree、审计结果、交接事件、
团队消息和自动集群目标。任务状态仍通过 MCP 或 CLI 写入；集群目标和归档操作
可以直接在 WebUI 完成。

Dashboard 的写接口只接受 loopback `Host`、same-origin `Origin`（无 Origin 的
CLI 客户端保持兼容）和 `application/json`。非法来源或媒体类型在业务状态写入前
返回 403/415，防止跨站请求与 DNS rebinding 操作本地任务库。

也可以双击根目录的 `启动Codex团队看板.cmd`。

看板只显示已经通过 `codexTeamBoard` MCP 或 `team.ps1 task create`/
`cluster start` 写入任务图的任务。普通 Codex 对话和未登记的原生子 Agent 不会
自动变成看板任务；native 子 Agent 创建后需显式调用
`team_agent_register_native`。如果预期有任务但页面为空，先检查 `/api/board`
的 `tasks` 数量和 `CODEX_TEAM_HOME` 是否指向同一数据库。

### WebUI 集群控制

看板顶部可以直接选择项目、填写目标并创建集群。`auto` 模式会持续调度到目标和
任务完成；`supervised` 模式在执行到 Review 时暂停，等待人工决定。创建请求会
同时确保 `teamd` 已启动。

Goal resume 先确保 daemon 启动成功，再把状态改为 active；daemon 启动失败时
Goal 保持 paused。项目或任务归档与 claim 在同一个 `BEGIN IMMEDIATE` 事务内
竞争，不会产生“归档项目下仍有 running task”的组合。

集群卡提供以下操作：

- Active/Planning：暂停或取消。
- Paused：恢复或取消；恢复会把目标改为 Active 并确保 `teamd` 已启动。
- Completed/Failed：归档已完成或失败的目标；归档后可恢复。

取消在 Web API 中通过目标状态 `failed` 落板，具体原因保存在 `last_error`，不会
删除目标、任务、事件或 Git worktree。

### 集群健康与 attention

`/api/summary` 与 `/api/board` 的 `cluster_summary` 汇总目标、任务、活动进程和
attention：

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
active_workers
active_leads
global_jobs
global_capacity
worker_capacity_per_goal
system_jobs
system_capacity
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
`WORKER SLOTS`（活动 Worker/所有 active goal 的 Worker 槽位总和）、
`LEADS`（活动主 Agent 数）、`QUEUED`（排队目标及公平等待数）和
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

只有 `completed` 或 `failed` 的集群目标可以归档。归档和恢复使用独立端点：

```powershell
$archive = @{
  goal_id = $goalId
  archived = $true
} | ConvertTo-Json

Invoke-RestMethod `
  -Uri "$base/api/clusters/archive" `
  -Method Post `
  -ContentType "application/json" `
  -Body $archive
```

将 `archived` 设为 `false` 可恢复目标。归档目标默认不进入 `/api/board`；使用
`?include_archived=1` 或 WebUI 的“显示已归档”筛选后可以查看和恢复。
CLI 对应命令为 `cluster archive <goal-id>` 和 `cluster restore <goal-id>`；
MCP 的 `team_cluster_control.action` 除 `pause`、`resume`、`cancel` 外，也接受
`archive` 和 `restore`。

### 轮询效率

Dashboard 读接口按用途拆分，完整 `/api/board` 继续保留用于兼容：

```text
GET /api/summary
GET /api/tasks
GET /api/goals
GET /api/agents
GET /api/attempts
GET /api/events
GET /api/tasks/<task-id>/events
GET /api/board
```

`/api/summary`、`/api/board` 和列表接口接受 `project_id` 与
`include_archived=1`。列表接口默认返回 100 条，`limit` 最大 500；失败时返回
结构化 `400`，不会静默扩大响应。`/api/tasks`、`/api/goals`、`/api/agents`
和 `/api/attempts` 使用返回的最后一项 ID 作为 `cursor`；事件接口使用
`event_id`：

```text
/api/events?limit=100&cursor=<oldest-event-id>
/api/tasks/<task-id>/events?limit=12
```

`/api/summary` 只返回项目、计数、容量、心跳和 revision，不返回任务行、Agent
行或事件体。`/api/board` 与 `/api/summary` 都返回 revision 和 ETag；客户端带上
`If-None-Match` 时，状态未变化会得到 `304` 且没有 JSON body。

页面首次加载会读取一次 `/api/projects`，之后每 2 秒先读取 `/api/summary`。
summary revision 未变化时不请求 `/api/board`；只有 revision 变化、首次加载、
项目筛选变化或显式强制刷新时才下载完整 board。任务详情不依赖全局最新 500
条事件，而是异步读取该任务自己的分页事件。

看板默认每 2 秒轮询一次，并有以下保护：

- 同一时间只有一个 summary/board 请求序列在途；队列中的刷新会合并。
- 页面隐藏时停止定时器并取消在途请求，返回页面时立即刷新。
- 请求失败后按 2、4、8、16、30 秒退避，成功响应后恢复 2 秒间隔。
- 只有 board 内容签名变化时才重绘；正常轮询中的 claim 年龄和
  `generated_at` 更新不会单独触发表格重绘。
- Token/cache 缺失保持 `null` 并显示 `—`，真实零值显示 `0`；运行时动作区分
  `fresh`、`resume`、`fresh_exec` 和 `fallback_exec`，不再把所有非复用路径
  都显示成 `fresh exec`。

### 项目、任务与集群目标归档

归档按钮会根据当前上下文切换目标：

- 没有打开任务详情时：归档或恢复当前项目。
- 打开任务详情时：归档或恢复当前任务。
- 集群目标使用目标卡片上的“归档目标（含Done）”或“恢复目标（含Done）”按钮。
- 先关闭任务详情，按钮才会切回项目归档。

项目、任务和集群目标都采用软归档：

- 默认的“所有活动项目”视图会隐藏归档项目、归档任务、归档集群目标及其消息。
- 勾选“显示已归档”后可以重新查看，按钮会切换为“恢复当前项目”或
  “恢复当前任务”；归档的集群目标则显示“恢复目标”。
- 归档不会删除数据库记录、任务事件、worktree、分支或合并提交。
- 有未结束的集群目标、Running 任务或活动 Agent 进程时禁止归档。
- 项目归档后的下一 `teamd` tick 会同步归档其持久 Agent；恢复项目时同步恢复
  Agent 记录，避免归档项目继续出现在 AgentPool 选择范围中。
- 已归档任务不能 claim；已归档项目不能创建任务、cluster goal 或 worktree，
  恢复后解除限制。
- 只有 `completed`/`failed` 集群目标可以在没有活动 job 时归档；归档目标不能
  直接改变状态，恢复后才能继续生命周期操作。
- 归档目标会同时软归档该目标下尚未归档的 `done` 任务。恢复目标时只恢复由目标
  级联归档的任务，用户单独归档的任务保持归档。
- 目标归档不会归档 Agent Thread、Codex 会话、任务事件、worktree、分支或合并
  记录。归档任务可通过 `team_list_tasks(include_archived=true)` 读取；目标详情
  和 `team_cluster_status` 始终包含关联任务，供 Agent 按需查询。

## MCP 工具

`codexTeamBoard` MCP 支持 `CODEX_TEAM_MCP_ACCESS=full|read_only|off`。
`read_only` 仅暴露查询工具，`off` 不暴露任何工具。设置
`CODEX_TEAM_ALLOWED_ROOTS`（Windows 使用 `;` 分隔）后，所有带 `repo_root`/`repo`
参数的调用必须位于允许根目录内。基准程序固定使用 `off`。

默认工具面是缓存稳定的 `core-v1`，只暴露两个 schema 永不变化的工具：

- `team_read(operation, arguments, request_id?)`
- `team_write(operation, arguments, request_id?)`

初始化后调用一次 `team_read(operation="help")` 可取得操作目录；新增 operation
不会改变工具 schema。help 会返回每个 operation 的完整 `inputSchema`，包括类型、
默认值、范围、枚举和字段说明。Core dispatcher 和 legacy surface 都会在执行
业务前校验同一份 schema，不接受隐式布尔/整数转换。
`CODEX_TEAM_MCP_SURFACE=core|admin|legacy` 控制工具面，其中 `core` 是默认值，
`admin`/`legacy` 暴露下列兼容细粒度工具，建议仅用于人工管理、迁移和故障回退。

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
- `team_cluster_start`
- `team_cluster_ensure`
- `team_cluster_status`
- `team_cluster_control`
- `team_goal_set_workstream`
- `team_goal_set_checkpoint`
- `team_goal_migrate_workflow`
- `team_agent_register_native`
- `team_agent_list`
- `team_agent_reset`

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
- 已完成 Agent 不会被隐式调用为 follow-up；是否复用持久 thread 由任务
  `continuity_mode`、角色和 workstream 规则决定。`team_worker` 的 write 任务在
  `auto` 模式下始终新建 thread，只有显式 `reuse` 才会跨任务复用。
- 任务 ready 后 60 秒未 claim 显示黄色提醒，5 分钟显示红色异常。
- 跨模块共享字段必须声明名称、单位、合法区间、枚举、更新者和消费方，并验证
  真实生产者到真实消费者的一体化路径。
- 团队角色模板在初始化时同步到项目；managed 文件固定使用 UTF-8/LF，避免
  Windows checkout 因 CRLF 产生整文件伪 diff。

## 自动 Agent 集群

非平凡开发任务优先使用幂等的 `cluster ensure`，确保每个项目只有一个未完成
目标：

```powershell
.\scripts\team.ps1 cluster ensure `
  --repo "D:\path\to\repo" `
  --objective "实现并验证目标，自动拆分、执行、测试和验收" `
  --autonomy auto
```

该命令会复用现有的 `planning`、`active` 或 `paused` 目标。返回值通过
`created`、`reused` 和 `requires_resume` 区分结果；遇到 paused 目标只返回提示，
不会自动恢复。需要明确创建新目标时使用 `cluster start`：

```powershell
.\scripts\team.ps1 cluster start `
  --repo "D:\path\to\repo" `
  --objective "实现并验证目标，自动拆分、执行、测试和验收" `
  --autonomy auto
```

`cluster start` 只用于显式新建目标；同一项目已有 `planning`/`active` 目标时会
返回 `project already has active goal`。

### Agent runtime、workstream 与缓存复用

调度器默认使用 `app_server` 持久 Agent thread。未设置
`CODEX_TEAM_AGENT_RUNTIME` 或设置为空时都会启用 thread 复用；需要完全关闭
持久 thread、改用独立进程时，显式设置为 `exec`：

```powershell
$env:CODEX_TEAM_AGENT_RUNTIME = "exec"
$env:CODEX_TEAM_MODEL = ""            # 可选；空值使用 Codex 默认模型
$env:CODEX_TEAM_MODEL_PROVIDER = ""   # 可选
```

默认的 `app_server` 模式由 `ClusterManager` 持有私有 App Server 进程。新 thread 使用
`thread/start`，已存储且满足复用规则的 thread 使用 `thread/resume`，每次派发
通过 `turn/start` 提交当前 goal/task。稳定 `developerInstructions` 以
`Managed Agent Role Contract` 开头，角色约束在前，随后才是稳定 Thread Scope；
Thread Scope 包含项目 ID、项目根目录和 workstream，不含 goal ID、task ID、
时间戳或日志路径。动态 board 和任务内容只放在后续 turn 中。`turn/start` 的
`cwd` 始终是当前执行 worktree，因此同一 thread 可以从一个 write worktree
安全切换到另一个 worktree，sandbox policy 按当前任务重算。

App Server 的 `tokenUsage` 同时保留线程累计 `total` 和本轮 `last`。job 的
input、cached、uncached、output 优先记录本 job 的 final `total` 与该 Agent
thread 既有 baseline 的差值；只有 `total` 没有 token 证据时才回退 `last`。
`usage_missing_fields` 保存字段级缺失信息，Dashboard 将未知字段展示为 null，
不会把缺失 cached/output 伪造成 0。成功但 baseline 不完整的 thread 会被
标记为 `failed/needs_reset`，不会进入复用池污染下一 job 的累计差值。

`cluster_jobs` 是统一的 attempt ledger，记录 `correlation_id`、prompt hash、
cache profile、`thread_action`、outcome 和 settled time。所有 job 终态走同一
settlement helper；completed、failed、interrupted、lost 都会写入 outcome 和
settled_at。Dashboard 的 Agent 卡片通过 latest-job 和 attempt ledger 投影失败或
中断任务的真实 usage，不再依赖成功 release 后的累计字段。

数据库初始化遇到 `database is locked/busy` 时进行指数退避重试；只读连接使用
编码后的 `file:` URI，路径中的空格、`#` 等字符不会被误解析。日志脱敏同时覆盖
JSON、`=` 赋值、命令行和未加引号的冒号格式凭据。

`teamd` 的每次 tick 固定按以下阶段执行：

```text
settle → recover → control → prepare → dispatch → health → publish
```

`clusterd.json` 的 tick 结果包含 `phase_order`、`phases`、`failed_phase`、
`skipped_phases` 和总 `ok`。每个阶段带 `duration_ms` 与 `counts`；业务阶段失败
会停止后续阶段，但仍执行 publish，让看板和 daemon 状态能定位失败位置。同一
`ClusterManager` 内的并发 tick 使用锁串行化。

当 Goal 的所有任务都已 done/canceled 且没有活动 job 时，`planning` Goal 会先
进入 `active`，再进入 `completed`，用两个明确事件完成 terminal reconciliation。

`workstream_key` 是项目内连续性作用域；省略时默认使用当前 `goal_id`。默认复用
策略为：

- Lead：同项目、同角色、同 workstream 且连续时自动恢复 thread。
- Explorer 的 read 任务、Reviewer 的 review 任务：同项目、同角色、同
  workstream 且 cache profile 一致时自动复用。
- Worker：`auto` 默认 fresh。
- 任意角色：显式 `continuity_mode=reuse` 才允许复用；超过上下文阈值仍会换新。

`CODEX_TEAM_AGENT_REUSE_CONTEXT_RATIO` 默认 `0.85`。当 Agent 的
`context_tokens / model_context_window` 达到该比例时，不再复用旧 thread。
如果 context 或 model window 任一项未知，旧 thread 默认不复用；新注册但尚未
执行过 turn 的 Agent 仍按空上下文处理。
Agent cache profile 会同时绑定实际执行目录的 AGENTS 策略、Core 工具 schema、
surface、access mode、Prompt Contract manifest、MCP instructions、sandbox
mode、运行时身份和最终稳定 developer instructions；任一变化都会阻止旧 thread
复用。`admin`/`legacy` 兼容工具面不会参与 Agent 自动复用，始终使用新 thread，
避免管理工具面变化污染缓存身份。

Dashboard 会为 managed Thread 派生 `profile_contract_status`：只有包含当前
Prompt Contract 版本和 manifest hash 的 profile 标记为 `current`；`legacy`、
`v0.5` 以及其他旧/部分 profile 标记为 `historical · not reusable`。该字段只由
现有 profile 派生，不新增第二套 Thread 状态，也不删除历史 Thread。
`continuity_reason` 说明相关 goal 为什么可以延续同一 Lead 上下文。创建或恢复
目标时可以直接提供：

```powershell
.\scripts\team.ps1 cluster ensure `
  --repo "D:\path\to\repo" `
  --objective "继续实现同一功能并验证缓存收益" `
  --workstream-key "feature-cache" `
  --continuity-reason "同一功能的多阶段实现" `
  --no-daemon

.\scripts\team.ps1 cluster set-workstream <goal-id> `
  --workstream-key "feature-cache" `
  --continuity-reason "同一功能的多阶段实现"
```

任务默认 `continuity_mode=auto`。Worker 在 `auto` 下 fresh，Lead 和符合条件
的 Explorer/Reviewer 可以自动复用；`fresh` 强制新 thread，`reuse` 显式允许复用。
创建任务时可覆盖：

```powershell
.\scripts\team.ps1 task create `
  --repo "D:\path\to\repo" `
  --title "后续实现" `
  --description "复用同一功能的上下文" `
  --acceptance "测试通过" `
  --kind write `
  --owner-role team_worker `
  --continuity-mode reuse
```

查看和重置持久 Agent：

```powershell
.\scripts\team.ps1 agent list --project-id <project-id>
.\scripts\team.ps1 agent reset <agent-id> --reason "手动重置上下文"
```

`teamd` 每次 tick 会应用 reset 请求，对 App Server thread 调用
`thread/unsubscribe`，再把旧 Agent 标记为 `superseded`。MCP 对应工具为
`team_agent_list`、`team_agent_reset` 和 `team_goal_set_workstream`。
CLI、MCP、ClusterManager 和 WebUI 的 reset 都统一进入
`AgentPool.request_reset`；`starting`/`busy`、`superseded` 和已归档 Agent
会被拒绝，避免重置正在执行或已失效的 thread。

清理不可复用的 managed Thread 使用：

```powershell
.\scripts\team.ps1 agent archive-stale `
  --actor human `
  --reason "clean historical failed Agent threads"
```

命令默认只返回 dry-run 候选，不写数据库；确认后追加 `--apply`。清理范围是
`failed`、`superseded` 以及 cache profile contract 非 `current` 的 managed
Thread。Native Thread、current reusable Thread、`busy/starting` 和仍绑定活动
Goal/Task/Job 的记录不会归档。归档是可恢复软删除：默认诊断隐藏，include-archived
仍可查询，Agent events、cluster jobs 和 Codex session 文件全部保留。

### Native 子 Agent 登记

Codex 原生子 Agent 不会进入 managed AgentPool 的自动复用池。创建 thread 后，
Lead 必须在任务正文实际送达后登记：

```powershell
.\scripts\team.ps1 agent register-native `
  --repo "D:\path\to\repo" `
  --thread-id "<native-thread-id>" `
  --role team_explorer `
  --workstream-key "native-investigation" `
  --task-id "<task-id>" `
  --actor "<native-thread-actor>"
```

等价的 Core operation 是：

```text
team_write(
  operation="team_agent_register_native",
  arguments={...}
)
```

登记会读取 Codex session JSONL，记录 parent thread、agent path、nickname、
task prompt hash 和最近更新时间。只有用户输入消息中出现该 task ID，状态才是
`prompt_status=delivered`；assistant 文本、日志或工具输出中的同名字符串不算
送达。`missing` 会阻止 claim，必须补发任务并重新登记。

native Thread 为 `same_task_only`：同一 thread/actor 只能对应同一 task，不能由
managed runtime reset，也不会被 `continuity_mode=auto|reuse` 选中。每日 Codex
session 根目录不是默认 `~/.codex/sessions` 时，通过
`CODEX_NATIVE_SESSIONS_ROOT` 指定。

Dashboard 还会低频自动发现最近 24 小时的 Codex subagent session。只处理
`source.subagent.thread_spawn`、且 `cwd` 位于已登记活动项目内的 session；
主 Thread、未知项目、归档项目和非 subagent 文件不会入库。发现过程只读取
`session_meta` 和任务 ID，不读取或展示对话正文。首次登记仍会写入
`cluster_agents`，之后按 thread ID 幂等跳过，不会重复生成 Agent。

WebUI 在首次轮询、每 30 秒和展开“Agent Threads 诊断”时调用：

```text
POST /api/agents/sync-native
{"limit": 500}
```

自动发现适合展示和监控；需要精确绑定角色、task prompt 送达状态或
`same_task_only` 认领门禁时，仍应执行显式 `team_agent_register_native`。
历史 managed `failed` Thread 不会被自动隐藏或删除，诊断面板继续完整保留。

Native session 的 token 数据来自 `subagent_history_start_ordinal` 之后的
`token_usage_record` 和 `token_count`。Dashboard 显示最近一次 input/cached/
uncached/context 和累计 cache rate；session 尚无 usage 事件时显示 `—`，不会把
数据库默认 `0` 误报成真实用量。显式记录的零值仍保持为 `0`。
已经在 `cluster_agents` 登记的 native Thread 即使超过自动发现时间窗，也仍会
检查其 session 并刷新 usage。

### Workstream checkpoint

阶段边界使用 compact checkpoint 代替复制完整历史：

```powershell
.\scripts\team.ps1 cluster checkpoint <goal-id> `
  --summary-file .\workstream-checkpoint.md
```

checkpoint 应包含确认决策、已完成工作、变更文件、测试结果、已否决方案、风险
和下一步，不包含原始日志或完整 transcript。Lead 通过
`team_goal_set_checkpoint` 自动维护；fresh Worker 会收到同 workstream 最近一次
checkpoint 的截断摘要。

Lead 的每轮 Board 只包含状态计数、活动任务、最近 8 个完成摘要和省略数量；
完整任务与事件仅在需要时通过 Team Board MCP 读取。

App Server 初始化失败时会重启 runtime 重试一次；仍失败则记录
`agent_runtime_fallback`，清理该 job 的 thread/Agent 关联，并让同一 job 使用
exec。runtime 初始化完成后，单个 `thread/start` 或 `turn/start` RPC 失败不会
重启健康的共享进程，只允许当前 job 做一次 fresh-thread 或 exec fallback，
其它活动 turn 保持不变。只有 transport、健康检查或进程级故障才触发 restart；
restart 前后都会显式把其它活动 job 收敛为 `lost`，避免旧进程的 turn 在恢复后
残留。`thread/start`、`turn/start` 和 `thread/resume` 调用前后还会比较 runtime
generation；若 `_ensure_started()` 已内部替换进程，即使调用随后抛错且新进程
恢复 healthy，也会在本次派发返回前隔离其它旧 generation job，并让当前 job
在新进程上按既有 fresh/resume 一次性边界继续。已选 thread 的
`thread/resume` 失败时，旧 Agent 会标记为 `needs_reset`，同一 job 只再创建
一次 fresh thread。`goal.lead_thread_id` 仅是兼容字段，不能绕过 AgentPool 对
项目、workstream、模型、provider、状态和 reset 的检查。

每个活动 App Server job/turn state 都持久化创建时的 `runtime_generation`。
reconcile 会与当前 runtime health 的 generation 比较；一旦进程被替换，旧
generation 的 job 在下一次 reconcile 即标记为 `lost`，不等待 job 超时，并继续
执行既有 Worker 重试或 Lead generation recovery。私有 runtime 的 stderr 写入
`logs_dir/app-server.stderr.log`，fallback、restart 和 job 错误附带脱敏后的最近
stderr，便于定位启动或传输故障。

超时或取消会优先对活动 turn 调用 `turn/interrupt`。daemon 重启后，数据库中的
stored Agent 仍可恢复；无法归属到仍存活 App Server 的活动 turn 会标记为
`lost`，其任务按既有重试策略恢复。`contextWindowExceeded` 会把旧 Agent 标记为
`needs_reset`，清理 thread 并自动用新 thread 重试一次；Lead 第二次仍溢出时
goal 进入 failed，Worker 第二次仍溢出时任务进入 blocked 并记录原因。

### A/B 缓存基准

基准仍以 fresh `exec` 作为对照、`app_server` thread reuse 作为复用变体；
当前生产默认已经使用 `app_server`，设置 `CODEX_TEAM_AGENT_RUNTIME=exec`
可以恢复对照路径。
`scripts/agent-cache-benchmark.py` 接受 fresh/reuse 的 JSON 或 JSONL 观测，
仅用成功任务计算 token 和延迟中位数，同时用全部样本计算成功率。主指标是
成功任务的 `last_uncached_input_tokens` 中位数；`last_*` 全部保留，
`total_*` 作为会话累计诊断，不参与过线判断。报告包含 `sample_count`、
`successful_sample_count` 和 `median` 字段，并输出 JSON 与 Markdown：

```powershell
python .\scripts\agent-cache-benchmark.py `
  --fresh .\bench\fresh.jsonl `
  --reuse .\bench\reuse.jsonl `
  --json .\bench\agent-cache-report.json `
  --markdown .\bench\agent-cache-report.md
```

每条记录可使用扁平字段，也可使用 App Server 的嵌套 `usage.total` /
`usage.last`。每条记录都必须提供 `task_success`，缺失时 benchmark 直接拒绝；
首轮延迟应为毫秒或秒。失败样本仍计入 `sample_count` 和成功率，但不会污染成功
成本中位数。成功样本缺少有效 `last_uncached_input_tokens` 时结果固定为 FAIL，
不会把缺失值当 0。手工构造旧格式记录时，`total_*` 可继续表示已换算的单轮累计
差值；诊断字段不能替代 `last_*` 主门槛。`cache_hit_rate` 也按成功样本的 last
单轮 cached/input 计算。

复用变体通过的门槛是：任务成功率不得下降，并且满足以下任一条件：

- 成功任务的 median `last_uncached_input_tokens` 相对 fresh 降低至少 10%；
- 成功任务的 median 首轮延迟相对 fresh 降低至少 20%。

8 组交叉顺序微探针显示，复用同一 `app_server` thread 的第二轮 cache hit
平均高约 `0.58pp`，8/8 配对为正。该收益仅限连续 thread 的后续 turn；
无关新任务和默认 write Worker 仍按 fresh 规则隔离。

### 多集群容量与公平调度

`teamd` 可以同时调度不同 Git 项目中的目标，但以下容量和隔离规则仍然生效：

- 每个 active goal 的 Worker 容量默认是 `3`；Lead 独立存在，不占 Worker 槽位。
  两个 active goal 默认拥有 6 个可并行 Worker 槽位。
- 另有一个默认 `16` 的系统进程安全阀，统计所有项目中 `starting` 或 `running`
  的 Lead 和 Worker。它是防止整机过载的外层保护，不是正常的 per-session 配额。
- 同一项目仍只允许一个 `planning` 或 `active` goal。需要并行多个集群时，应为
  不同项目分别创建目标。
- 调度器优先选择最久未启动任务的 goal，再比较该 goal 当前活动 job 数和 goal ID；
  goal 内则按任务 priority、创建时间选择 Worker。这是持续填充全局槽位的公平
  轮转，不保证每个 goal 获得固定槽位数。
- 系统安全阀已满且某个 goal 仍有可派发 Worker 时，会记录
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

Agent runtime 与模型配置：

| 环境变量 | 默认值 | 合法值/作用 |
| --- | --- | --- |
| `CODEX_TEAM_AGENT_RUNTIME` | `app_server` | `app_server` 启用持久 thread；显式设为 `exec` 可关闭复用 |
| `CODEX_TEAM_AGENT_REUSE_CONTEXT_RATIO` | `0.85` | 复用 thread 的 context/window 上限比例，范围 `0.1..1.0` |
| `CODEX_TEAM_AGENT_CODEX_HOME` | `<CODEX_TEAM_HOME>\agent-codex-home` | 托管 Agent 专用 Codex Home；同步 `config.toml`、`auth.json`、`models.json`，不复制用户 sessions |
| `CODEX_NATIVE_SESSIONS_ROOT` | `%USERPROFILE%\.codex\sessions` | Native Thread 登记的 Codex session JSONL 根目录 |
| `CODEX_TEAM_NATIVE_DISCOVERY_MAX_AGE_HOURS` | `24` | Dashboard 自动发现的 session 回溯窗口；已登记 Thread 不因超出窗口而删除 |
| `CODEX_TEAM_MODEL` | 空 | 可选模型覆盖；空值使用 Codex 默认模型 |
| `CODEX_TEAM_MODEL_PROVIDER` | 空 | 可选 provider 覆盖；必须与 AgentPool 身份匹配 |

以下调度变量由 `teamd` 和相关管理命令读取。四个调度/超时变量控制实际行为，
两个 heartbeat 变量是可选诊断阈值：

| 环境变量 | 默认值 | 输入范围 | 实际调度值/作用 |
| --- | ---: | ---: | --- |
| `CODEX_TEAM_CLUSTER_MAX_JOBS` | `16` | `1..86400` | 系统安全上限，统计所有项目 Lead+Worker 的活动 job；硬限制为最多 `32` |
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

$env:CODEX_TEAM_CLUSTER_MAX_JOBS = "24"
$env:CODEX_TEAM_CLUSTER_MAX_WORKERS = "2"
$env:CODEX_TEAM_WORKER_TIMEOUT_SECONDS = "7200"
$env:CODEX_TEAM_LEAD_TIMEOUT_SECONDS = "2700"
$env:CODEX_TEAM_HEARTBEAT_WARNING_SECONDS = "300"
$env:CODEX_TEAM_HEARTBEAT_CRITICAL_SECONDS = "900"
# 未设置时默认使用 app_server；如需回退：
# $env:CODEX_TEAM_AGENT_RUNTIME = "exec"

powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\ensure-cluster-daemon.ps1
.\scripts\team.ps1 cluster daemon-status
```

daemon 状态文件会记录 runtime、model/provider、Team Home、版本和配置指纹。
Team Board 子 Agent 使用独立 service `CODEX_HOME`，因此持久 thread、resume、
token 和 cache 统计仍保留，但不会写入日常 Codex 侧边栏。
修改运行配置后，使用以下命令安全重启：

```powershell
.\scripts\team.ps1 daemon restart --if-idle
```

已有的 exec JSONL usage 可离线回填。先 dry-run，再应用；缺失字段保持缺失：

```powershell
.\scripts\team.ps1 usage backfill --dry-run
.\scripts\team.ps1 usage backfill --apply
```

每个 active goal 的正常容量是 1 个 Lead 加 `CODEX_TEAM_CLUSTER_MAX_WORKERS`
个 Worker，默认即 1 个主 Agent 加 3 个子 Agent。多个 goal 的 Worker 槽位分别
计算，因此两个 active goal 默认有 6 个 Worker 槽位，而不会共享一个总数 4 的池。
`CODEX_TEAM_CLUSTER_MAX_JOBS` 只是全机进程安全阀，默认 16，不是正常 per-session
配额；只有达到该安全阀时才触发跨 goal 公平排队。输入 `999` 时，有效系统上限
为 `32`，每 goal Worker 上限为 `8`。

若 critical 阈值小于或等于 warning，调度器和看板都会把有效 critical 提升到
warning，因此 critical 始终不早于 warning。两项心跳变量均可省略。

WebUI 的 `global_capacity` 是 active goal 数乘以每 goal Worker 容量；Lead 不
占 Worker 槽位。`system_capacity` 才对应进程安全阀。修改配置后还应停止并重新
启动看板，使显示值与 `teamd` 的实际调度保持一致。

如果系统安全阀低于多个 goal 的实际进程总数，安全阀优先并启用公平轮转；未达到
安全阀时，各 goal 可在自己的 Worker 配额内独立推进。

若依赖任务已经 `canceled` 或归档，依赖它的任务会转为 `blocked`，记录
`dependency_invalidated`，并在同一轮调度中交给 Lead 处理，不会继续显示为
永久 `dependency_wait`。

`CODEX_TEAM_GOAL_STALL_TICKS` 控制 goal 停滞保护，默认 `6` 个 tick。planning/active
goal 在没有活动 job、没有有效外部 lease、行为签名连续不变达到阈值时，会自动转为
paused，写入 `goal_stalled` 和 `inspect_stalled_goal` 健康提示；人工恢复 goal 时
停滞计数会清零。

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
- `read`/`review` 任务由角色规则禁止修改源码，但使用 `workspace-write` 以运行
  需要临时目录的测试。
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
node scripts/dashboard-ui-smoke.mjs
node scripts/dashboard-edge-smoke.mjs
```

运行 `dashboard-edge-smoke.mjs` 前，需确保看板监听默认
`http://127.0.0.1:8765`；可用 `CODEX_TEAM_DASHBOARD_URL` 覆盖地址。脚本通过
`--remote-debugging-port=0` 使用唯一 CDP 端口，并将 Edge profile 放在项目已忽略
的 `artifacts/edge-smoke-profiles/edge-smoke-<PID>-<随机后缀>` 下，不依赖系统
`TEMP`。它验证 1120px 无横向溢出、Worker/Lead 指标、项目联动、归档入口、网络
状态和浏览器运行时错误，结束时只终止本次 Edge 进程树并清理本次 profile。

## 发布包

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\build-release.ps1
```

输出位于 `dist\codex-team-board-v0.5.0.zip`，同时生成 SHA256 文件。GitHub
Release 应附带 ZIP 与 `.sha256` 两个资产。

推送 `v*` tag 会触发 `.github/workflows/release.yml`：Actions 先校验版本和完整
测试，再构建便携包并通过仓库内置 `GITHUB_TOKEN` 创建 GitHub Release、上传
ZIP 与 SHA256。发布机器不需要保存 Release token；Git 推送仍使用 SSH。
