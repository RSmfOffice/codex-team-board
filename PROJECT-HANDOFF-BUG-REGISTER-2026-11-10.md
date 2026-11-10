# Codex Team Board v0.5.0 项目流程拆分与交接 Bug 风险登记册

> 文档基线：2026-11-10
> Git 提交：`3830f4b5ef2ee7989fcf61ee7632a49473553d5c`
> Release：`v0.5.0`
> schema：`20`
> workflow epoch：`workflow-v4`
> 测试基线：`187 tests OK, 1 skipped`

本文档是当前 `v0.5.0` 的流程地图和交接风险登记册。
[AUDIT-FLOW-2026-11-10.md](AUDIT-FLOW-2026-11-10.md) 保留为加固前历史审计；
本文档用于回答“现在代码怎么拆、每一步交给下一步什么、交接可能怎么坏”。

## 0. 使用方式

新会话排查问题时按以下顺序使用：

1. 先定位问题属于哪个真相边界：SQLite、Git、进程/Thread、模型可见前缀。
2. 用本文的 H 编号定位交接点。
3. 先查生产者写出的事实，再查消费者看到的投影。
4. 对照“必须保持的不变量”和“潜在 Bug”。
5. 用对应测试或命令复现，不要只凭页面状态推断上游状态。

状态标记：

- `已关闭`：旧缺陷已有实现和自动化测试，当前基线未复现。
- `受保护`：关键不变量已实现，但仍存在边界条件或人工操作风险。
- `残余风险`：不会阻止当前发布，但长期运行、升级或异常路径仍可能触发。
- `Phase 5`：已识别、尚未进入当前修复范围。

严重度：

- `P0`：可能静默批准错误代码、破坏数据或错误杀进程。
- `P1`：可能造成隐藏任务、错误复用、成本误报、错误归档或不可恢复状态。
- `P2`：可恢复但会造成误判、重复操作、性能退化或排障困难。
- `P3`：维护性、文档或低概率环境问题。

---

## 1. 快速结论

系统不是一个单一数据库应用，而是同时维护四个真相边界：

1. **SQLite 业务真相**：Task、Goal、Job、Agent、Event。
2. **Git 真相**：branch、worktree、commit、merge ancestry、clean/dirty。
3. **运行时真相**：daemon、子进程、App Server generation、Thread/Turn。
4. **模型可见前缀真相**：AGENTS、角色 Prompt、MCP instructions/schema、
   sandbox、runtime/model、workflow epoch。

大多数高价值 Bug 都发生在边界之间，而不是单个函数内部：

- SQLite 认为 Agent `busy`，但 App Server Thread 已丢失。
- Git merge commit 存在，但目标分支检查之后又发生变化。
- Task 已 `review`，但 Worker commit 不属于 merge。
- profile 看起来一致，但 MCP access mode 已变化。
- session JSONL 有 Native Thread，但 Registry 与 Dashboard 尚未同步。

当前最高优先级残余风险：

1. `H28/P0` daemon、job 和 stop 仍主要依赖 PID，PID 复用可能误判或误杀。
2. `H01/P1` 安装器先删除 MCP 再重装，后续步骤失败时没有完整回滚。
3. `H10/P1` Review 的 Git 检查与任务落板之间仍隔着外部 Git 可变窗口。
4. `H09/P1` submit/block/update 的部分 read-check-write 仍不是统一
   `BEGIN IMMEDIATE + version CAS`。
5. `H23/P1` Native 角色依赖 `agent_path` 启发式，Thread 生命周期状态不完整。
6. `H25/P1` revision 依赖秒级时间和聚合值，极端同秒变化可能漏刷新。
7. `H26/P2` tasks/goals/agents/attempts 分区 API 仍先组装完整 snapshot 再切片。
8. `H29/P2` 版本升级后旧进程可能继续加载旧 Python，出现前后端契约错位。
9. `H30/P2` 历史 worktree、branch 和日志没有完整自动清理/保留策略。
10. `H13/P2` 任一业务 tick phase 失败会跳过后续 phase，可能暂时停止调度。

---

## 2. 项目分层

| 平面 | 组件 | 拥有的状态 | 不应越界做什么 |
| --- | --- | --- | --- |
| 控制面 | CLI、MCP、Dashboard POST | 用户/Agent 意图 | 不直接改表，不复制状态机 |
| 领域面 | `TeamService` | Task/项目/事件/Review/归档 | 不启动 Agent，不合并 Git |
| 调度面 | `ClusterManager` | Goal/Job/容量/恢复/Tick | 不维护第二份 Task 主状态 |
| Agent 面 | `AgentPool` | Thread 身份/复用/profile/usage | 不决定 Task 是否完成 |
| 运行时面 | `AppServerRuntime`、exec | Thread/Turn/进程/回调 | 不直接批准 Review |
| Git 面 | `WorktreeManager`、Review | worktree/branch/commit/merge | 不替代 SQLite Task 状态 |
| 观测面 | Dashboard Read Model/WebUI | 投影和展示 | 不成为业务真相源 |
| 支持面 | 安装、配置、DB migration、redaction | 环境和兼容边界 | 不静默改变前缀或 schema |

### 2.1 运行时总图

```text
install.ps1
  → Codex config / global AGENTS / role files / MCP / watchdog
  → new Codex session loads MCP schema + instructions

CLI / MCP / Dashboard POST
  → TeamService
  → tasks / projects / task_events / archive / Review

cluster ensure/start
  → ClusterManager.create_goal
  → teamd.tick
  → prepare → dispatch
  → WorktreeManager
  → AgentPool
  → AppServerRuntime or exec
  → callbacks / JSONL
  → job settlement
  → task/goal recovery

SQLite
  → dashboard summary/partition/snapshot
  → revision + ETag
  → WebUI

Codex native sessions
  → native discovery
  → cluster_agents
  → usage refresh
  → Dashboard Agent Threads
```

### 2.2 四种“完成”不是同一件事

- Agent 进程退出 `0`：只表示 runtime 正常结束。
- Job `completed`：还要求 Worker 已经把 Task 推进到 review/done/blocked。
- Task `done`：还要求 Lead 提供通过 ancestry 检查的 merge commit。
- Goal `completed`：还要求全部 Task 终态且没有活动 Lead/Worker job。

任何一层把另一层的“完成”当成自己的完成，都可能产生假成功。

---

## 3. 全局身份与不变量

### 3.1 身份

| 身份 | 生产者 | 稳定作用域 |
| --- | --- | --- |
| `project_id` | `register_project` | 规范化 Git root |
| `task_id` | `create_task` | 任务生命周期 |
| `goal_id` | `create_goal` | 自动调度目标 |
| `job_id` | `_start_job` | 一次执行尝试 |
| `agent_id` | AgentPool | 持久 Thread 记录 |
| `thread_id` | App Server / Codex native | 模型会话 |
| `turn_id` | App Server | 单个 Thread 内一次 Turn |
| `correlation_id` | cluster/event | job 与跨层审计关联 |

### 3.2 必须始终成立

- 同一 Task 在有效 lease 内最多一个 owner。
- write Task 的 worktree、branch、target branch 和 Task 一一对应。
- Worker commit 必须包含在 Lead 记录的 merge commit 中。
- merge commit 必须可达创建 worktree 时固定的 target branch。
- Task/Goal/Project 归档后不能被 claim、dispatch、submit 或 approve。
- Goal 没有终态 Task 和活动 job 时才能完成。
- Job 进入终态必须做 settlement，不能停留在 starting/running。
- Agent 只有在完整 usage、兼容 profile、未超 context 时才能自动复用。
- Native Thread 只能绑定一个 Task，不进入 managed 自动复用池。
- Dashboard unknown 不得投影成真实 0。
- 所有外部副作用必须可恢复：worktree、daemon、Thread、日志。

---

## 4. 交接风险登记

### H01 Installer → Codex 文件系统/MCP

- 生产者：`scripts/install.ps1`。
- 消费者：新的 Codex session、MCP、Role Loader、watchdog。
- 交接物：用户环境变量、agent TOML、Markdown roles、MCP 注册、全局 AGENTS、
  计划任务/启动项、Dashboard 进程。
- 必须保持：安装完成后的所有消费者看到同一版本、同一 Team Home、同一工具面。
- 潜在 Bug：
  - 先 `mcp remove` 再 `mcp add`；add/bootstrap/watchdog 后续失败时 MCP 可能已丢失。
  - `SetEnvironmentVariable` 对当前已运行进程不生效。
  - 当前 Codex 会话不会热加载新的 MCP tool schema/instructions。
  - 已运行的 Dashboard 不会自动加载更新后的 Python 后端。
  - 计划任务失败后创建启动项；旧注册方式可能同时残留。
  - 多 Team Home 可能让 MCP、Dashboard 和 watchdog 指向不同数据库。
- 当前状态：`残余风险 / P1`。配置审计和身份指纹已有，但安装仍非完整事务。
- 验证：`test_bootstrap`、`test_agent_home`、`/api/health` 的 database_id 与
  team_home；安装后必须重启 Codex。

### H02 全局 AGENTS/配置 → Prompt Contract

- 生产者：bootstrap、项目 `init_project`、角色模板。
- 消费者：Agent cache profile、workflow epoch、模型首请求。
- 交接物：`AGENTS.md` 托管块、角色 Prompt、原生 TOML、MCP instructions。
- 必须保持：任何模型可见稳定文本变化都使旧 cohort/thread 失配。
- 潜在 Bug：
  - 只改全局 AGENTS，项目已生成的 `.codex/agents` 未同步。
  - 文件路径相同但内容修改发生在正在运行的 Agent 中途。
  - 多个文本来源冲突但 hash 各自独立，没有统一人工可读版本号。
  - 安装回滚恢复 config，但未同时恢复 AGENTS/roles。
- 当前状态：`受保护`。profile/workflow epoch 已覆盖 manifest、policy、role、
  MCP surface/instructions、runtime identity；仍有部署原子性风险。
- 验证：`tests/test_cache_contract.py`、`test_team_flow` 的 workflow epoch 测试。

### H03 MCP/CLI HTTP → TeamService

- 生产者：MCP dispatcher、CLI parser、Dashboard handler。
- 消费者：`TeamService` 和 `ClusterManager`。
- 交接物：operation 名称、inputSchema 校验后的参数、surface/access mode。
- 必须保持：协议层只适配，不绕过领域层；所有写入有统一校验。
- 潜在 Bug：
  - stale MCP 进程继续加载更新前代码。
  - actor 仍是自报字符串，无法证明本地调用者身份。
  - legacy surface 与 core surface 的参数/语义漂移。
  - Dashboard GET 和 POST 契约不同，前端旧页面调用新后端或反之。
- 当前状态：`受保护`。Core schema 固定，运行时 schema 校验和 read-only gate 已覆盖。
- 验证：`test_cache_contract`、`test_validation`、`test_cluster` MCP round trip。

### H04 项目注册/初始化 → workflow cohort

- 生产者：`register_project`、`init_project`、`current_workflow_epoch`。
- 消费者：Task 创建、Goal 创建、Agent dispatch。
- 交接物：`project_id`、Git root、项目策略文件、workflow epoch。
- 必须保持：同一 Git root 只有一个 project；策略文件已提交后再创建 worktree；
  Task/Goal/Job 使用同一兼容 cohort。
- 潜在 Bug：
  - Windows 路径大小写、符号链接或 worktree root 解析不一致。
  - `init_project` 修改文件但用户未提交，worktree 创建因 dirty 被阻塞。
  - 策略文件在任务中途更新，旧 Agent 与新 Agent 前缀不同。
  - 旧 workflow epoch 的 Ready Task 被阻塞但未显式迁移。
- 当前状态：`受保护`。新 Task fail closed，显式 migrate-workflow 有活动 job 门禁。
- 验证：`test_team_flow` workflow epoch、`test_cache_contract` profile 变化测试。

### H05 Task 创建 → Goal/Dependency

- 生产者：`create_task`。
- 消费者：Scheduler、Worker、Reviewer、Dashboard。
- 交接物：Task 行、dependency 行、created event、workflow epoch。
- 必须保持：Task/Goal/Project 同事务；依赖同项目；终态/归档 Goal 不接受新调度任务。
- 潜在 Bug：
  - `description`/`acceptance` 只 strip，没有强制非空。
  - `owner_role` 只检查非空，不是允许角色时直到 dispatch 才暴露。
  - 依赖目标归档/取消后下游需要额外 invalidated 流程。
  - 新 Task 创建成功但 Scheduler 因 cohort/goal 状态永不再拾取。
- 当前状态：`受保护`。Goal 状态/归档/workflow 已校验；字段语义仍可收紧。
- 验证：`test_team_flow` task creation、dependency、terminal goal guard。

### H06 Task → Worktree Intent → Git

- 生产者：`WorktreeManager.create`。
- 消费者：Worker、Claim、Submit、Review。
- 交接物：`external_operations` intent、branch、worktree path、base/target commit。
- 必须保持：Task 只能拥有一个 worktree；branch/path/base/target 不可被另一 Task 复用；
  崩溃后可判定 intent 是完成还是失败。
- 潜在 Bug：
  - Git 已创建 worktree，但 SQLite finalize 失败，进入 recovery。
  - branch 已存在但 worktree 不存在，需要人工清理。
  - CLI 显式 `worktree_path` 绕过 MCP allowed-roots 校验。
  - 并发 create 由 active intent 约束，但 recovery 与新 create 仍可能出现时序竞争。
- 当前状态：`受保护`。durable intent、唯一 active operation、tick recover/control 已覆盖。
- 验证：`test_team_flow` worktree recovery/dirty/cross-task tests。

### H07 Claim → Lease → Heartbeat

- 生产者：`claim_task`、`heartbeat`。
- 消费者：Worker、Lead、reconcile。
- 交接物：claimed_by、claim_token、lease_until、claimed/heartbeat events。
- 必须保持：有效 lease 内单 owner；token 不进入日志；过期 lease 可恢复但不能抢活进程。
- 潜在 Bug：
  - actor 重名或 native actor 绑定错误。
  - Scheduler 预 claim 与 Worker 自己 claim 出现 token 更新差异。
  - heartbeat 成功但实际 Worker 已卡死。
  - 系统时钟跳变导致 lease 提前/延后过期。
- 当前状态：`受保护`。并发 claim 单赢家、native same-task/prompt gate、failed job 恢复均有测试。
- 验证：`test_team_flow` concurrent claim、`test_cluster` lease recovery。

### H08 Block / Unblock / Cancel

- 生产者：CLI/MCP/Lead/Tick。
- 消费者：Scheduler、Review、归档、Dashboard attention。
- 交接物：Task status、blocker、claim cleanup、blocked/unblocked/canceled event。
- 必须保持：活动 Job 不能被直接 cancel；block 后 ownership/lease 必须清理；
  取消是终态且保留审计。
- 潜在 Bug：
  - `block_task` 无 token 时允许本地调用者阻断任意非终态任务。
  - read-check 与 UPDATE 没有统一显式事务，存在并发窗口。
  - blocked Task 依赖解除后不会自动变成 ready，必须 unblock 或 Lead 操作。
  - Goal cancel 与 active Worker 的 interrupt/settlement 是两阶段。
- 当前状态：`受保护/残余 P1`。归档/claim 已原子化；block 仍有 actor 信任和事务收紧空间。
- 验证：`test_team_flow` archive claims、block ownership；`test_cluster` cancel settlement。

### H09 Progress / Submit → Review

- 生产者：Worker、`update_task`、`submit_task`。
- 消费者：Lead、Reviewer、usage/recovery。
- 交接物：result/tests/commit_sha、review status、submitted event。
- 必须保持：只有 running 可 submit；write worktree clean；commit 等于 worktree HEAD；
  result/tests/commit 与实际证据一致。
- 潜在 Bug：
  - 测试文本和 commit 是 Worker 自报，未自动重跑测试。
  - worktree 检查与数据库 submit 之间存在 Git 变化窗口。
  - submit 与 block/review 并发时只靠各函数局部检查。
  - 空 result/tests 可通过业务层，仅依赖 MCP schema。
- 当前状态：`受保护/残余 P1`。clean/HEAD guard 已实现，版本 CAS 只在 Review 明确。
- 验证：`test_team_flow` write submit/review、CLI commit。

### H10 Merge → Review → Task `done`

- 生产者：Lead Review。
- 消费者：Task 终态、Goal reconciliation、release。
- 交接物：review_notes、worker commit、merge_commit、reviewed event。
- 必须保持：Task 仍 review；worktree clean；worker commit resolve；
  worker commit 是 merge commit 祖先；merge commit 可达固定 target branch；
  task version 未变化。
- 潜在 Bug：
  - Git ancestry 检查后、DB 落板前 target branch 被移动或 worktree 被改。
  - actor 只允许固定字符串，但不能证明操作者真的是当前 Lead。
  - squash/rebase merge 若不包含 worker commit，会被错误拒绝。
  - 未验证 merge 是否只包含授权任务变更。
- 当前状态：`受保护/残余 P1`。ancestry 与 version CAS 已关闭旧主漏洞；
  外部 Git 可变窗口仍需 repo-level lock 或 merge commit immutability 策略。
- 验证：`test_team_flow` unrelated merge、concurrent review、merge ancestry。

### H11 Goal create / ensure / migrate-workflow

- 生产者：`ClusterManager.create_goal`、`ensure_goal`、
  `migrate_goal_workflow_epoch`。
- 消费者：`_prepare_goal`、Scheduler、Lead/Worker dispatch、AgentPool。
- 交接物：`goal_id`、workstream、autonomy、workflow epoch、checkpoint、
  重试上限。
- 必须保持：同一项目最多一个 `planning/active` Goal；`paused` Goal 可被
  `ensure_goal` 发现，但不得被自动恢复；workflow 迁移必须同时覆盖 Goal 和
  所有未终态 Task，且迁移时没有活动 Job 或 running Task。
- 潜在 Bug：
  - 项目已有 paused Goal 时，`ensure_goal` 会复用该 Goal 而不是创建新
    objective；新调用方若不检查返回值，可能误以为新目标已启动。
  - workflow 迁移只要求“存在非空 checkpoint”，没有验证 checkpoint 是否仍
    对应当前决策；陈旧 checkpoint 可能被带入新架构。
  - 已 `done/canceled` 的 Task 保留旧 workflow epoch 是设计行为；若外部统计
    假定整个 Goal 所有 Task 同 epoch，会误报不一致。
  - Goal 唯一性受 SQLite `BEGIN IMMEDIATE` 保护，但多个 Team Home/DB 之间没有
    跨数据库互斥。
- 当前状态：`受保护`。活动 Goal 唯一、paused 不自动 resume、迁移活动 Job
  门禁和 checkpoint 门禁均已实现。
- 验证：`test_cluster` Goal ensure/resume、workflow migration、
  prompt layout；`test_cache_contract` workflow epoch 输入变化。

### H12 Tick settle → Job 终态

- 生产者：`_reconcile_jobs`、`_finish_job`、`_settle_job_row`。
- 消费者：Task recovery、AgentPool release/fail、Goal 完成判断、Dashboard
  attempt 历史。
- 交接物：`status/outcome/finished_at/settled_at/exit_code/error`、usage、
  thread/turn/generation。
- 必须保持：只有 `starting/running` Job 能被 settlement；重复 reconcile 不得
  重复累计 usage；Worker 只有 runtime 成功且 Task 已到
  `review/done/blocked` 才算 Job `completed`。
- 潜在 Bug：
  - daemon 重启后内存中的 `Popen` 对象丢失，只能凭持久化 PID 判断进程是否
    仍存活；PID 复用会延长假 running，见 H19/H28。
  - App Server state 缺失时 Job 会转 `lost`，但若 DB settlement 已提交、后续
    AgentPool 收敛抛错，当前实现会吞掉异常，可能留下终态 Job 与 busy Agent
    暂时不一致。
  - 数据库把缺失 usage 写为结构字段 0，必须依赖 `usage_source`、
    `telemetry_complete` 和 `usage_missing_fields` 还原 unknown；直接查数值列
    会误判真实命中率 0%。
- 当前状态：`受保护/残余 P2`。terminal CAS 保证结算幂等，unknown usage
  投影受保护；Agent release 异常后的补偿仍是残余问题。
- 验证：`test_cluster` settlement、lost turn、cancel usage、daemon restart；
  `test_usage_repair` missing-vs-zero。

### H13 Tick recover / control

- 生产者：`Tick.recover`、`Tick.control`、`WorktreeManager.recover_pending`、
  Agent reset/archive control。
- 消费者：`external_operations`、Task worktree 指针、Agent status/archive。
- 交接物：worktree intent 收敛结果、Agent reset/archive 结果、Phase 统计。
- 必须保持：可确定完成的 worktree intent 被补写；不可确定的目标标记 failed，
  不得猜测删除；reset 只在 idle Agent 上执行；项目归档与 Agent 归档最终一致。
- 潜在 Bug：
  - worktree “branch 已存在但目录不存在”会被标成 failed，没有自动清理，
    同 Task 再次创建仍会被旧 branch 阻塞。
  - 任一 business phase 抛错后，后续 phase 全部跳过，但 `publish` 仍执行；
    因此 settle 失败可暂时阻止 recover、dispatch 和 health。
  - Agent archive per-project 异常被吞并只计 failed，Dashboard 只能看到
    control 总数，排障需查 Agent events。
- 当前状态：`残余风险 P2`。worktree 不猜测删除是正确安全边界，但 phase
  failure isolation 和 failed intent 处理仍需优化。
- 验证：`test_team_flow` worktree recovery；`test_cluster` tick archive、
  reset、phase failure。

### H14 Tick prepare / dispatch

- 生产者：`_prepare_goal`、`_block_invalid_dependencies`、
  `_schedule_goals`、`_dispatch_next_job`。
- 消费者：Lead/Worker Job、Task block/unblock、Goal health。
- 交接物：可调度 Goal 列表、ready Task 选择、dependency 失效结论、
  claim reservation。
- 必须保持：终态依赖不能继续阻塞；supervised Goal 在 review 处暂停；全局和
  Goal 并发上限不可突破；同一 Task 不能有并行 attempt。
- 潜在 Bug：
  - `supervised` 只对 review 自动暂停；普通 blocked Task 是否需要人工处理由
    Lead 决定，不会像 review 一样自动停整个 Goal。
  - Worker dispatch 的任意非 claim 异常都会把 Task 直接 blocked；短暂
    Thread/runtime 故障可能被扩大成人工阻塞。
  - prepare 中某 Goal 的 prepare 异常会被 phase failure 放大，后续所有 Goal
    本轮都失去 dispatch 机会。
  - dependency 失效通过无 token 的 `block_task` 修正，存在与其他合法操作并发
    时的最后写入窗口。
- 当前状态：`受保护/残余 P2`。容量、supervised 和依赖规则已覆盖；错误隔离与
  自动阻塞粒度偏粗。
- 验证：`test_cluster` scheduling、capacity、supervised、dependency、
  tick phase order。

### H15 Scheduler → claim reservation

- 生产者：`_dispatch_worker(reserve_claim=True)`、`TeamService.claim_task`、
  `_release_worker_reservation`。
- 消费者：Worker 自身的首次 `team_claim_task`、Job start、failure recovery。
- 交接物：managed actor、claim token、lease、预占状态、启动失败释放。
- 必须保持：调度器先原子的占住 Task 再启动运行时；Worker actor 必须与 Job
  绑定；Job 无法启动时必须归还预占；已有 Task 人工 claim 时不得再启动 Job。
- 潜在 Bug：
  - reserve 后到 runtime 启动前是非瞬时外部区间，进程崩溃会留下 running
    Task；后续要靠 settle/lease recovery 收敛。
  - `_release_worker_reservation` 的 read-check-update 没有显式
    `BEGIN IMMEDIATE`，依赖连接事务行为，并发窗口仍应统一收紧。
  - 释放前会检查同 Task 是否已有 `starting/running` Job，但 Job start 和 Claim
    本来就分属两个提交点；崩溃位置决定恢复路径。
- 当前状态：`受保护/残余 P2`。预占冲突、启动失败释放和 actor 绑定已实现。
- 验证：`test_cluster` scheduler claim reservation/start failure；
  `test_team_flow` concurrent claim。

### H16 AgentPool cache select / release / fail

- 生产者：`AgentPool.select_agent`、`mark_busy`、`release`、`mark_failed`、
  `request_reset`、`archive_stale_managed_agents`。
- 消费者：App Server Thread start/resume、Dashboard Agent Threads、usage
  ledger。
- 交接物：`agent_id/thread_id/profile/status/last_usage/total_usage`、
  reset/archive 状态。
- 必须保持：复用必须精确匹配 project、role、workstream、profile、model、
  model-provider 和 runtime；只选择 idle、未归档、未请求 reset 的 managed
  Agent；上下文未知时除零 Turn 外不得自动复用。
- 当前复用范围：
  - Lead：仅当 Goal 声明明确 continuity 时自动复用。
  - Explorer：`read` Task 可自动复用。
  - Reviewer：`review` Task 可自动复用。
  - Worker：`auto` 默认 fresh；只有显式 `reuse` 才跨 Task 复用。
  - Native Thread：只登记和监控，固定 `same_task_only`，永不进入自动复用池。
- 潜在 Bug：
  - `CODEX_TEAM_AGENT_REUSE_CONTEXT_RATIO` 默认 0.85；usage 不完整、窗口未知
    或超过阈值会 fresh。若运营侧只追求命中率而静默提高阈值，会增加上下文
    截断和质量损失。
  - `release` 把本次 turn delta 累加到 Agent total；正常路径由 terminal
    settlement gate 防重复，但 release 异常被吞后没有后台对账。
  - Agent 进入 `failed/superseded` 后不会被 select；stale archive 默认
    dry-run，长期未执行会增加卡片噪声。
  - cache profile 任一模型可见输入变化都会让旧 Thread 变成不可复用，这是
    正确 fail closed，但会让升级后命中率短期下降。
- 当前状态：`受保护/残余 P2`。精确 profile、单赢家选择、85% 阈值和失败隔离
  已实现；usage/total 对账和 release 补偿仍需后台巡检。
- 验证：`test_agent_pool` profile/context/lease/archive/reset/native；
  `test_cache_contract` profile and workflow inputs；
  `scripts/agent-cache-benchmark.py`。

### H17 AppServer thread start / resume

- 生产者：`_start_app_server_job`、`AppServerRuntime.start_thread/resume_thread`。
- 消费者：AgentPool、Turn callbacks、Worker Task。
- 交接物：`thread_id`、`developerInstructions`、cwd、sandbox、model/provider、
  `thread_action`。
- 必须保持：Thread 的稳定前缀必须与 cache profile 一致；resume 失败不能继续
  使用未知身份；同一个 Agent 不能在 busy 时绑定第二个 Job；fallback 不得伪装成
  成功 resume。
- 潜在 Bug：
  - resume 成功只证明 Thread 被加载，不证明上次对话的业务状态适合当前 Task；
    当前通过 profile/workstream/busy 和 Worker 默认 fresh 控制风险。
  - resume 失败会 mark Agent failed/reset，再新建 fresh Thread；可能将一次性
    transport 故障转化为新前缀成本。
  - `goal.lead_thread_id` 是迁移/展示和 exec continuation 状态，故意不能绕过
    AgentPool 治理；旧逻辑若直接 resume 它会破坏 profile 和 reset gate。
- 当前状态：`受保护`。新 Thread 写 stable developer instructions，复用前先
  resume，失败隔离并 fresh。
- 验证：`test_cluster` thread resume/failure/fallback；
  `test_app_server` start/resume JSONL；`test_agent_pool` busy/reset。

### H18 AppServer Turn callbacks / generation

- 生产者：App Server stdout reader、`AppServerEvents` callbacks、
  `_call_app_server_rpc`、generation guard。
- 消费者：`app_turns`、Job settlement、usage、Task recovery。
- 交接物：thread、turn、status、usage、error、runtime generation。
- 必须保持：进程 generation 变化后，旧 generation 的 active turn 必须转
  `lost`；新请求只能发往 replacement process；同一 Turn 的 usage 和完成事件
  必须绑定正确 Job。
- 潜在 Bug：
  - callback dispatcher 串行执行，较慢的持久化 handler 会让通知积压；当前
    Job timeout 和 generation reconcile 是最终兜底。
  - `_handle_message` 只把 generation 用于协议失败/断连判断，通知 callback
    本身未携带 generation；极晚到达的旧事件依赖 generation reconcile 和
    thread/turn map 防错绑。
  - 精确 `turn_id` 缺失时会回退到 `thread_id` 映射；同一 Thread 连续 Turn
    若收到无 turn 标识的迟到通知，存在错误归属窗口。
- 当前状态：`受保护/残余 P2`。generation change 隔离和一次 context-overflow
  fresh retry 已实现；callback envelope 仍建议显式携带 generation。
- 验证：`test_app_server` event mapping/restart；
  `test_cluster` generation delta、old-job isolation、context overflow。

### H19 exec runtime → PID

- 生产者：`_start_exec_job`、`subprocess.Popen`、`_pump_log`。
- 消费者：`_reconcile_jobs`、timeout/cancel/daemon stop、step log parser。
- 交接物：PID、process object、exit code、stdout/stderr JSONL、thread_id、
  output 文件。
- 必须保持：只终止本项目创建的进程；stdout 必须持续抽取以防管道阻塞；敏感
  信息写入日志前必须脱敏；进程退出后 Job 必须结算。
- 潜在 Bug：
  - Windows 上只保存 PID，没有 Job Object、进程启动时间、command line 或
    instance token；PID 复用会误判存活并可能误杀无关进程。
  - daemon 重启后 process object 丢失，仅靠 `pid_is_running` 维持 Job；无法
    reattach stdout/exit code，只能等 PID 消失或 timeout。
  - `taskkill /T /F` 没有二次核验目标身份，属于 H28 的 P0 问题。
  - prompt 从 stdin 写入，若 Popen 成功后写 stdin/启动 pump 失败，必须依赖
    exception settlement；该路径已有失败结算，但要检查子进程是否被回收。
- 当前状态：`残余风险 P1`。log pump、进程组、timeout 和启动异常结算已实现；
  进程身份不可验证是高风险根因。
- 验证：`test_cluster` exec startup/timeout/log parsing；
  `test_usage_repair` exec JSONL usage；daemon/restart tests。

### H20 Job settlement → usage / outcome

- 生产者：`_finish_job`、`_reconcile_app_server_job`、
  `_finish_lost_app_server_job`、usage normalizer。
- 消费者：Dashboard attempt 历史、Agent reuse eligibility、成本报告。
- 交接物：`completed/failed/interrupted/lost`、outcome、first/final usage、
  usage source、missing fields、`settled_at`。
- 必须保持：runtime 成功不等于业务成功；Worker 必须留下
  review/done/blocked；缺失 telemetry 不得显示为真实 0；已有有效 usage 不得被
  后续空扫描覆盖。
- 潜在 Bug：
  - Worker 正常退出但忘了 submit/block，会被记为 failed 并进入 retry/block，
    这是正确行为，但只读返回文本不能替代 Board 状态。
  - `blocked` Worker Job 在 runtime 无错误时可记为 completed；业务上它是
    “安全停点”，不是 Task 成功。
  - App Server total usage 是 thread lifetime 快照，必须减去该 Agent 之前
    completed jobs 的 baseline；若历史 usage 被修复或重算，delta 依赖 ledger
    一致性。
  - interrupted/cancel 时会尽力从 JSONL 恢复 usage；完全没有事件时保持
    unknown。
- 当前状态：`受保护/残余 P2`。terminal CAS、usage evidence、first/final
  快照和 unknown 投影已覆盖；ledger backfill 后仍需核对 baseline。
- 验证：`test_cluster` app/exec settlement、cancel usage、lost usage；
  `test_usage_repair` normalize/backfill。

### H21 Worker failure / retry / recovery

- 生产者：`_handle_worker_failure`、`_recover_task`、
  `_finish_app_server_context_overflow`、`_mark_worker_claim_conflict`。
- 消费者：Scheduler 下一 attempt、Task blocked/ready、Goal health。
- 交接物：failure reason、attempt、expected actor、retry scope、block/recover
  event。
- 必须保持：只有原 claim owner 能被自动释放；attempt 不超过 Goal 上限；
  context overflow 只在同一 scope 自动 fresh 重试一次；claim conflict 不得抢走
  另一个有效 Worker。
- 潜在 Bug：
  - claim conflict 会把 Job 转 `lost`，但保留对方 running Task；如果对方不是
    本调度器创建的 Worker，Task 可能长期等人工处理。
  - `_recover_task` 的 read-check-update 没有统一 `BEGIN IMMEDIATE`，与
    submit/block 竞争时可能依赖 rowcount 兜底。
  - 重试计数是 Task 级而不是 Goal 级；多次不同类型的瞬时失败可能快速耗尽
    上限并自动 blocked。
  - Agent 被标 failed 后后续 attempt 一定 fresh，成本会上升但隔离更安全。
- 当前状态：`受保护/残余 P1`。attempt 上限、scope 重试和 actor 检查已实现；
  事务一致性与人工 owner 冲突 UX 需继续收紧。
- 验证：`test_cluster` retry/recovery/context overflow/claim conflict；
  `test_team_flow` lease expiration。

### H22 Task/Goal/Agent events → correlation

- 生产者：`TeamService._event`、`ClusterManager._event`、
  `AgentPool._event`、MCP dispatcher。
- 消费者：Dashboard recent events、task detail、故障复盘、Review 证据。
- 交接物：event type、actor、thread/job/correlation/assignment id、JSON data、
  timestamp。
- 必须保持：事件 append-only；managed event 应指向真实 Job/Turn；claim token
  不得进入事件或日志；用户可读错误必须脱敏。
- 潜在 Bug：
  - Task event 的 context 通过“该 Task + actor 的最新 Job”推导；managed actor
    包含 job_id，通常唯一，但外部 CLI actor 重名时可能关联到较新的 Job。
  - MCP schema 接收 `request_id`，当前 dispatcher 没有用它做 mutation
    idempotency key；客户端超时后重试 create/message 可能产生重复记录。
  - 三类 event 表没有跨表统一 event sequence，只能按各自 event_id 排序；
    Dashboard 的 recent list 不能仅凭 ID 判断全局先后。
  - 事件 JSON 解析失败会被展示为空 data，不会自动修复原始证据。
- 当前状态：`受保护/残余 P2`。managed correlation 和 redaction 已覆盖；幂等和
  跨表时间线仍可加强。
- 验证：`test_team_flow` event context/native actor；
  `test_cluster` attempt ledger/workspace events；redaction tests。

### H23 Native session discovery / registration

- 生产者：`discover_native_sessions`、`inspect_native_session`、
  `register_native_agent`、`sync_native_agents`。
- 消费者：Agent Threads 诊断、Dashboard、Team Task 绑定。
- 交接物：thread/parent/agent path/nickname/role、cwd、task IDs、prompt
  status/hash、session path、last seen。
- 必须保持：只发现 subagent session；Thread 必须属于未归档项目；同一 native
  Thread 只能绑定一个 Task；actor 不能同时绑定两个 Thread；Native Thread 永不
  进入 managed 自动复用。
- 潜在 Bug：
  - Role 优先读 session metadata，缺失时从 `agent_path` 字符串启发式推断；
    naming 变化可能把角色误判为 explorer。
  - Task binding 从 session 中出现的 Task ID 选择第一个仍存在且未归档的 ID；
    若 prompt 引用了多个 Task，可能绑定错任务。
  - `find_native_session` 对匹配文件按路径排序取最后一个，不保证就是该 Thread
    最新/唯一 session；异常残留文件可能导致解析旧内容。
  - 注册状态主要是 `loaded`，无法从 JSONL 精确区分 active/completed/aborted；
    Dashboard 的“已加载”不等于“正在运行”。
  - 每轮同步先 `rglob` 收集全部 jsonl，再按 mtime 截断；长期 session 增多后
    扫描成本和延迟上升。
- 当前状态：`受保护/残余 P1`。项目归属、单 Task、actor 冲突和 managed 隔离
  已实现；角色/Task 身份仍需显式元数据优先。
- 验证：`test_native_threads` discovery/metadata/usage；
  `test_cluster` sync-native；`test_agent_pool` native no-auto-select。

### H24 Native usage parsing / projection

- 生产者：native session JSONL parser、`_update_native_usage`、
  `_project_unknown_usage`。
- 消费者：Agent Threads 卡片、cache hit rate、上下文复用判断。
- 交接物：last/total input、cached/uncached、context/window、turn count、
  latest turn id、available。
- 必须保持：total 与 last 不混淆；cached 不高于 input；unknown 保持 None；
  Context usage 只使用可信的 last input；读取 session 不持久化用户消息正文。
- 潜在 Bug：
  - session 若曾可用后变为不可解析，`_update_native_usage` 在
    `available=false` 时只更新窗口，可能保留旧 token 数并显得过期但不标记。
  - 只有 total_usage、没有 turn id 时 `turn_count` 被估为 1；多 Turn 的
    Native Thread 会被低估。
  - Prompt hash 来自包含预期 Task ID 的用户消息；若 Task ID 只是被引用而非
    分派，仍会判 delivered。
  - JSONL 可能正在写入，单次读取没有文件锁；半行/半事件会被跳过，下一轮
    mtime 变化后再刷新。
- 当前状态：`受保护/残余 P2`。unknown-vs-zero 和 total/last 区分正确；
  session 生命周期和 stale usage 仍不完整。
- 验证：`test_native_threads` usage parsing、missing fields；
  `test_usage_repair` cache rate/unknown；Dashboard read model tests。

### H25 SQLite → revision / ETag

- 生产者：`_dashboard_revision`、`_contextual_revision`、`_send_revisable_json`。
- 消费者：`/api/summary`、`/api/board`、WebUI polling、API clients。
- 交接物：base revision digest、runtime context digest、weak ETag、
  `If-None-Match` 结果。
- 必须保持：任何会影响页面意义的 SQLite 变化都改变 revision；敏感/非语义的
  volatile age 字段不触发无意义重绘；304 只能表示同一个 API context。
- 潜在 Bug：
  - revision 使用表行数、秒级 `MAX(updated_at)`、`SUM(version/ticks/tokens)`
    等聚合；两个同秒更新且聚合恰好相同，可能漏刷新。
  - Goal 的 `last_error`、Task review notes 等字段若在 `updated_at` 同秒变化而
    version/aggregate 不变，签名可能相同。
  - revision 只覆盖 snapshot 使用的核心表；新增读模型字段必须同步加入签名，
    否则会成为“数据变了但页面不重绘”的长期回归点。
  - ETag 是 weak validator，且当前 WebUI 主要比较 `/api/summary` revision，
    并未主动发送 `If-None-Match`；ETag 优化主要服务外部客户端。
- 当前状态：`残余风险 P1`。常规更新和 runtime context 已验证；同秒漏刷新是
  设计级残余风险。
- 验证：`test_dashboard_read_model` revision/304/polling；
  `scripts/dashboard-ui-smoke.mjs` render signature。

### H26 Read API → WebUI

- 生产者：Dashboard GET handlers、`dashboard_summary`、
  `dashboard_snapshot`、`dashboard_partition`、`dashboard_events`。
- 消费者：`app.js`、外部 API 客户端、Edge smoke。
- 交接物：summary、board snapshot、partition page、cursor、revision、
  recent events。
- 必须保持：limit 有上限且 cursor 指向当前结果；默认过滤归档；404/400/409
  分类稳定；WebUI 隐藏标签页时停止轮询；渲染只消费 read model，不反写业务。
- 潜在 Bug：
  - tasks/goals/agents/attempts 分区接口先调用完整 `dashboard_snapshot`，再在
    Python 中切片；响应封顶但数据库读取和内存构造没有真正分页下沉。
  - partition cursor 只是当前列表中的某个 ID；列表顺序或成员在两页之间变化，
    仍可能重复/漏项，cursor 失效时返回 conflict。
  - board 是单体大快照；当前 WebUI 每轮先同步 Native、再取 summary，revision
    变化才取 board，减少了一部分渲染，但 summary 本身仍会做多条聚合查询。
  - 项目已归档但显式传 `project_id` 且 `include_archived=false` 时，任务查询
    未额外过滤 project archive；WebUI 会主动清除该选择，直接 API 调用仍可看到。
  - GET 默认监听 loopback，但 build server 支持自定义 host；若改成非 loopback，
    读接口没有认证。
- 当前状态：`受保护/残余 P2`。响应上限、cursor 校验、轮询退避、隐藏暂停和
  local write origin gate 已实现；分区查询未真正下沉。
- 验证：`test_dashboard_read_model` pagination/filter/revision；
  `test_validation` HTTP/CSRF/size；UI/Edge smoke。

### H27 Archive / history / error filtering

- 生产者：`set_task_archived`、`set_project_archived`、
  `set_goal_archived`、AgentPool archive、Dashboard filters。
- 消费者：默认看板、归档视图、Task/Goal detail、recent events、Agent Threads。
- 交接物：`archived_at`、`archived_by_goal`、archive/restore events、
  `include_archived` 投影。
- 必须保持：归档是可恢复软删除；归档 Goal 级联所有其 Task；恢复 Goal 只恢复
  自身级联、不覆盖人工单独归档；活跃 Goal/Task/Job/Agent 不能误归档；
  错误和用户控制原因不能污染“真实失败”判断。
- 潜在 Bug：
  - Goal archive 会级联归档所有 Task（包括 done），这是当前 UI 文案“含Done”
    的实现；不要误以为只归档 done。
  - project archive 要求项目无未完成 Goal/running Task/active Job，但不会把
    已完成 Goal 自动 archive；页面仍靠 project archive filter 隐藏。
  - Task archive/restore 的 actor 固定写 `human`，即使从 API 触发也无法追溯
    具体调用者。
  - `dashboard_events` 无 project 时排除 canceled Task，指定 project 时未排除；
    同一事件在不同筛选下可见性可能不一致。
  - 错误文本经过 redaction，但 `pause/resume/cancel requested through dashboard`
    等用户控制文案仍需前端归类，不能仅按包含 `failed/error` 的字面判断。
- 当前状态：`受保护/残余 P2`。级联归档、选择性恢复、Agent 来源区分和
  historical health 已实现；事件筛选一致性和 actor 可追溯性仍需加强。
- 验证：`test_team_flow` archive cascade/restore；
  `test_agent_pool` project vs stale archive；Dashboard smoke archive view。

### H28 daemon / watchdog / process ownership

- 生产者：`ensure_daemon`、`clusterd.acquire_lock`、Windows Scheduled Task、
  user startup watchdog、Dashboard 启动脚本。
- 消费者：Tick、App Server runtime、exec child、Dashboard、stop/restart/cancel。
- 交接物：`clusterd.lock` PID、`clusterd.json` state/identity、daemon log、
  watchdog registration、process PIDs。
- 必须保持：全机器同一 Team Home 只运行一个 daemon；stop/kill 只能作用于本
  项目目标进程；daemon identity/config mismatch 可见；watchdog 不创建重复实例。
- 潜在 Bug：
  - `P0`：daemon lock、Job reconcile、goal cancel 和 stop 都以裸 PID 判断
    身份。Windows 快速复用 PID 时可能把无关进程当作 daemon/child，进而
    `taskkill /T /F` 错杀。
  - daemon 重启后旧 Job 没有 process object，也没有 Windows Job Object handle；
    无法可靠证明持久化 PID 仍是同一进程。
  - `ensure_daemon` 发现任意 running PID 即返回，不自动处理 state identity
    mismatch；升级后可能继续运行旧 Python/旧配置。
  - watchdog 通过“最新 pythoncore”查找解释器；安装多版本 Python 后，下一次
    restart 可能切到不同解释器。
  - Scheduled Task 失败时退回 HKCU Run watchdog；注册状态分散在两处，排障需
    同时检查 task、Run key、named mutex 和 watchdog log。
  - Dashboard 没有独立 ownership lock；端口冲突/多实例时只能从日志和 health
    推断。
- 当前状态：`残余风险 P0`。这是当前最高优先级；在引入进程启动时间、命令行/
  instance token、Windows Job Object 或可验证 control channel 前，不能把裸 PID
  当作强身份。
- 验证：`test_cluster` daemon identity/restart/if-idle；
  `scripts/ensure-cluster-daemon.ps1`；Windows 实机需要 PID reuse 注入测试。

### H29 Upgrade / release / workflow epoch

- 生产者：`install.ps1`、`build-release.ps1`、版本常量、DB schema migration、
  prompt manifest、workflow epoch、cache profile。
- 消费者：Codex session、MCP、Role prompt、daemon、Dashboard、AgentPool。
- 交接物：package/schema/prompt/runtime 版本、release zip/checksum、workflow
  epoch、可复用 Agent profile。
- 必须保持：新代码只接收兼容 workflow cohort；升级后新 Agent 使用新 prompt；
  DB migration 幂等；release artifact 不含 DB/log/secret；旧进程不能继续写新
  契约。
- 潜在 Bug：
  - 安装器替换文件和 MCP 注册后不会统一停止/重启所有旧 daemon、Dashboard 和
    已运行 MCP；新会话可能连接旧进程，产生前后端契约错位。
  - 新代码会自动升级 SQLite schema，但旧 daemon 进程可能仍在内存中运行旧
    Python；没有 writer version lease 主动阻止。
  - release build 检查版本号，但不生成可复现时间戳/固定 Python runtime；
    同一版本重复构建的 checksum 可能不同。
  - workflow epoch mismatch 会 fail closed，但 `paused` Goal 不会自动恢复；
    升级后的运营步骤必须显式 checkpoint、migrate、resume。
  - 安装链的 MCP remove/add 无回滚，见 H01；release 成功不等于本机所有消费者
    已切换到新版本。
- 当前状态：`受保护/残余 P2`。schema/workflow/cache contract fail closed；
  升级编排仍缺少统一的 stop → install → health → new-session gate。
- 验证：`test_migrations` idempotence/schema；
  `test_cache_contract` versions/manifests；`build-release.ps1`；
  `/api/health` version/schema; new-session manual gate。

### H30 Worktree / branch / log / history cleanup

- 生产者：当前没有完整的自动清理器；只有 worktree create/recover/list、软归档
  和 stale Agent archive。
- 消费者：磁盘、Git worktree registry、agent usage 扫描、长期看板性能。
- 交接物：历史 worktree/branch、cluster job logs、app-server stderr、events、
  archived Task/Goal/Agent、failed external operations。
- 必须保持：清理必须可 dry-run、按明确状态和年龄选择；不删除未合并 commit 或
  dirty worktree；不删除审计事件；不凭路径猜 target；不自动执行 broad
  recursive delete。
- 潜在 Bug：
  - 已 done/canceled/archived 的 worktree 和 branch 没有保留期/清理策略，
    长期会积累磁盘和 `git worktree list` 噪声。
  - `logs/cluster/<goal>/<job>.jsonl` 与 `logs/app-server.stderr.log` 只做
    redaction，没有 rotation/retention；stderr memory deque 有上限，但文件没有。
  - failed `external_operations` 和 branch-exists/target-missing 状态没有自动
    修复入口，后续同 Task 创建会遇到旧 branch。
  - SQLite events/messages 只有响应 limit，没有 TTL/compact；长期 summary 聚合
    和 revision 可能变慢。
  - Native session JSONL 由 Codex 自己管理，本项目不清理；discovery 会持续扫描。
- 当前状态：`Phase 5 / 残余 P2`。当前做法是只软归档、不自动物理删除，安全但
  不可长期无界运行。
- 验证：当前无自动 cleanup 测试；未来至少需要 dry-run、未合并保护、路径边界、
  dirty worktree、branch retention 和 log rotation 测试。

---

## 5. 残余风险优先表

| ID | 严重度 | 当前状态 | 触发条件 | 主要影响 | 建议下一步 |
| --- | --- | --- | --- | --- | --- |
| H28 | P0 | 残余风险 | Windows 重启/崩溃后 PID 被复用 | 误判 daemon/Job 存活，可能误杀 | 引入 Job Object + 进程启动时间/instance token，stop 前强校验 |
| H19 | P1 | 残余风险 | daemon 重启后继续依赖持久化 PID | 无法 reattach，假 running 到 timeout | 与 H28 同批实现 process identity/control channel |
| H01 | P1 | 残余风险 | MCP remove 后后续安装在失败 | MCP 消失，需人工修复 | staging + 回滚，或原子切换 MCP 注册 |
| H10 | P1 | 受保护/残余 | Review ancestry 后 Git 被外部移动 | 可能批准不再对应 target 的 merge | repo lock 或 merge commit/target ref CAS |
| H09/H08 | P1 | 受保护/残余 | submit/block/update 与其他写并发 | read-check-write 竞争，状态覆盖 | 统一 `BEGIN IMMEDIATE` + version CAS |
| H21 | P1 | 受保护/残余 | retry/recover 与外部 claim 并发 | Task 暂时卡住或错误恢复 | 事务化 recovery，明确 foreign owner 处理 |
| H23 | P1 | 受保护/残余 | metadata 缺 role/multiple task IDs | Native Thread 角色/Task 误绑 | 显式 metadata 优先，存 parent+assignment identity |
| H25 | P1 | 残余风险 | 同秒更新且聚合签名不变 | Dashboard 不刷新 | 表级 monotonic revision 或 change counter |
| H13 | P2 | 残余风险 | 任一 business phase 异常 | 当轮后续调度停摆 | phase 独立错误隔离 + retry budget |
| H14/H15 | P2 | 受保护/残余 | runtime 瞬时失败/预占中断 | Task 被过粗 blocked 或短期 running | 区分 transient/permanent，事务化 reservation release |
| H16 | P2 | 受保护/残余 | release/usage 对账异常 | busy Agent 或累计成本偏差 | 后台 reconcile Agent vs Job ledger |
| H18 | P2 | 受保护/残余 | 慢 callback/旧通知迟到 | Turn 归属窗口 | callback envelope 携带 generation/job correlation |
| H22 | P2 | 受保护/残余 | 客户端重试 mutation | duplicate Task/Goal/message/events | 使用 `request_id` 建幂等键 |
| H26 | P2 | 受保护/残余 | 数据量增长/翻页期间状态变化 | 慢查询、重复或漏项 | SQL 分页下沉 + snapshot cursor |
| H27 | P2 | 受保护/残余 | 事件筛选/归档组合 | 可见性不一致、actor 不可追溯 | 统一 filter predicate 和真实 actor |
| H29 | P2 | 受保护/残余 | 安装后旧进程继续跑 | 前后端/缓存契约错位 | 升级 orchestrator 统一 stop/start/health/new session |
| H30 | P2 | Phase 5 | 长期运行 | 磁盘、worktree、logs、DB 无界增长 | 设计带 dry-run 和合并保护的 retention |

---

## 6. 排障 Playbook

### 6.1 SQLite 业务真相

1. 调 `/api/health`，核对 `team_home`、`database_id`、`schema_version`、
   `cluster_runtime.version`；先排除看板连错 DB 或旧进程。
2. 用 `team_status`/`team_list_tasks` 看 Task 主状态，再按 H 编号检查
   `cluster_jobs`、`cluster_agents` 和 event。
3. Task 卡住时同时核对：Task status、active Job、Agent status、lease、
   claimed_by、latest event。只看其中一项不足以定位。
4. usage 异常时看 `usage_source`、`telemetry_complete`、
   `usage_missing_fields`；不要把投影后的 0 当真实 usage。
5. 页面不刷新时比较 `/api/summary` revision 与实际行变化；若行已变但
   revision 不变，归到 H25，不要先怀疑浏览器。

### 6.2 Git / worktree 真相

1. 从 `team_get_task` 取 `worktree_path/branch/target_branch/base_commit`，
   不从目录名反推。
2. 依次检查 `git worktree list --porcelain`、worktree `status --porcelain`、
   `symbolic-ref --short HEAD`、`rev-parse HEAD`。
3. submit 失败优先查 dirty 和 commit SHA；Review 失败再查 worker commit
   resolve、merge resolve、target ancestry、worker-in-merge。
4. worktree intent 卡住时查 `external_operations` 和 tick phase
   `worktree_recovery`；不要手动删除 branch/目录，先证明没有未合并 commit。
5. H30 cleanup 上线前，任何 worktree/branch 清理都应人工 dry-run 并保留 commit
   证据。

### 6.3 Runtime / Thread / process 真相

1. `team_cluster_status` 看 Goal、Lead/Worker Job、daemon、retry、generation。
2. `clusterd.lock`/`clusterd.json` 的 PID 只能作为线索；在 H28 修复前，不把
   `pid_is_running=true` 当作同一进程证明。
3. App Server Job 同时看 `thread_id/turn_id/runtime_generation`、
   `app_turns` 日志事件、当前 runtime health generation。
4. generation 不一致必须归为 `lost` 并走 Task recovery，不得继续等旧 Turn。
5. 检查 watchdog 时同时看 Scheduled Task、HKCU Run key、
   `cluster-watchdog.log`、named mutex 和 `clusterd.log`。

### 6.4 Prefix / cache 真相

1. 先比较 Agent profile：surface、tools、policy、role、sandbox、access、
   instructions、manifest、contract、workflow、schema、runtime version/identity。
2. 再比较 Goal/Task workflow epoch；版本升级后旧 Goal 需要显式 checkpoint +
   migrate，paused Goal 不会自动恢复。
3. 确认 Agent 是否满足复用范围：managed、idle、同 project/role/workstream、
   model/provider 相同、未 reset/archive、上下文已知且不超过 85%。
4. Native Thread 只能作为监控记录；不能因为它“存在且空闲”就当 managed Agent
   复用。
5. 命中率下降先区分 fresh-by-contract 与错误重复建 Thread：
   `thread_action=fresh/resume/fallback_exec`、`cache_profile`、
   `prompt_hash`、first/final usage 和 `last_uncached_input_tokens`。
6. 高命中率不是正确性本身；必须同时满足 profile 一致、无 context overflow、
   Task 结果真实落板。

---

## 7. 测试覆盖映射

| 测试模块 | 主要覆盖交接点 | 重点证明 |
| --- | --- | --- |
| `test_bootstrap.py` | H01/H02 | AGENTS、角色、MCP 指令和托管区块 |
| `test_agent_home.py` | H01/H17 | Agent CODEX_HOME、配置隔离 |
| `test_validation.py` | H03/H06/H26 | schema、allowed roots、HTTP/write gate |
| `test_team_flow.py` | H04-H10/H30 | Task 生命周期、claim、worktree、Review、归档 |
| `test_cluster.py` | H11-H22/H25/H27-H29 | Goal/Tick/dispatch/retry/generation/settlement/cache profile |
| `test_agent_pool.py` | H07/H16/H17/H23/H27 | 精确复用、85% 阈值、busy/release/reset/archive/native |
| `test_app_server.py` | H17-H20 | JSONL RPC、turn、usage、restart/generation |
| `test_native_threads.py` | H23/H24 | discovery、metadata、prompt、usage parsing |
| `test_cache_contract.py` | H02/H16/H29 | Core schema、manifest、profile、workflow epoch |
| `test_dashboard_read_model.py` | H25-H27 | revision、ETag、snapshot、partition、filter |
| `test_dashboard_cluster_control.py` | H11/H26-H28 | Dashboard Goal/daemon control |
| `test_migrations.py` | H25/H29/H30 | schema migration/backfill 幂等 |
| `test_usage_repair.py` | H19/H20/H24/H28 | exec usage、unknown、backfill、MCP 边界 |
| `scripts/dashboard-ui-smoke.mjs` | H25-H27 | render signature、polling、archive UI |
| `scripts/dashboard-edge-smoke.mjs` | H26/H27 | 真实浏览器看板、归档和恢复路径 |
| `scripts/agent-cache-benchmark.py` | H16/H20/H24 | fresh/reuse usage 与 gate 判定 |

测试覆盖不代表交接点已无风险。尤其 H19/H21/H23/H25/H28 包含时间、PID、并发、
长期数据和 Windows 实机语义，单元测试只能覆盖可注入部分。

---

## 8. 每次修改后的验证门禁

在仓库根目录运行：

```powershell
$env:PYTHONPATH='src'
$env:PYTHONUTF8='1'
python -m unittest discover -s tests
node --check src/codex_team/web/app.js
node scripts/dashboard-ui-smoke.mjs
python -m compileall -q src tests
git diff --check
```

涉及真实浏览器、Dashboard 或归档恢复时再运行：

```powershell
node scripts/dashboard-edge-smoke.mjs
```

发布前额外核对：

```powershell
git status --short --branch
git rev-parse HEAD
git rev-parse main
git rev-parse origin/main
git tag --points-at HEAD
Invoke-RestMethod http://127.0.0.1:8765/api/health
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/team.ps1 cluster daemon-status
```

期望：工作区无未提交变更；版本、schema、workflow epoch、tag 一致；
`cluster_runtime` 不是旧配置；完整测试和 UI smoke 通过。

---

## 9. Phase 5 建议顺序

1. `P0` 进程身份：Windows Job Object、进程启动时间、instance token、可验证
   stop/control，关闭 H19/H28 误杀根因。
2. `P1` 写事务：submit/block/heartbeat/update/recovery/review 的外部检查与 DB
   CAS 统一成明确事务边界。
3. `P1` Review Git 窗口：repo-level lock 或 target ref/merge commit CAS。
4. `P1` Native identity：显式 role/task/parent metadata，生命周期状态与
   stale usage 刷新。
5. `P1` revision：引入表级 monotonic change counter，替换同秒聚合猜测。
6. `P2` tick 隔离：phase 独立失败、有限重试、错误事件可查询。
7. `P2` read model：SQL 分页下沉、稳定 cursor、减少 summary 聚合。
8. `P2` mutation idempotency：把 `request_id` 落成幂等键。
9. `P2` 升级编排：统一 stop → install → health → new session → resume gate。
10. `P2` retention：worktree/branch/log/event 的 dry-run、保留期和未合并保护。

长期优先级原则：先保证身份、事务和可恢复性，再优化查询和清理；缓存命中率
只能在稳定前缀、正确 profile 和真实 usage 证据成立时作为成本指标。

---

## 10. 文档维护规则

1. 本文以 release/tag、Git commit、schema、workflow epoch 和测试数为基线；
   每次发布更新头部基线。
2. 新缺陷必须分配新的 H 编号或扩展现有交接点，不重写旧审计历史。
3. 当前状态只使用 `已关闭`、`受保护`、`残余风险`、`Phase 5`；混合状态写成
   `受保护/残余 Pn`。
4. 风险结论必须附生产者、消费者、不变量、失败模式和验证入口；不能只写
   “可能有 Bug”。
5. 关闭风险时同时更新实现证据、自动化测试和本文状态；仅有监控或手动步骤
   不能标为 `已关闭`。
6. 每次测试数变化、schema migration、Core schema、AGENTS、角色 prompt、
   runtime/model、review ancestry 或 daemon ownership 变化，都必须复查相关 H。
7. 排障时先确认真相边界，再操作；不得用 Dashboard 投影代替 SQLite、Git 或
   runtime 证据。
