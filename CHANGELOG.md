# Changelog

本项目遵循语义化版本。

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
  `thread/resume`；稳定 developer instructions 不混入动态任务。
- 新增项目内 `workstream_key` / `continuity_reason`、任务 `continuity_mode`、
  Agent list/reset 与 goal workstream 控制；缺省 workstream 使用 goal ID，
  `team_worker` 的 write 任务默认 fresh，只有显式 reuse 才复用。
- 新增 App Server context overflow 自动换 thread 重试、超时/取消
  `turn/interrupt`、daemon 重启 lost turn 恢复、reset unsubscribe 和项目/Agent
  归档同步。
- 新增 `scripts/agent-cache-benchmark.py`，汇总 fresh/reuse 的 token、首轮延迟
  和成功率并输出 JSON/Markdown；门槛为成功率不下降且 uncached 降低 10% 或
  首轮延迟降低 20%。

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
