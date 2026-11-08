# Changelog

本项目遵循语义化版本。

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
- 新增 MCP `team_goal_set_checkpoint`。
- 新增 CLI `cluster checkpoint <goal-id> --summary|--summary-file`。
- fresh Worker 会继承同 project/workstream 的最近 checkpoint。

### Database

- schema 升级到 13；`TeamService` 启动时执行真实迁移并新增 checkpoint 列，
  不删除已有数据。
- `/api/health` 的 `schema_version` 改为读取不可回退的
  `schema_migrations` 账本并兼容 `PRAGMA user_version`，不再只返回代码常量，
  也不会因旧版 MCP 进程重连而把已迁移版本误报为旧值。

### Fixed

- 已归档 goal 仍可提供同 project/workstream 的最近 checkpoint；归档继续只承担
  页面整理职责，不会切断 Agent 的持久上下文。

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
