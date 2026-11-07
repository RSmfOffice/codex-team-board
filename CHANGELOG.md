# Changelog

本项目遵循语义化版本。

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
