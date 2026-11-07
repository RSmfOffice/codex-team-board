# Changelog

本项目遵循语义化版本。

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
