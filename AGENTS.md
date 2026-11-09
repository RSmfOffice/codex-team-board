<!-- CODEX_TEAM_BOARD:START -->
## Codex 团队协作策略

- 项目 ID：`1216d98495d7`。所有协作任务都通过 `codexTeamBoard` MCP 管理，
  不以聊天记忆或临时 Markdown TODO 作为共享状态。
- 默认使用稳定 Core 工具面：查询通过
  `team_read(operation="…")`，可变操作通过
  `team_write(operation="…")`。初始化后只调用一次
  `team_read(operation="help")` 获取操作目录，不要在线程中反复发现新工具。
- 主 Agent 是唯一 Lead：负责创建任务、分配角色和 worktree、最终 Review、
  集成测试、合并和标记 `done`。子 Agent 不得自行批准任务。
- 处理已标记团队项目时，Lead 必须先调用
  `team_read(operation="team_list_tasks")` 检查现有任务、依赖和 Review 状态；
  能恢复已有任务时不得重复创建。
- 子 Agent 返回后，Lead 必须读取 `team_get_task` 和
  `team_get_task_events` 验证
  `claimed_by`、`claimed` 和 `submitted`（或 `blocked`）事件。没有状态落板的
  Agent 回复不视为交付，必须重试或标记失败。
- 已完成的子 Agent thread 不得用 follow-up 开启另一个任务；新材料和新审查必须
  创建新 thread。60 秒未 claim 应提醒，5 分钟无 heartbeat 应标记异常。
- 只读探索使用 `team_explorer`，写入使用 `team_worker`，独立审查使用
  `team_reviewer`。当前 Codex 版本不能自动加载角色 TOML 时，先调用
  `team_read(operation="team_role_prompt", arguments={...})`，把返回的角色
  Prompt 作为子 Agent 的 spawn prompt。
- 每个 `write` 任务必须有独立 Git worktree 和 `codex/team/<task-id>-<slug>`
  分支。先通过 `team_write(operation="team_create_worktree")` 创建 worktree，
  再 claim 任务。
- 创建 worktree 前主 checkout 必须干净并位于命名分支；初始化产生的
  `AGENTS.md` 和 `.codex` 文件应先提交，确保 Worker 能看到团队规范。
- 一个 worktree 同一时间只能有一个 writer。不要并行修改同一文件；若任务必然
  触碰同一文件，应拆分阶段或串行执行。
- Worker 领取任务后必须保存返回的 `claim_token`，定期发送 heartbeat，并在提交
  时附上 commit、变更文件、行为摘要、测试命令与结果、风险和待决问题。
- Worker 只能把任务提交到 `review`，不能直接改为 `done`。只有 Lead 或人工
  验收者可以调用 `team_review_task`。
- 两个以上模块共享状态时，必须先声明字段名、单位、合法区间、枚举、更新者和
  消费方，并至少有一条真实生产者到真实消费者的一体化测试。
- 主 Agent 合并前必须检查 diff、依赖、测试和任务审阅记录；合并后运行集成测试，
  再关闭任务。
- 主 Agent 在阶段边界通过 `team_goal_set_checkpoint` 只记录持久决策、完成项、
  测试、风险、已否决方案和下一步；不得复制原始日志或完整 transcript。
- Lead 在同一 workstream 连续时自动复用；Explorer 的 read 任务和 Reviewer 的
  review 任务在同一 project/role/workstream/cache profile 下自动复用。
  Worker 的 write 任务默认 fresh，只有显式 `continuity_mode=reuse` 才复用。
- 接手或验收任务前读取 `team_get_task_events` 检查认领、进度、提交和 Review
  事件，不能只看最终状态。
- 在团队项目中，凡是非平凡开发任务（会修改文件、跨多个步骤或预计超过 5 分钟），
  主 Agent 必须在第一次编辑或创建 worktree 前调用幂等 cluster ensure，
  确保项目已有但仅有一个 planning/active cluster goal。已有目标时复用，不重复
  创建；paused goal 不得自动恢复。ensure 不得改写 AGENTS 或角色模板。
- cluster start 只用于明确新建目标。常驻 `teamd` 负责 Lead resume/重建、
  Worker 自动重试和 lease 回收；不得手工重复 spawn 同一 goal 的 Agent。
  `supervised` 模式在 Review 阶段暂停等待人工决定。
- 推荐最大并发线程数：4。只有任务文件范围互不重叠时才提高并发。
<!-- CODEX_TEAM_BOARD:END -->
