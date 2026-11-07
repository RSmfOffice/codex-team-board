<!-- CODEX_TEAM_BOARD:START -->
## Codex 团队协作策略

- 使用 `codexTeamBoard` MCP 作为唯一任务状态源。
- 主 Agent 负责拆解、创建任务、分配 worktree、验收、合并和关闭任务。
- 每个 write 任务必须先创建独立 worktree，再认领、实现、测试和提交 review。
- 子 Agent 不得直接标记 done，也不得修改其他 Agent 的工作区。
- 主 Agent 在合并前检查 diff、测试证据、依赖和 Review 结论。
<!-- CODEX_TEAM_BOARD:END -->
