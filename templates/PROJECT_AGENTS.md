<!-- CODEX_TEAM_BOARD:START -->
## Codex 团队协作策略

- 使用 `codexTeamBoard` MCP 作为唯一任务状态源。
- 主 Agent 负责拆解、创建任务、分配 worktree、验收、合并和关闭任务。
- 非平凡开发任务开始编辑前，主 Agent 必须调用 `team_cluster_ensure`；
  已有 planning/active goal 时复用，paused goal 不得自动恢复。
- 每个 write 任务必须先创建独立 worktree，再认领、实现、测试和提交 review。
- 子 Agent 不得直接标记 done，也不得修改其他 Agent 的工作区。
- 主 Agent 在合并前检查 diff、测试证据、依赖和 Review 结论。
- 主 Agent 在阶段边界调用 `team_goal_set_checkpoint`，只记录持久决策、完成项、
  测试、风险、已否决方案和下一步；不得复制原始日志或完整 transcript。
- 主 Agent 默认延续同一 workstream；Explorer、Reviewer 和 Worker 默认 fresh，
  只有显式 `continuity_mode=reuse` 才复用 thread。
<!-- CODEX_TEAM_BOARD:END -->
