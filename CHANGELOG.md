# Changelog

本项目遵循语义化版本。

## [Unreleased]

### Added

- 默认 Team Board MCP 工具面切换为缓存稳定的 `core-v1`：
  `team_read`/`team_write` 使用固定 schema 和 operation dispatcher，完整
  细粒度工具保留在 `admin`/`legacy` 工具面。
- Agent cache profile 绑定 Core 工具 schema、项目 AGENTS 策略和稳定角色
  instructions；任一版本变化都会禁止旧 thread 复用。
- Core help 返回完整 operation `inputSchema`，保留类型、默认值、范围、枚举和
  字段说明。
- `cluster_jobs` 扩展为统一 attempt ledger，记录 correlation、prompt hash、
  cache profile、thread action、outcome 和 settled time。
- 新增 `external_operations` durable intent 表；worktree 创建可跨数据库/Git
  中断恢复，并发创建由唯一 active intent 约束。
- 新增版本化 Prompt Contract manifest；MCP、角色/项目指令、工具 schema、
  access mode、运行时身份和 policy 共同进入 Agent cache profile。
- 统一 `cluster_agents` Thread Registry，用 `runtime=managed|native` 区分托管
  复用线程与 Codex 原生子 Agent。
- 新增 `team_agent_register_native` Core operation 和
  `agent register-native` CLI；登记时检查 session parent/path/nickname、task
  prompt 是否由用户消息真实送达及 prompt hash。
- `task_events` 增加结构化 `thread_id`、`job_id`、`correlation_id` 和
  `assignment_id`，Dashboard/API 直接返回关联字段。
- 新增 `workflow_epoch` 稳定前缀 cohort：Goal、Task 和 cluster job 绑定同一
  架构语义；新任务只进入当前 cohort，旧 cohort fail closed。
- 新增显式 `cluster migrate-workflow` 和
  `team_goal_migrate_workflow`；有活动 job/running task 时禁止迁移，
  存在未完成任务时要求 durable checkpoint。
- `teamd` tick 改为固定
  `settle/recover/control/prepare/dispatch/health/publish` 流水线，输出阶段
  `duration_ms`、`counts`、失败阶段和跳过阶段。
- Dashboard 写接口新增 loopback Host、same-origin Origin 和
  `application/json` 校验；无 Origin 的 CLI 客户端保持兼容。
- Dashboard 读模型新增 `/api/summary`、`/api/tasks`、`/api/goals`、
  `/api/agents`、`/api/attempts`、`/api/events` 和
  `/api/tasks/<task-id>/events`；完整 `/api/board` 保持向后兼容。
- Read API 支持 `limit` 上限 500、列表 cursor、事件 `event_id` cursor、
  revision、ETag 和 `If-None-Match` 304。
- WebUI 常规轮询先读取 summary revision，未变化时不下载完整 board；任务详情
  改为按任务异步读取事件，不再依赖全局最新 500 条窗口。
- Dashboard 新增 `POST /api/agents/sync-native`，从最近 Codex subagent
  sessions 自动发现 native Thread，并按已登记活动项目的 cwd 归属幂等入库。
- WebUI 在初始轮询、每 30 秒和展开 Agent Threads 时自动同步；历史 managed
  failed Thread 继续保留，并增加明确的 failed 视觉状态。
- Native Agent 同步会读取 session 的 `token_usage_record` 与 `token_count`，
  持久化最近/累计 input、cached、uncached、context、model window 和 turn 数。
- 新增 `agent archive-stale`，可 dry-run/apply 归档 failed、superseded 与
  历史 cache profile 的 managed Thread；默认诊断不再显示，审计和 session 保留。

### Changed

- Explorer 的 read 任务和 Reviewer 的 review 任务可在同一 project/role/
  workstream/cache profile 下自动复用；Worker 的 auto 策略继续保持 fresh。
- Agent profile 现绑定实际执行目录的 worktree AGENTS、sandbox mode 和 MCP
  surface；`admin`/`legacy` 兼容工具面强制 fresh，不复用旧 thread。
- cache profile 进一步绑定 Prompt Contract manifest、MCP instructions、
  access mode 和运行时身份；升级或 instructions 变化不会误用旧 thread。
- 自动 AgentPool 只选择 `runtime=managed` 的可用 Thread；native Thread 仅支持
  same-task follow-up，不参与 managed reset 或自动复用。
- `cluster ensure` 不再隐式初始化或重写项目 AGENTS、角色模板和 marker；
  bootstrap/policy 写入必须显式执行。
- Worker、Explorer 和 Reviewer 的 Markdown/native TOML 模板全部改用
  `team_read`/`team_write` operation，避免默认 core 工具面与角色指令冲突。
- App Server usage delta 的 baseline 改为读取已完成 attempt ledger，不再依赖
  Agent 累计字段是否在成功 release 时更新。
- Goal resume 先确保 daemon 启动成功，再迁移到 active；daemon 失败保持 paused。
- Dashboard 运行动作现在区分 `fresh`、`resume`、`fresh_exec` 和
  `fallback_exec`；Token/cache 的 unknown、真实 0 与不可用语义继续分离。

### Fixed

- canceled 或已归档依赖不再把下游任务永久留在 `dependency_wait`；下游任务会转为
  blocked，并通过 `dependency_invalidated` 自动交给 Lead 处理。
- planning/active goal 在无活动 job 且行为签名长期不变时，由停滞看门狗自动暂停，
  避免依赖环或调度异常造成无声挂起。
- Core 和 legacy MCP surface 都在执行业务前校验 operation `inputSchema`；布尔、
  整数、枚举、required 和 additionalProperties 不再依赖下游隐式转换。
- Review 现在要求 submitted Worker commit 已包含在 merge_commit 中，并使用
  task version CAS，避免无关 merge 或多个并发 Review 把任务误置为 done。
- completed/failed/archived goal 不能继续创建可调度任务；归档 task/project
  不能继续 Review。
- context/model window 缺失的 managed Thread 不再自动复用；只有完整且低于
  `CODEX_TEAM_AGENT_REUSE_CONTEXT_RATIO` 的上下文允许复用。
- `CODEX_TEAM_ALLOWED_ROOTS` 同时约束显式 `worktree_path`。
- CLI `task update` 补齐 `--commit`，与 MCP `team_update_task` 保持一致。
- 全局和项目 AGENTS 全面使用 `team_read`/`team_write` operation 语法，避免默认
  Core 面要求不存在的细粒度工具。
- completed/failed/interrupted/lost job 统一经过 settlement，写入 outcome、
  settled_at 和可用 usage；Dashboard 对 failed Agent 优先投影 latest job。
- 前端保留 unknown 语义，`null` token/cache 显示为 `—`，不再变成 0/0%。
- native prompt 检测只接受真实 user message；assistant 输出、工具输出或日志中
  偶然出现 task ID 仍保持 `missing`，未知/缺失状态不能 claim。
- native Thread 可显示 runtime、prompt 状态、Agent 身份和父 Thread，避免把
  “已建线程但任务未送达”误判为正常 idle。
- managed Thread 卡片派生显示 `contract current` 或
  `contract historical · not reusable`，旧 profile 继续保留但默认明确标识。
- write task 在 claim、submit 和 approve 时验证 worktree 路径归属、branch
  归属、实际 checkout branch、主仓库隔离和 clean 状态；跨 task worktree 或
  错误 branch 会在业务状态迁移前被拒绝。
- 旧 `workflow_epoch` 的 ready Task 在 claim 前被阻断，旧 Goal 不自动 resume；
  Lead/Task dispatch 会记录 `workflow_epoch_mismatch` 并保持等待迁移。
- Dashboard 的 Goal、Task、Agent latest job 显示当前或未绑定 workflow epoch。
- `planning` Goal 在全部任务终态时显式经过 `active → completed`，不再尝试非法
  的 `planning → completed` 直接转换。
- 同一 `ClusterManager` 的并发 tick 串行化；业务阶段失败后停止后续阶段，但
  publish 仍会产出可诊断状态并脱敏错误。
- 项目/任务归档与 claim 使用同一 `BEGIN IMMEDIATE` 事务，消除归档竞争窗口。
- 数据库初始化对 locked/busy 做指数退避重试；只读连接使用编码 URI，兼容空格和
  `#` 路径。
- 脱敏补充未加引号的冒号格式凭据。
- Native 子 Agent 不再依赖 Lead 手工登记才能出现在 Dashboard；自动发现只读取
  session metadata 和任务 ID，主 Thread、未知项目和非 subagent session 会被跳过。
- Native Agent 尚无 usage 证据时，Dashboard 保持 unknown/`—`，不再把数据库默认
  token 列显示成真实 `0`；有 usage 后由下一次自动同步刷新。
- 已登记的 native Thread 不再受自动发现 24 小时窗口限制，窗口外的 session
  在时间戳或 usage 状态变化时仍会幂等刷新。
- Dashboard revision 覆盖任务版本、Goal/Agent 更新时间、job 状态与 usage、
  Task/Goal/Agent 事件序列和消息序列；事件分页使用严格半开 cursor，避免重复或
  漏项。
- 大任务库回归门禁验证分区响应受 `limit` 约束、summary 显著小于完整 board，
  并对 summary/board 延迟设置明确上限。

### Database

- schema 升级到 20；`cluster_goals`、`tasks` 和 `cluster_jobs` 新增
  `workflow_epoch`；旧记录一次性回填为 `legacy-unbound`。
- `workflow_epoch` 语义版本升级到 `workflow-v2`，覆盖新的 tick 状态机与阶段
  流水线。
- `workflow_epoch` 升级到 `workflow-v4`，覆盖分区 Read Model、summary 轮询、
  ETag/cursor 和任务详情事件读取语义；v3 继续作为安全与归档事务阶段的历史
  cohort。
- schema 19 的 `cluster_agents` runtime、parent/path/nickname、
  actor、prompt hash/status、native session path 和 last-seen；
  `task_events` 新增 thread/job/correlation/assignment 关联字段。
- schema 18 的 `cluster_jobs` attempt ledger 和 `external_operations`
  保持兼容，schema 19 migration 不删除或重写历史任务状态。
- schema 17 引入的 `cluster_goals.stall_ticks`、`stall_signature` 和
  `stalled_at` 保持兼容并继续用于跨 daemon 停滞检测。

## [0.5.0] - 2026-11-08

### Changed

- Agent 上下文策略改为“Lead 连续、Subagent fresh”：Explorer、Reviewer 和
  Worker 的 `auto` 模式默认新建 thread，只有显式 `continuity_mode=reuse`
  才复用。
- Lead 仅在已有关联 workstream 时自动恢复 thread；新增上下文比例阈值
  `CODEX_TEAM_AGENT_REUSE_CONTEXT_RATIO`，默认 `0.85`。
- Stable Developer Prompt 改为角色 Contract 前置、项目/workstream scope 后置；
  Worker turn input 改为静态 Assignment Contract 前置、动态 Goal/Task/checkpoint
  后置。
- Lead 每轮不再接收完整 task 历史，改为状态计数、全部活动任务、最近 8 个完成
  摘要和省略数量。
- Worker 和 Lead 只返回结构化摘要，不复制原始日志或完整 transcript。

### Added

- `cluster_goals` 新增持久 `checkpoint_summary` 与 `checkpoint_updated_at`。
- `cluster_agents` 新增 `profile`，旧/NULL thread 迁移为 `legacy` 且禁止被
  `v0.5` 架构自动复用。
- `cluster_jobs` 新增 `usage_missing_fields`，区分显式 0、缺失字段和无 usage
  证据；未知成本在 service/dashboard 投影为 null。
- 新增 MCP `team_goal_set_checkpoint`。
- 新增 CLI `cluster checkpoint <goal-id> --summary|--summary-file`。
- fresh Worker 会继承同 project/workstream 的最近 checkpoint。
- App Server job 成本使用 final `total` delta，并保留 JSONL usage 以修复取消、
  超时和中断前未落库的成本。

### Database

- schema 升级到 15；`TeamService` 启动时执行真实迁移并新增 checkpoint、
  Agent profile 与 usage presence 列，
  不删除已有数据。
- `/api/health` 的 `schema_version` 改为读取不可回退的
  `schema_migrations` 账本并兼容 `PRAGMA user_version`，不再只返回代码常量，
  也不会因旧版 MCP 进程重连而把已迁移版本误报为旧值。

### Fixed

- 已归档 goal 仍可提供同 project/workstream 的最近 checkpoint；归档继续只承担
  页面整理职责，不会切断 Agent 的持久上下文。
- partial/unknown usage 的成功 App Server job 会隔离对应 Agent thread，避免
  未知 baseline 污染后续复用任务的成本归属。
- 归档 goal 会级联归档其全部未单独归档子任务，包括 blocked、review 和
  canceled；恢复 goal 时只恢复这些级联任务。默认 Board 和 MCP 查询同时按
  goal 归档状态兜底过滤历史残留。

## [0.4.2] - 2026-11-08

### Added

- 新增统一 `exec`/App Server usage normalizer 和 exec JSONL usage 解析。
- 新增 `teamctl usage backfill --dry-run|--apply`，从已有日志幂等回填 token。
- 新增 `/api/health` 的 Team Home、数据库路径、稳定数据库 ID 和 schema version。
- 新增 daemon 配置指纹及 `teamctl daemon restart --if-idle`。
- 新增 `CODEX_TEAM_MCP_ACCESS=full|read_only|off` 和
  `CODEX_TEAM_ALLOWED_ROOTS` 访问边界。
- 新增托管 Agent 专用 `CODEX_HOME`，只同步模型配置和认证白名单，不读取用户
  日常 sessions，避免子 Agent 出现在主侧边栏。

### Changed

- Agent 集群 runtime 默认切换为 `app_server` thread reuse；未设置环境变量时
  启用持久 thread，显式 `CODEX_TEAM_AGENT_RUNTIME=exec` 可回退。
- WebUI 顶部 RUNTIME 指标和任务卡 runtime badge 显示 thread reuse / fresh exec；
  `/api/health` 与 `/api/board` 返回 daemon 实际 runtime。
- 新增 tag 触发的 GitHub Release workflow，自动校验版本、运行测试、构建
  `v0.4.2` 便携包并上传 ZIP/SHA256。
- 8 组交叉顺序微探针验证连续 thread 第二轮 cache hit 平均提高约 `0.58pp`，
  8/8 配对为正；write Worker 的 fresh/reuse 安全边界保持不变。
- `pyproject.toml`、运行时版本、健康接口和发布包版本统一为 `0.4.2`。

### Fixed

- schema 升级到 12，记录 cache-write、reasoning、usage source 和 telemetry
  完整性。
- exec job 不再把可解析 usage 留为全 0；无证据字段保持缺失语义。
- cache hit rate 在无 usage 时返回 unavailable，不再显示为 0%。
- App Server 集成测试使用临时 `CODEX_HOME`，不再向日常 Codex 会话写入测试 thread。

## [0.4.1] - 2026-11-07

### Changed

- 集群目标归档会级联归档同一目标下尚未归档的 `done` 任务；恢复目标时只恢复
  由该目标级联归档的任务。
- 用户单独归档的任务不会因目标恢复而自动恢复。
- 归档目标和级联任务仍是软归档；Agent Thread、Codex 会话、任务事件、worktree
  和合并记录均不会归档或删除。
- `team_list_tasks` 可通过 `include_archived=true` 读取归档任务；集群目标详情和
  `team_cluster_status` 始终保留其关联任务，便于 Agent 按需查询。
- WebUI 目标按钮明确标记“含Done”，并在操作前说明 Agent Threads 不随目标归档。

### Fixed

- 数据库 schema 升级为 11，新增 `tasks.archived_by_goal`，避免目标恢复时误恢复
  用户单独归档的任务。
- 升级时一次性回填已有归档目标下尚未归档的 `done` 任务，并通过 metadata 标记
  防止重复回填或重复事件。

## [0.4.0] - 2026-11-07

### Added

- 新增跨项目多集群调度：每个 active goal 默认 1 个 Lead 和 3 个 Worker；
  另有默认 16 个进程的全机安全阀。
- 新增按最久未启动 goal 优先的公平轮转；全局容量满时记录
  `fairness_deferred`，并对同一 goal 的重复事件做 60 秒去重。
- 新增 `team_cluster_ensure` 与 `cluster ensure`：复用项目现有的
  `planning`/`active`/`paused` 目标，paused 目标只提示恢复、不自动恢复。
- 新增集群目标软归档与恢复；只允许归档已结束且无活动 job 的
  `completed`/`failed` 目标，并支持在 WebUI 中筛选查看。
- 新增 `CODEX_TEAM_CLUSTER_MAX_JOBS`、`CODEX_TEAM_CLUSTER_MAX_WORKERS`、
  `CODEX_TEAM_WORKER_TIMEOUT_SECONDS` 和
  `CODEX_TEAM_LEAD_TIMEOUT_SECONDS` 调度/超时配置，以及可选的
  `CODEX_TEAM_HEARTBEAT_WARNING_SECONDS`、
  `CODEX_TEAM_HEARTBEAT_CRITICAL_SECONDS` 心跳阈值配置。
- 容量输入接受 `1..86400`，但调度器将有效系统安全阀硬限制为最多 32、每 goal
  Worker 硬限制为最多 8。
- 长任务默认使用 2 小时 Worker 超时和 45 分钟 Lead 超时，超时后回收 job
  并按既有重试策略继续或 blocked。
- 新增 Worker 心跳 5 分钟 warning、15 分钟 critical 审计事件；心跳告警不直接
  终止进程。critical 的有效值不会低于 warning。
- WebUI 新增 Worker 槽位、Lead 数、排队 goal、公平等待、heartbeat overdue、最老心跳、
  每 goal 活动 job、队列原因和当前等待指标。
- 新增可选 App Server Agent runtime：默认仍为 `exec`，显式设置
  `CODEX_TEAM_AGENT_RUNTIME=app_server` 后使用持久 thread、后续 turn 和
  `thread/resume`；稳定 developer instructions 以项目、角色和 workstream
  前缀开头，不混入动态 goal/task。
- 新增项目内 `workstream_key` / `continuity_reason`、任务 `continuity_mode`、
  Agent list/reset 与 goal workstream 控制；缺省 workstream 使用 goal ID，
  `team_worker` 的 write 任务默认 fresh，只有显式 reuse 才复用。
- 新增 App Server context overflow 自动换 thread 重试、超时/取消
  `turn/interrupt`、daemon 重启 lost turn 恢复、reset unsubscribe 和项目/Agent
  归档同步。
- App Server job 持久化 `runtime_generation`；generation 漂移会在下一次
  reconcile 立即标记旧 turn 为 lost，并复用既有 Worker/Lead recovery。
- 新增 `scripts/agent-cache-benchmark.py`，汇总 fresh/reuse 的 token、首轮延迟
  和成功率并输出 JSON/Markdown；成功任务使用中位数，主指标为
  `last_uncached_input_tokens`，total 仅作诊断，并输出 sample count/median。

### Changed

- 正常调度按会话计算 Worker 槽位，Lead 独立；系统安全阀同时约束 Lead 和 Worker，
  只用于防止跨会话进程失控。
- 同一项目仍只允许一个 `planning`/`active` goal；多集群并行需要为不同项目创建
  目标。
- 日常非平凡开发应先调用 `team_cluster_ensure`；`team_cluster_start` 保留为
  明确创建新目标的入口，避免重复创建集群。
- 新增 `dependency_wait` 队列原因和 WebUI“等待依赖完成”标签，避免把仍受依赖
  限制的 ready 任务误报为可调度。
- `read`/`review` 任务仍禁止修改源码，但保留 `workspace-write` 沙箱以运行需要
  临时目录的测试；`write` 任务继续使用独立 worktree。
- 项目文档、健康接口版本和发布包版本统一升级到 `0.4.0`。
- `exec` 继续作为默认与 fallback；App Server 初始化自动重试一次，连续失败记录
  `agent_runtime_fallback` 后回退 exec。
- 健康共享 App Server 上的单个 RPC 失败只影响当前 job，不再重启进程或中断
  其它活动 turn；restart 前和完成后都会显式隔离其它旧 generation job。

### Fixed

- App Server `tokenUsage` 同时保留 total 与 last；单轮 input/cached/uncached/output
  优先使用 last，缺失时才按同一 Agent 的累计差值兜底，避免重复计算 output。
- ClusterManager、WebUI、CLI 和 MCP 的 Agent reset 统一走
  `AgentPool.request_reset`，并拒绝 starting/busy、superseded 和 archived Agent。
- Lead 兼容字段 `goal.lead_thread_id` 不再绕过 AgentPool 的身份、状态和 reset
  检查；thread resume 失败时会重置旧 Agent，并在同一 job 中最多新建一次 fresh
  thread。
- Lead 和 Worker 的 `contextWindowExceeded` 都只自动换新 thread 一次；Lead 第二次
  溢出会将 goal 标记 failed，Worker 会 blocked，并记录重试耗尽原因。
- App Server `thread/start` 或 `turn/start` 重启重试仍失败时，同一 job 自动清理
  thread/Agent 关联并回退 exec，不再让 dispatcher 直接 block。
- App Server RPC 内部自动重启后抛错时，通过调用前后 generation 快照立即隔离
  其它旧 generation job，并让当前 job 按既有 fresh/resume 边界在新进程继续，
  不再等待下一次 reconcile 或误回退 exec。
- 缓存 benchmark 要求显式 `task_success`；成功样本缺少有效 last 用量时固定 FAIL，
  cache-hit rate 改为按 last 单轮 cached/input 计算。
- fake App Server fixture 强制 UTF-8 Python 环境，跨平台测试不再受控制台代码页影响。
- App Server stderr 日志、fallback/restart/job 错误统一附带脱敏后的最近
  stderr；Edge smoke 的 spawn error 和 early-exit 会给出明确错误并始终清理本次
  profile。

## [0.3.1] - 2026-11-07

### Fixed

- 取消自动集群目标时会真实终止活动 Agent job，并标记为 `interrupted`。
- 被取消目标中的未完成任务保留为 `blocked`，不会留下仍在运行的幽灵任务。
- CLI、MCP 与 WebUI 的 cancel 操作统一使用同一套进程终止语义。

## [0.3.0] - 2026-11-07

### Added

- WebUI 可直接创建集群目标，并按目标状态暂停、恢复或取消。
- 新增 `POST /api/clusters` 和 `POST /api/clusters/control` 本地 API。
- 看板新增集群摘要、目标健康状态、下一步动作和 attention 提示。
- 健康摘要覆盖活动进程、各状态任务数、认领等待和需要人工处理的目标。

### Changed

- 页面首次加载后不再逐轮请求项目列表，常规轮询只读取 `/api/board`。
- 页面隐藏时暂停并取消轮询；恢复可见时立即刷新。
- 轮询失败采用 2 秒到 30 秒的指数退避，内容未变化时跳过重复渲染。
- 集群 attention 明确统计 blocked、review、认领等待至少 300 秒的任务，以及
  paused/failed 目标；60–299 秒等待只显示 warning。

### Fixed

- WebUI 控制操作完成后立即刷新 board，确保 archived 筛选和当前项目上下文
  保持一致。
- 节点不可达或后端暂时失败时不再持续高频请求，并在状态恢复后回到正常间隔。
- completed/failed 目标不能被恢复，避免非法状态转移和重复 active goal。
- daemon 启动失败时自动将新目标标记为 failed，并保留失败原因。
- 依赖未完成的 ready 任务不再触发错误的认领超时 attention。
- claim 等待等级变化会触发重绘，`/favicon.ico` 不再产生控制台 404。

## [0.2.1] - 2026-11-07

### Added

- 失效任务取消工具、ready 等待认领预警和活动 Agent job 可视化。
- 角色模板强制 claim/submit 闭环、Reviewer 新 thread 规则和跨模块契约要求。

### Fixed

- Windows 初始化把 managed 文件改写为 CRLF，制造整文件伪 diff。
- `team_create_task.priority` 增加明确的 `0..3` 范围和方向说明。

## [0.2.0] - 2026-11-07

### Added

- SQLite 任务图、依赖关系、原子认领、lease 和 append-only 事件时间线。
- Codex MCP 服务与本地 Web Kanban。
- 每项目团队角色、项目级 `AGENTS.md` 和跨会话交接规则。
- 自动 Agent 集群、Lead resume、Worker 重试和 daemon watchdog。
- 项目和任务的上下文软归档、查看已归档与恢复。
- 团队看板与上下文监控插件的跨机器联动配置。

### Fixed

- Codex `workspace-write` 对 `.git` 的只读保护导致 Worker 无法提交。
- 项目初始化覆盖并发配置并制造脏 checkout。
- JSONL 日志中的 claim token 泄漏。
- 详情侧栏覆盖顶部工具栏。
