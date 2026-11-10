# Codex Team Board 完整流程拆解与交接点审计

> 审计快照：2026-11-10  
> 主仓库：`D:\AI开发项目文件夹\Codex团队协作环境`  
> 分支：`codex/context-memory-architecture`  
> HEAD：`016dfd710b7ccbd1df49b9301f92fbf28c5a9792`  
> 文档性质：可持续更新的流程地图、交接契约和只读审计记录  
> 当前工作区：审计完成时干净

本文档回答三个问题：

1. 系统中的每个步骤究竟在做什么。
2. 每一步把什么数据、状态和责任交给下一步。
3. 哪些交接点缺少原子性、身份绑定、数据完整性或可观测性。

本文以代码、测试和运行时数据库为主要证据。`README.md` 只作为产品契约参考；如果文档与代码不一致，应把差异本身登记为缺陷。

---

## 1. 快速结论

- 当前主链的核心收口是正确的：CLI、MCP 和 Dashboard 最终都进入 `TeamService`，任务主状态和 `task_events` 基本同事务写入。
- 默认 MCP Core 面已经稳定为 `team_read` / `team_write` 两个工具；全新 MCP 进程实测只暴露这两个工具。
- 当前主要风险不在“工具数量”，而在交接契约：
  - Review 没有证明 Worker commit 被 merge commit 包含。
  - MCP inputSchema 没有被运行时执行。
  - 多个状态迁移的 read-check-write 不在同一事务中。
  - 未知 context 被当成 0 并允许复用。
  - cache profile 没有覆盖 access mode、MCP instructions、Codex/App Server 版本等模型可见前缀。
  - native Agent 没有进入统一 Thread 注册、任务关联和看板监控。
  - failed/interrupted Agent 没有清算当前 job usage。
  - daemon/job 的进程所有权仍只依赖 PID。
- 本轮完整测试为 `136 tests OK, 1 skipped`；UI smoke、compileall 和 diff check 均通过。这说明上述问题主要是现有测试未覆盖的契约缺口，而不是当前测试直接失败。
- 当前正式库没有 starting/running job；有 10 个 failed Agent。正式库中这 10 个 failed Agent 的累计 usage 全为 0，但部分对应 job 有真实 token。
- 当前未检测到 `127.0.0.1:8765` 监听；因此当前页面看不到线程和缓存数据，除了代码缺陷外，还存在 Dashboard 未启动这一环境因素。

---

## 2. 组件地图

| 组件 | 入口 | 主要职责 | 持久状态 |
| --- | --- | --- | --- |
| 安装器 | `scripts/install.ps1` | 安装角色、注册 MCP、写全局配置、安装 watchdog/计划任务、启动看板 | 用户环境变量、Codex config、AGENTS、注册表、计划任务 |
| CLI | `src/codex_team/cli.py:main` | 人工管理任务、worktree、goal、Agent、usage、daemon | SQLite |
| MCP | `src/codex_team/mcp.py:main` | Agent 访问 Team Board 的稳定工具面 | SQLite、daemon |
| Dashboard/API | `src/codex_team/dashboard.py` | 看板、控制 API、健康检查 | SQLite、daemon 状态文件 |
| WebUI | `src/codex_team/web/app.js` | 轮询、渲染、操作按钮 | 浏览器内存 |
| 领域服务 | `src/codex_team/service.py:TeamService` | 项目、任务、状态机、事件、归档、Review | SQLite |
| Worktree | `src/codex_team/worktrees.py:WorktreeManager` | 创建 write task 的隔离分支和工作区 | Git worktree/branch + SQLite 字段 |
| 调度器 | `src/codex_team/cluster.py:ClusterManager` | goal、job、容量、公平性、恢复、Agent runtime | SQLite + 进程 + 日志 |
| Daemon | `src/codex_team/clusterd.py` | 周期调用 `ClusterManager.tick()` | lock/state/log |
| Agent 池 | `src/codex_team/agent_pool.py:AgentPool` | Thread 复用、busy/release/fail/reset | `cluster_agents` |
| App Server | `src/codex_team/app_server.py` | 私有 `codex app-server --listen stdio://` 连接 | Thread/turn + 内存事件 |
| exec runtime | `ClusterManager._start_exec_job` | 无持久 Thread 的独立 Codex 进程 | job JSONL + 输出文件 |
| 数据库 | `src/codex_team/db.py` | schema、迁移、连接 | SQLite |
| Usage | `src/codex_team/usage.py` | token 归一化、缺失语义、cache hit rate | job/agent 字段 |
| Cache profile | `src/codex_team/cache_contract.py` | Core schema 和 Agent 复用身份 | `cluster_agents.profile` |
| Agent Codex Home | `src/codex_team/agent_home.py` | 同步受管 Agent 的 config/auth/models | `<TeamHome>/agent-codex-home` |
| 脱敏 | `src/codex_team/redaction.py` | 清理 token/secret 文本 | 日志、事件、错误 |

---

## 3. 顶层运行时关系

```text
                    ┌──────────────────────────┐
                    │ install.ps1 / bootstrap  │
                    └────────────┬─────────────┘
                                 │ writes config/AGENTS/roles
                                 ▼
 Codex session ──MCP stdio──> codexTeamBoard ──> TeamService ──> SQLite
      │                              │                 │
      │                              ├──> WorktreeManager
      │                              └──> ClusterManager
      │                                        │
      │                         ┌──────────────┴──────────────┐
      │                         │                             │
      ▼                         ▼                             ▼
 native subagent          AppServerRuntime               exec process
 session JSONL             private stdio                 JSONL log
      │                         │                             │
      └──────── manual CLI/MCP ─┴──────── job/task events ────┘
                                 │
                                 ▼
                        dashboard_snapshot
                                 │
                                 ▼
                           /api/board
                                 │
                                 ▼
                              app.js
```

关键区别：

- **managed App Server Agent** 由 Team Board 创建、注册、调度、复用和监控。
- **native Agent** 由 Codex 原生协作工具创建，目前只存在于 Codex session JSONL；Team Board 只能在它主动调用 CLI/MCP 后看到任务事件。
- **exec runtime** 不使用持久 Agent Thread；每个 job 是独立进程。

---

## 4. 数据模型与状态机

### 4.1 数据库表职责

| 表 | 作用 | 主要消费者 |
| --- | --- | --- |
| `projects` | 注册 Git 仓库、软归档 | TeamService、Dashboard |
| `tasks` | 任务状态、claim、worktree、commit、review、归档 | TeamService、ClusterManager |
| `task_dependencies` | 任务 DAG | TeamService、ClusterManager |
| `task_events` | 任务审计事件 | Dashboard、Lead、Reviewer |
| `messages` | 团队消息 | Dashboard、CLI、MCP |
| `cluster_goals` | 集群目标、自治模式、Lead 状态、checkpoint、停滞检测 | ClusterManager |
| `cluster_jobs` | 每次 Lead/Worker 执行尝试 | ClusterManager、Dashboard、usage |
| `cluster_agents` | managed Thread 身份、状态、累计 usage、cache profile | AgentPool、Dashboard |
| `cluster_agent_events` | Agent 选择、busy、释放、失败、reset 事件 | Dashboard、审计 |
| `cluster_events` | goal 级调度、恢复、取消、心跳、停滞事件 | ClusterManager、Lead |
| `metadata` | schema 和一次性迁移标记 | db.py |
| `schema_migrations` | 不可回退的迁移账本 | `/api/health`、db.py |

### 4.2 Task 状态机

```text
create
  │
  ▼
ready ──claim──> running ──submit──> review ──approve──> done
  │                 │                   │
  │                 ├──block──> blocked ─┘
  │                 │             │
  │                 │         unblock
  │                 │             │
  │                 │             ▼
  │                 │           ready
  │                 │
  │                 └──cancel（实现禁止 running，必须先 submit/block）
  │
  ├──cancel──> canceled
  └──archive（standalone/non-active 条件）──> archived
```

实现细节：

- `create_task()` 固定创建 `ready`。
- `claim_task()` 允许 `ready` 和旧 `backlog`，但写任务必须有 worktree。
- `block_task()` 允许非终态任务。
- `unblock_task()` 一律回到 `ready`，未完成依赖会继续在 claim 时阻止。
- `submit_task()` 只能从 `running` 到 `review`。
- Review reject 回到 `ready`；approve 回到 `done`。
- Running task 不能直接 cancel；取消 goal 时由 ClusterManager 将其改为 blocked。
- 归档是 `archived_at` 软删除，不是状态字段。

### 4.3 Goal 状态机

```text
planning ──> active ──> completed
    │           │
    │           ├──> paused ──> active
    │           │       │
    │           │       └──> failed
    │           └──> failed
    └──> paused / failed
```

终态为 `completed`/`failed`。只有终态且无活动 job 的 goal 可以归档。

### 4.4 Job 状态机

```text
starting ──> running ──> completed
    │           │
    │           ├──> failed
    │           ├──> interrupted
    │           └──> lost
    └──> failed / lost
```

Job 是“一次执行尝试”，不是任务本身。同一 task 可以有多条 job，attempt 从 1 递增。

### 4.5 Agent 状态机

```text
register ──> idle
                │
              select
                │
                ▼
              busy ──release(full usage)──> stored
                │
                ├──failure/context overflow──> failed
                └──reset/project archive────> superseded/archived
```

`loaded`/`idle`/`stored` 都可被 AgentPool 选择；`failed` 不再自动复用，需要 reset 后产生新 thread。

---

## 5. 安装与启动流程

### 5.1 `scripts/install.ps1`

执行顺序：

1. 定位 `pythoncore` 解释器。
2. 计算 `CODEX_TEAM_HOME`，写入用户环境变量。
3. 复制三个 Agent TOML 到 `%USERPROFILE%\.codex\agents`。
4. 复制三个 Markdown 角色到 `%USERPROFILE%\.codex\team-roles`。
5. `codex mcp remove codexTeamBoard`。
6. `codex mcp add codexTeamBoard`，配置 Team Home、PYTHONUTF8 和稳定 stdio server 入口。
7. 运行 `codex_team.bootstrap` 写 Codex config 和全局 AGENTS 托管块。
8. 安装 Windows 计划任务；失败时回退到用户启动 watchdog。
9. 可选初始化目标仓库。
10. 检查 Dashboard 健康；不可达则启动 Dashboard。
11. 可选安装 Context Meter 联动。

交接点：

- 新 Codex 会话只有在重新读取 config 后，才会获得新 MCP tool surface。
- 已存在会话不会热更新 MCP tools/list 或 initialize instructions。
- 安装器先删除 MCP 再重装，后续步骤失败时没有完整回滚。
- 当前 bootstrap 的全局 AGENTS 仍使用部分旧式工具名称，而默认 MCP Core 只暴露 `team_read`/`team_write`。

### 5.2 `bootstrap.install_global_integration`

动作：

1. 备份全局 `config.toml` 和 `AGENTS.md`。
2. 在 `[mcp_servers.codexTeamBoard]` 中更新 cwd、超时、approval、enabled。
3. 替换或追加 `CODEX_TEAM_BOARD_GLOBAL` 托管块。

风险：

- 全局 AGENTS、项目 AGENTS、角色 prompt 和 MCP instructions 是四个独立文本来源；目前没有统一的 model-visible-prefix digest。
- 修改全局 AGENTS 或 MCP instructions 不会自动使已有 managed Thread 的 profile 失配。

### 5.3 Codex session 启动

Codex 会组合：

- 全局 developer/system 约束；
- 全局和项目 AGENTS；
- skills/plugins；
- MCP initialize instructions；
- MCP tools schema；
- environment context；
- collaboration mode；
- 角色 prompt 或 developerInstructions。

对缓存最敏感的是这些内容的顺序和稳定性：

```text
system/developer
  → AGENTS
  → skills/plugins
  → MCP instructions/tools
  → 稳定角色 prompt
  → 动态 turn input
```

动态内容应尽量放到最后的 turn input；工具 schema、MCP instructions 和角色契约应保持固定。

---

## 6. 项目初始化与任务创建

### 6.1 注册项目

入口：

- `TeamService.register_project()`
- `TeamService.init_project()`

动作：

1. `git rev-parse --show-toplevel` 解析真实仓库根。
2. 用规范化绝对路径的 SHA256 前 12 位生成 `project_id`。
3. UPSERT `projects`。

幂等性：

- 同一路径重复注册不会创建重复项目。
- 已归档项目不会被注册动作自动恢复。

### 6.2 初始化团队项目

`TeamService.init_project()`：

1. 写 `.codex/agents/*.toml`。
2. 写 `.codex/team-roles/*.md`。
3. 替换 `AGENTS.md` 中的 `CODEX_TEAM_BOARD` 托管块。
4. 写 `.codex/team-project.json`。

副作用：

- 会修改仓库文件，必须由用户提交后才能创建干净 worktree。
- 这些文件会影响所有后续 Agent prompt 和 cache profile。
- `cluster ensure` 不会隐式重写这些文件；初始化必须显式执行。

### 6.3 创建任务

`TeamService.create_task()` 输入：

- repo；
- title/description/acceptance；
- kind；
- owner_role；
- priority；
- depends_on；
- goal_id；
- continuity_mode；
- actor。

事务内动作：

1. `BEGIN IMMEDIATE`。
2. 校验 goal 属于同一项目。
3. 校验依赖属于同一项目。
4. INSERT task，固定状态 `ready`。
5. INSERT dependencies。
6. INSERT `created` event。

当前缺口：

- 不校验 goal 是否 archived/completed/failed。
- 已完成 goal 下可以创建 ready task，但调度器不会拾取。
- owner_role 只校验非空，不校验是否是可调度角色。
- MCP schema 没有在运行时执行，因此空 description/tests、错误类型、非法枚举可能到达业务层。

---

## 7. Worktree 与写入任务流程

### 7.1 创建 worktree

入口：

- `team_write(operation="team_create_worktree")`
- `codex_team.cli worktree create`

`WorktreeManager.create()`：

1. 读取 task。
2. 要求 task kind 为 `write`。
3. 要求项目 active、任务未归档、任务还没有 worktree。
4. 核对 task 的 repo 与传入 repo 是同一个 Git root。
5. 主 checkout 必须位于命名分支。
6. 主 checkout 必须干净。
7. 解析 base commit。
8. 生成 `codex/team/<task-id>-<slug>`。
9. 执行 `git worktree add -b <branch> <target> <base>`。
10. 在一个 SQLite 事务中更新 task 并写 `worktree_created` event。

交接不变量：

- write task 没有现存 worktree 时不可 claim。
- worktree 必须对应任务自己的 branch。
- 目标分支固定为创建 worktree 时主 checkout 所在分支。

风险：

- `git worktree add` 和 DB 更新不是一个原子操作。
- DB 更新失败或进程崩溃会留下孤儿 branch/worktree。
- 并发 create 没有任务级互斥。
- `worktree_path` 不受 `CODEX_TEAM_ALLOWED_ROOTS` 检查。

### 7.2 Worker 写入与提交

Worker 流程：

1. claim task，获得 claim token。
2. 只在 task 的 worktree 内修改。
3. 运行测试。
4. commit 并确保 worktree 干净。
5. `submit_task`，提交 result、tests 和 commit SHA。

`submit_task()` 对 write task 校验：

- worktree 目录存在；
- `git status --porcelain` 为空；
- commit_sha 至少 7 位；
- commit_sha 与 worktree 当前 HEAD 匹配；
- 将完整 HEAD SHA 写入 task。

当前缺口：

- authorization 和 UPDATE 不是统一 `BEGIN IMMEDIATE` 临界区。
- 与 block/cancel/review 并发时，存在 read-check-write 竞态。
- result/tests 的必填和长度没有统一入口校验。

### 7.3 Lead 合并与 Review

`review_task()` 对 write task 当前检查：

1. actor 字符串是 lead/main/human。
2. task 当前状态为 `review`。
3. worktree 仍存在且干净。
4. merge_commit 能解析为 commit。
5. merge_commit 是创建 worktree 时记录的目标分支的祖先。
6. 更新 task 为 done/ready，写 `reviewed` event。

当前缺少的关键条件：

- 没有检查 task.commit_sha 是 merge_commit 的祖先；
- 没有检查 merge_commit 包含 Worker 的变更；
- 没有检查 task/项目已归档；
- actor 只是自报字符串；
- 没有以 task.version 做 compare-and-swap，两个 Review 可同时通过。

本轮已复现：

- 任意目标分支历史 commit 可作为 merge_commit；
- Worker commit 不在 merge_commit 中，Review 仍把任务置为 done。

建议硬门槛：

```text
worker_commit ∈ merge_commit
merge_commit ∈ target_branch
merge_commit 包含任务变更
task.version 在 Review 前后未变化
task/project 未归档
```

若允许 squash/rebase，应以 patch-id、tree 等价性或显式 squash proof 作为替代证据。

---

## 8. Claim、Lease、Heartbeat 与业务写入

### 8.1 Claim

`claim_task()`：

1. 校验 actor。
2. 生成 claim token 和 lease。
3. `BEGIN IMMEDIATE`。
4. 拒绝 archived task/project、终态、review、blocked。
5. 拒绝其他 actor 持有的有效 lease。
6. 拒绝未完成依赖。
7. write task 校验 worktree。
8. 更新 status/running、claimed_by、claim_token、lease。
9. 写 `claimed` event。

这是目前实现最完整的状态入口之一。

### 8.2 Heartbeat

`heartbeat()`：

- 必须提供正确 claim token；
- claimed_by 必须匹配；
- lease 不能过期；
- task 必须 running；
- 延长 lease，并写 heartbeat event。

### 8.3 Progress update

`update_task()`：

- 需要正确 claim token；
- task 必须 running；
- 可写 result/tests/commit_sha；
- 每次写 `progress` event。

当前 CLI 的 `task update` parser 缺少 `--commit`，但 main 分支读取 `args.commit`，会抛 `AttributeError`。

### 8.4 Block / Unblock

`block_task()`：

- 非终态任务可 blocked；
- 如果调用者提供 token，则校验 token/actor；
- 清空 claim_token/lease，但保留 claimed_by；
- 写 blocked event。

`unblock_task()`：

- 只接受 blocked；
- 改为 ready；
- 清空 blocker/claimed_by；
- 写 unblocked event。

风险：

- block 的 token 可选；没有 claim token 时，任何调用者可阻止任务。
- block 与 claim/submit 的读取和更新不在同一事务。

---

## 9. 取消、归档与历史过滤

### 9.1 Cancel task

- 当前 running task 不允许直接取消。
- 非 running、非终态任务可以 canceled。
- 清空 claim 和 lease，保留事件。
- 使用 `BEGIN IMMEDIATE`，但前面已有一次 get_task 读取。

### 9.2 Archive project/task

项目归档守卫：

- 不能有 planning/active/paused goal；
- 不能有 running task；
- 不能有 starting/running job。

任务归档守卫：

- 不能 running；
- 不能有活动 job；
- 不能属于未结束 goal。

当前问题：

- 守卫在一个只读事务，更新在另一个写事务。
- 归档、claim、dispatch 之间可形成 TOCTOU。
- 本轮复现过 `project archived=true` 与 `task running=true` 同时存在。

### 9.3 Goal archive

- 只允许 completed/failed goal；
- 不允许活动 job；
- 归档时软归档该 goal 下全部未归档任务；
- 用 `archived_by_goal` 区分目标级归档和用户单独归档；
- 恢复只恢复目标级归档任务。

注意：

- WebUI 当前仍写“归档目标（含Done）”，但代码实际会归档全部未归档任务。
- 目标归档不会归档 managed Agent Thread、Git worktree、branch、commit 或事件。

---

## 10. Cluster Goal 与调度流程

### 10.1 Create goal

`create_goal()`：

1. 注册项目。
2. 校验项目 active。
3. 生成 goal_id。
4. `BEGIN IMMEDIATE`。
5. 拒绝已有 planning/active goal。
6. 写 planning goal。
7. 写 goal_created event。

### 10.2 Ensure goal

`ensure_goal()`：

1. 查找 planning/active/paused unfinished goal。
2. 优先 active，其次 planning，再 paused。
3. 如果找到，复用并更新 workstream/continuity reason。
4. 如果没有，创建 planning goal。
5. paused goal 只返回 `requires_resume=true`，不自动恢复。

### 10.3 Daemon tick 顺序

`ClusterManager.tick()` 当前顺序：

1. 应用 Agent reset 请求。
2. 同步项目归档与 Agent 归档。
3. reconcile active jobs。
4. 检查 Worker heartbeat。
5. 读取 planning/active goals。
6. 对每个 goal 做 prepare：
   - 检查无效依赖；
   - 检查全部终态；
   - supervised review pause；
   - 恢复 lease 过期但无活动 job 的 running task。
7. 调度 goals。
8. 检查 goal 停滞。

这个顺序意味着：

- 先修复旧 job，再派发新 job；
- 先应用 reset/archive，再选择 Agent；
- 但 cancel/archive 自身不是完整原子状态机，仍可能被 tick 穿透。

### 10.4 Dispatch 决策

每个 goal：

1. 找 active workers。
2. 找 ready、依赖完成、未超过重试次数的任务。
3. 优先派 Worker。
4. 满足容量后，在“无任务、review 或 blocked”时派 Lead。
5. 全局 max_jobs 限制 Lead + Worker 总数。
6. goal max_workers 限制每 goal Worker。
7. 公平调度按最久未启动 goal 排序。

### 10.5 Worker dispatch

`_dispatch_worker()`：

1. 校验角色。
2. write task 自动创建 worktree。
3. 读取 role prompt。
4. 生成 actor：`auto-<role>-<task>-<job>`。
5. 为 task 预留 claim。
6. 读取 workstream checkpoint。
7. 组装 Assignment Contract 和动态 Current Assignment。
8. 选择 exec/App Server 启动 job。
9. 启动失败时释放未落 job 的 claim。

注意：

- 调度器先预 claim。
- Worker prompt 仍要求“第一步 claim”；同一 actor 会重新调用 claim 并获得新 token。
- 如果 prompt 没有正确送达，task 会保持 running 直到超时或恢复。

### 10.6 Lead dispatch

Lead turn input 包含：

- 当前 objective；
- compact board digest；
- active tasks；
- 最近 8 个 completed 摘要；
- checkpoint；
- turn rules。

Lead 不接收完整历史，只按需读取 task/events。

---

## 11. AgentPool 与缓存复用

### 11.1 Register

`register_thread()` 持久化：

- project_id；
- role；
- workstream；
- thread_id；
- model/provider；
- display name；
- profile；
- status；
- timestamps。

同一 thread_id 只能注册一次；元数据不同会拒绝。

### 11.2 Select

`select_agent()` 的匹配条件：

- project；
- role；
- workstream；
- profile；
- model；
- provider；
- status ∈ loaded/idle/stored；
- 未归档；
- 没有 reset 请求；
- context/window 条件。

策略：

- Lead 在同 workstream 连续任务时自动 reuse。
- Explorer read 自动 reuse。
- Reviewer review 自动 reuse。
- Worker write auto 默认 fresh。
- explicit reuse 强制允许复用，但仍受 profile/context 限制。

### 11.3 Context 门槛

目标：

```text
context_tokens / model_context_window < CODEX_TEAM_AGENT_REUSE_CONTEXT_RATIO
```

默认 ratio 为 `0.85`。

当前缺陷：

- `_normalize_usage()` 把缺失 context 变成 0；
- SQL 只在 window/context 已知时限制；
- context 未采集时 thread 仍可复用；
- context 在 turn 中增长后，下一次选择前才可能发现超阈值。

安全规则应为：

```text
context unknown => no automatic reuse by default
context known and below threshold => reuse
context near threshold => fresh
```

### 11.4 Cache profile

当前 profile 包含：

- `core-v1`；
- surface；
- tool schema hash；
- AGENTS file hash；
- stable developer instructions hash；
- sandbox mode。

当前未包含：

- MCP access mode；
- MCP initialize instructions hash；
- App Server/Codex 版本；
- codex executable；
- agent Codex Home config；
- model catalog；
- approval policy；
- native/managed runtime 类型；
- 全局 AGENTS/其他 model-visible prefix。

已复现 full 与 read_only 生成同一个 profile，但实际暴露的工具不同。

### 11.5 Release 与 fail

`release()`：

- 只在 usage telemetry 完整时由 Cluster 调用；
- 更新 last/total usage；
- 清空 current goal/task/job；
- status idle 或 stored；
- 写 released event。

`mark_failed()`：

- 清空 current goal/task/job；
- 写 last_error；
- 可选设置 reset_requested_at；
- 写 failed event；
- 不清算当前 job usage。

这导致 Dashboard 的 failed Agent 卡片显示 0/0，即使对应 job 有真实 token。

---

## 12. App Server Runtime 流程

### 12.1 启动

默认命令：

```text
codex app-server --listen stdio://
```

启动步骤：

1. 启动私有子进程。
2. 创建 stdout/stderr/event 线程。
3. 发送 initialize。
4. 发送 initialized notification。
5. health 标记 healthy。

### 12.2 Job 启动

`_start_app_server_job()`：

1. 计算 stable developerInstructions 和 cache profile。
2. 非 core surface 强制 fresh。
3. 选择可复用 Agent。
4. 有 Agent 时 resume_thread。
5. resume 失败时标记旧 Agent failed/needs_reset，创建 fresh thread。
6. 新 thread 注册到 AgentPool。
7. mark_busy，关联 goal/task/job。
8. `turn/start` 发送动态 turn input、cwd、sandbox、model。
9. 更新 job status/thread/turn/agent/generation。
10. 写 App Server JSONL 事件。

### 12.3 Turn 事件

callback 进入：

- `thread/status/changed`；
- `turn/started`；
- `turn/completed`；
- `thread/tokenUsage/updated`；
- `error`；
- `thread/closed`。

Cluster 在内存 `app_turns` 中保存：

- thread_id；
- turn_id；
- generation；
- status；
- first_usage；
- latest usage；
- error；
- will_retry；
- context_window_exceeded；
- interrupt_requested。

### 12.4 正常完成

1. turn completed callback 更新内存状态。
2. 下一次 reconcile 读取 completed。
3. `_finish_job()` 聚合 usage。
4. Worker 读取 task 状态，确认已经 review/done/blocked。
5. 更新 job completed/failed。
6. 成功且 usage 完整时 release Agent。
7. usage 不完整时 quarantined/failed。
8. unsubscribe thread。

### 12.5 失败、超时和取消

- Turn error 为 failed：job failed。
- Job timeout：调用 turn/interrupt，job failed。
- Goal cancel：interrupt，job interrupted，task blocked。
- App Server disconnect/restart：旧 generation job 标 lost。
- Worker lost：按 attempt 恢复 task 或 blocked。
- Lead lost：增加 lead_failures，必要时清 lead_thread_id。

### 12.6 Context overflow

1. callback 识别 contextWindowExceeded。
2. 标记 Agent failed/needs_reset。
3. interrupt/unsubscribe。
4. 记录 agent_context_reset。
5. 同一 scope 第一次允许 fresh retry。
6. Lead 第二次 overflow：goal failed。
7. Worker 第二次 overflow：task blocked。

---

## 13. exec Runtime 流程

`_start_exec_job()`：

1. 拼接 developer instructions 和 turn input。
2. 构造 `codex exec` 命令。
3. 注入 `CODEX_HOME`、goal/job/task/role/actor 环境变量。
4. 启动独立进程。
5. stdout/stderr 统一进入 job JSONL。
6. 实时脱敏。
7. reconcile 检查进程退出码。
8. 解析 JSONL 的 thread.started、turn.completed、turn.failed/error 和 usage。
9. 更新 job 和 task/goal 恢复状态。

exec 与 App Server 的区别：

- 无 AgentPool 持久 Thread。
- 无跨 job 自动复用。
- 更易恢复进程退出码。
- 仍依赖 PID 判断 daemon 重启后进程是否仍存活。

---

## 14. Reconciliation 与恢复流程

### 14.1 Job reconcile

- 读取全部 starting/running job。
- App Server job：按内存状态、generation、health、timeout 判断。
- exec job：按 Popen、exit code、PID、timeout 判断。
- 终态更新 job/usage。
- Worker 未 submit/block：失败恢复。
- Lead 失败：记录 failure，决定 reuse/fresh/fail。

### 14.2 Worker 恢复

`_handle_worker_failure()`：

1. 如果 task 已 review/done/blocked/canceled，不恢复。
2. 如果 running claim 属于其他 actor，标记 claim conflict/lost。
3. attempt 达到 max：blocked。
4. 否则清 claim，返回 ready。

### 14.3 Daemon 启动

`ensure_daemon()`：

1. 读取 lock 中 PID。
2. 只要 PID 存活就认为 daemon running。
3. 否则启动 `python -m codex_team.clusterd`。
4. 等待状态文件显示 running。

当前无 process identity 校验，PID 复用会误判；stop 时可能 kill 无关进程。

### 14.4 Heartbeat 与 stall

- Worker 心跳事件超过阈值写 warning/critical cluster event。
- 告警只提示，不直接 kill job。
- goal 连续多个 tick 无调度进展时自动 paused。
- resume 会重置 stall counter。

---

## 15. Dashboard/API/WebUI 流程

### 15.1 GET

- `/api/health`：版本、Team Home、数据库路径/ID、schema、daemon runtime。
- `/api/projects`：项目列表。
- `/api/board`：dashboard_snapshot。

`dashboard_snapshot()` 汇总：

- projects/tasks/messages/task events；
- cluster goals/jobs/agents/agent events；
- capacity、queue reason、heartbeat、attention、next action。

### 15.2 POST

- `/api/clusters`：创建 goal 并 ensure daemon。
- `/api/clusters/control`：pause/resume/cancel。
- `/api/clusters/archive`：archive/restore goal。
- `/api/agents/reset`：请求 reset。
- `/api/projects/archive`：项目归档。
- `/api/tasks/archive`：任务归档。

当前缺少：

- Host 校验；
- Origin 校验；
- Content-Type 校验；
- CSRF/本地写 token；
- 非 loopback 模式的认证。

### 15.3 Web UI

1. 获取 projects。
2. 每 2 秒请求 board。
3. 计算 board signature，内容不变则不重绘。
4. 渲染集群目标、任务泳道、Agent Threads、消息和详情。
5. 页面隐藏时停止轮询。
6. 错误时 2/4/8/16/30 秒退避。

当前显示语义风险：

- `null` 经 `Number(null)` 变成 0；
- 未知 cache 显示 0%；
- 未知 token 显示 0；
- 所有 App Server job 都显示 “thread reuse”，即使是 fresh thread；
- failed Agent 没有最新 job 关联，无法显示真实失败成本；
- 任务详情事件来自全局最新 500 条，而不是 task-specific API。

---

## 16. Native Agent 流程

当前 native 路径：

1. 主 Agent 调用 Codex 原生 `spawn_agent`。
2. Codex 创建 session JSONL。
3. session_meta 包含：
   - `thread_source=subagent`；
   - `parent_thread_id`；
   - `agent_path`；
   - `agent_nickname`；
   - `agent_role`。
4. 子 Agent 继承父会话 AGENTS、environment 和工具面。
5. 子 Agent 通过 CLI/MCP 手工 claim/submit。
6. task_events 只记录 actor 字符串，不记录 thread_id。
7. Dashboard 不查询 session JSONL，也不显示 native thread。

本轮实测：

- 5 个 native Reviewer session 都存在；
- 正式数据库中这 5 个 thread_id 的注册数是 0；
- `fork_turns=none` 的线程首轮只收到 AGENTS/environment，任务正文未出现；
- 后续重新发送消息后任务才进入会话；
- 任务 `T-20261109-B267BB` 的 claim/heartbeat/submit/review 事件没有结构化 thread_id。

结论：

- native thread 在 Codex 层面可以持久存在；
- 但 Team Board 不拥有它的 lease、状态、cache profile、task/job 关联；
- 不能可靠地把 native thread 称为 Team Board 的“复用 Agent”；
- 更安全的目标是统一注册和监控，而不是把 native thread 直接塞入 managed 自动复用池。

---

## 17. Cache/Prompt 前缀流程

### 17.1 Managed Thread

```text
Agent cache profile
  + thread/start developerInstructions
  + worktree/project AGENTS
  + MCP instructions + tool schema
  + model/provider
  + sandbox policy
  → thread creation
  → thread resume/fresh
  → turn input only carries dynamic goal/task/board data
```

### 17.2 Native Thread

```text
父会话完整 AGENTS
  + environment context
  + skills/plugins
  + collaboration instructions
  + MCP 工具集合
  + spawn prompt
  → native child session
```

原生 spawn 继承的 context 和父线程高度相关，其中包括父线程的 visualization root 等动态路径，因此每个父会话可能形成不同的首请求前缀。

### 17.3 任何 profile 更新都应使旧 Thread 失配

以下变化应改变 profile：

- MCP surface；
- MCP access mode；
- MCP initialize instructions；
- tool schema；
- 角色 instructions；
- 项目/工作区/全局 AGENTS；
- sandbox；
- Codex/App Server 版本；
- model/provider；
- agent Codex Home 配置；
- runtime=managed/native/exec；
- 影响模型可见 prompt 的其他稳定配置。

动态 goal/task/board/checkpoint 不应进入 thread 创建前缀。

---

## 18. 交接点不变量清单

| 编号 | 交接点 | 必须保持的不变量 | 当前风险 |
| --- | --- | --- | --- |
| H1 | 安装器 → Codex config/MCP | 新会话拿到同一套工具和 instructions | 旧会话不热更新；安装无完整回滚 |
| H2 | MCP → TeamService | schema、类型、枚举、required 先于业务执行 | 运行时未校验 |
| H3 | TeamService → SQLite | 状态更新和事件同事务 | archive/claim、submit/block 等仍有竞态 |
| H4 | task → worktree | branch/path/base/target 一一对应 | Git 与 DB 非原子 |
| H5 | worktree → merge → review | worker commit 必须进入 merge commit | 当前未校验 |
| H6 | goal → scheduler | 每个 goal 只有一个有效 lead，job 不重复 | cancel/archive 与 tick 可竞争 |
| H7 | scheduler → claim | 预留 claim 与启动 job 可收敛 | prompt 送达/claim 竞争/失败释放 |
| H8 | AgentPool → App Server | profile/model/status/thread 身份一致 | profile 不完整；self-reported actor |
| H9 | App Server callbacks → job | thread/turn/job/generation 一一关联 | 迟发事件和重启边界风险 |
| H10 | job → task/goal | completed/failed/lost 必须驱动可解释恢复 | failed usage 未清算 |
| H11 | cache profile → reuse | 所有模型可见稳定输入一致 | unknown context、access mode、版本遗漏 |
| H12 | native thread → Team Board | thread_id、task_id、job_id、usage 可追溯 | 当前无统一注册 |
| H13 | SQLite → Dashboard | unknown ≠ zero，latest job 可追溯 | 0/0 和 0% 误导 |
| H14 | daemon/watchdog → process | 只管理自己拥有的进程 | PID 复用和 stale lock 竞态 |

---

## 19. 当前审计发现与流程映射

### P1

1. H5：Review 可批准不包含 Worker commit 的 merge。
2. H2：MCP 未执行 inputSchema，`"false"` 可变成 approve，非法 action 可 resume。
3. H3/H6：archive、cancel、submit/block、并发 Review 的状态迁移非原子。
4. H11：context 未知被当作 0，85% 门槛失效。
5. H8/H11：cache profile 未覆盖 access mode、MCP instructions、版本和全局 prompt。
6. H12：native Thread 未注册、未关联 task/job、未进入 Dashboard。
7. H10：failed/interrupted Agent 未清算 usage。
8. H14：daemon/job 仅按 PID 认定所有权。
9. H2/H13：Dashboard 写接口缺少来源边界；allowed roots 可被 worktree_path 绕过。
10. H1/H2：全局 AGENTS 和项目 AGENTS 仍有旧工具名，与 core tool surface 冲突。

### P2

1. CLI `task update` 缺少 `--commit` 参数。
2. completed/failed/archived goal 下仍可创建 ready task。
3. `git worktree add` 与 DB 更新非原子。
4. UI 把 null/unknown 显示成 0。
5. App Server job 全部显示 “thread reuse”，无法区分 fresh/resume。
6. usage backfill 把缺失 context 写成 0，并把 telemetry_complete 写成 1。
7. /api/board 全量返回任务和最多 500 条事件，详情缺少独立事件查询。
8. 安装器和 watchdog 不是完整事务，日志无轮转。
9. 脱敏规则不覆盖未加引号的冒号格式。
10. goal 归档按钮文案与“归档全部任务”的实际行为不一致。

---

## 20. 当前已验证的健康部分

- claim 使用 token 和 lease，并有并发单赢家测试。
- write task 未创建 worktree 时不能 claim。
- submit 会检查 worktree 干净且 commit 等于 HEAD。
- target branch 在 worktree 创建时固定。
- 完整测试 136 项通过，1 项真实 App Server 测试默认跳过。
- 默认 MCP Core 新进程只暴露 `team_read`/`team_write`。
- read_only 模式不能通过 Core dispatcher 调用 write operation。
- App Server 私有 stdio，不依赖桌面 App Server daemon。
- App Server generation 变化会隔离旧 job。
- context overflow 有 fresh retry 上限。
- task/agent/event 输出基本剥离 claim token。
- 后端 usage 的缺失字段投影为 null。

---

## 21. 验证命令

```powershell
Set-Location "D:\AI开发项目文件夹\Codex团队协作环境"

$env:PYTHONPATH = "src"
$env:PYTHONUTF8 = "1"

python -m unittest discover -s tests -v
node scripts/dashboard-ui-smoke.mjs
python -m compileall -q src tests
git diff --check
git status --short --branch
```

当前快照结果：

```text
Ran 136 tests ... OK (skipped=1)
dashboard-ui-smoke: ok
compileall ok
git diff --check: no output
git status: clean
```

只读运行态检查：

```powershell
Get-NetTCPConnection -LocalPort 8765 -ErrorAction SilentlyContinue
Invoke-RestMethod http://127.0.0.1:8765/api/health
.\scripts\team.ps1 cluster daemon-status
```

检查 native thread 是否进入 cluster_agents：

```sql
SELECT thread_id
FROM cluster_agents
WHERE thread_id = '<native-thread-id>';
```

检查 failed Agent 是否有未清算 job：

```sql
SELECT
  a.agent_id,
  a.thread_id,
  a.total_input_tokens,
  j.job_id,
  j.status,
  j.input_tokens,
  j.cached_input_tokens
FROM cluster_agents a
LEFT JOIN cluster_jobs j
  ON j.agent_id = a.agent_id
WHERE a.status = 'failed'
ORDER BY a.updated_at DESC, j.started_at DESC;
```

---

## 22. 推荐修复顺序

### 第一批：状态真实性

1. Review 增加 Worker commit ancestry/patch equivalence。
2. MCP 强制 operation inputSchema。
3. 禁止未知 action fallback。
4. archive/cancel/submit/review 使用原子状态迁移或 version CAS。
5. 禁止 terminal/archived goal 下创建可调度任务。

### 第二批：缓存安全

1. context unknown 默认不复用。
2. cache profile 自动计算完整 model-visible-prefix digest。
3. read_only 使用独立 instructions。
4. per-job 记录 `thread_action=fresh|resume|fallback_exec`。
5. surface/access/runtime/版本变化自动使旧 Thread 失配。

### 第三批：Agent Thread 统一

1. 新增统一 `agent_threads` 和 `agent_thread_events`。
2. 保留 `native` 和 `managed` 两个 adapter。
3. native thread 默认 `same_task_only`，不进入自动复用池。
4. task event 记录结构化 thread_id/job_id。
5. Dashboard 合并两类 thread，并区分 runtime。

### 第四批：Usage 与监控

1. failed/interrupted/lost job 统一 settle usage。
2. Agent 卡片关联 latest job。
3. unknown、真实 0、不可用三种状态分开显示。
4. task detail 使用独立 events API。

### 第五批：进程与安装

1. lock 记录 process identity。
2. 所有 taskkill 前验证进程身份。
3. worktree create/failed cleanup 可恢复。
4. install 分阶段幂等并可回滚。

---

## 23. 历史隔离分支说明

存在 blocked 的 `T-20261109-FE3227` 隔离 worktree：

```text
D:\AI开发项目文件夹\.codex-team-worktrees\Codex团队协作环境-t-20261109-fe3227
branch: codex/team/t-20261109-fe3227-p1-cli-mcp
HEAD: a4eb576
```

该分支包含 Review、CLI、归档原子性、数据库初始化、脱敏、Dashboard 来源校验等候选修复，但尚未合并到当前主分支。

本文不把该分支当作生产现状，也不在本文生成过程中恢复、修改或合并该任务。

---

## 24. 维护规则

每次重新审计时：

1. 先记录 branch、HEAD、schema version 和主工作区状态。
2. 先跑完整测试和 smoke，再检查运行态。
3. 按 H1-H14 逐项检查，不从“猜 bug”开始。
4. 每个问题必须写出：
   - 触发入口；
   - 交接点；
   - 不变量；
   - 复现命令或代码证据；
   - 实际结果；
   - 影响；
   - 已存在的修复分支。
5. 动态运行态数据另行取快照，不覆盖本文件中的历史审计结论。
6. 如果代码行为变化，保留旧结论并增加“已被哪次提交改变”。

---

## 25. 术语

- **managed thread**：Team Board 通过 `thread/start` 和 `thread/resume` 管理的 App Server Thread。
- **native thread**：Codex 原生 `spawn_agent` 创建的子会话。
- **job**：一次 Lead/Worker 执行尝试。
- **attempt**：同一 task/lead scope 的第几次 job。
- **workstream**：项目内 Agent 复用和 checkpoint 连续性作用域。
- **checkpoint**：面向下一轮 Agent 的持久决策摘要，不是完整 transcript。
- **profile**：决定 Agent thread 能否复用的稳定身份串。
- **fresh**：新建 thread 或新 exec 进程。
- **reuse**：resume 已有 managed thread。
- **fallback**：App Server 启动失败时，同一 job 回退 exec。
- **lost**：运行时重启或 generation 改变后无法继续归属的活动 job。
- **quarantine**：成功完成但 usage baseline 不完整，因此不允许继续复用的 thread。
