# Team Board 流程加固任务清单与施工注意事项

> 计划日期：2026-11-10  
> 关联审计：[AUDIT-FLOW-2026-11-10.md](AUDIT-FLOW-2026-11-10.md)  
> Team Board Goal：`G-20261110-027E04`  
> Workstream：`flow-hardening-phase1`  
> 当前生产基线：`016dfd710b7ccbd1df49b9301f92fbf28c5a9792`

---

## 1. 目标

本计划把审计中发现的流程问题拆成可独立验收的施工批次。原则是先修“状态和契约会撒谎”的问题，再修“恢复和观测不完整”的问题，最后才做大规模模型重构。

施工必须同时满足：

1. 不降低现有功能。
2. 不破坏 Core MCP 稳定工具面。
3. 不让新的状态表成为第二个或第三个真相源。
4. 不把成功路径修好却遗漏失败、取消、超时和重启路径。
5. 不把 native Thread 直接塞进 managed 自动复用池。
6. 不在未完成迁移和兼容设计前删除旧数据路径。
7. 不触碰 blocked 的 `T-20261109-FE3227` 及其 worktree。

---

## 2. 施工批次

### Phase 0：规划与文档

状态：已完成

- [x] 完成完整流程拆解。
- [x] 建立 H1-H14 交接点。
- [x] 建立 P1/P2 问题清单。
- [x] 创建本施工清单。
- [x] 将审计与计划文档提交到主分支。
- [x] 创建实施 task 和隔离 worktree。

验收：

- 主 checkout 干净。
- 计划文档可独立阅读。
- Team Board 有唯一活动 goal。

---

### Phase 1：入口契约、状态真实性和基础安全

状态：已完成并通过 Lead Review、合并和真实库迁移验证

这是当前第一批实施内容。

#### F1-01 MCP 严格 schema 校验

- [x] 实现 operation/tool inputSchema 运行时验证。
- [x] 支持对象、字符串、整数、布尔、数组、required、enum、minLength、minimum、maximum、additionalProperties 和 items。
- [x] Core dispatcher 和 legacy surface 都执行相同校验。
- [x] 禁止字符串 `"false"` 被 `bool()` 转为批准。
- [x] 禁止未知 `team_cluster_control.action` 进入 resume fallback。
- [x] 错误以 MCP tool error 返回，不抛非结构化 traceback。

不变量：

```text
invalid input => no domain mutation
```

测试：

- `approve="false"` 被拒绝。
- `approve=0` 被拒绝。
- 未知 action 被拒绝。
- 缺少 required 字段被拒绝。
- additionalProperties 被拒绝。
- 合法 Core 和 legacy 调用保持兼容。

#### F1-02 CLI 与 MCP 命令契约一致

- [x] 为 CLI `task update` 增加 `--commit`。
- [x] 所有 CLI/MCP 同名字段使用相同语义。
- [x] 回归 CLI submit/update commit_sha。

测试：

- CLI update 能持久化 commit SHA。
- MCP update 与 CLI update 对同一任务行为一致。

#### F1-03 禁止在 terminal/archived goal 下创建可调度任务

- [x] `create_task(goal_id=...)` 检查 goal 状态和 archived_at。
- [x] completed/failed/archived goal 返回明确 TeamError。
- [x] 仍允许 planning/active/paused goal 下预先规划任务。

测试：

- completed goal 拒绝创建 task。
- failed goal 拒绝创建 task。
- archived goal 拒绝创建 task。
- paused goal 按产品契约允许创建但保持不调度。

#### F1-04 Review 合并证据闭环

- [x] write task 的 `commit_sha` 必须存在。
- [x] Worker commit 必须是 merge commit 的祖先。
- [x] merge commit 必须仍可解析在目标分支上。
- [x] task/project archived 时禁止 Review。
- [x] Review 使用 task.version 做 compare-and-swap。
- [x] 并发 Review 只有一个终态赢家。

测试：

- 无关历史 merge commit 被拒绝。
- 正确包含 Worker commit 的 merge 被接受。
- 已归档 task/project 被拒绝。
- 两个并发 Review 只有一个成功。

#### F1-05 unknown context 禁止自动复用

- [x] Agent release 时区分 context unknown 与真实 context 0。
- [x] context/window 不完整时清空可复用窗口信息。
- [x] 自动 reuse 只允许 context/window 已知且低于 ratio 的 Agent。
- [x] `turn_count=0` 的新 Agent 按空上下文处理。

测试：

- context 缺失时 reuse 返回 None。
- context=0、window 已知、turn_count>0 时可以复用。
- context 超过 85% 时返回 None。
- 第一次成功 usage 后能正常进入复用池。

#### F1-06 Worktree 路径边界

- [x] `CODEX_TEAM_ALLOWED_ROOTS` 同时检查 `repo_root/repo/worktree_path`。
- [x] 确认路径 resolve 后不能逃逸 allowed root。

测试：

- allowed repo + outside worktree_path 被拒绝。
- allowed repo + allowed worktree_path 通过。

#### F1-07 Prompt/tool 名称一致

- [x] bootstrap 全局 AGENTS 改用 `team_read`/`team_write` operation 语法。
- [x] 项目 AGENTS 中残留的直接 operation 名改成明确调用形式。
- [x] 不改变 Core 两工具 schema。

测试：

- 默认 bootstrap 生成内容不要求不存在的 fine-grained tool。
- 角色 Markdown/TOML、项目 AGENTS、MCP Core 工具面一致。

#### F1-08 回归与运行态验证

- [x] 完整 unittest。
- [x] UI smoke。
- [x] compileall。
- [x] diff check。
- [x] Core tools/list 仍只有 `team_read/team_write`。
- [x] 工作区干净。

本轮验证：

```text
146 tests OK, 1 skipped
dashboard-ui-smoke: ok
compileall ok
Core schema hash:
86e78eef5f5f631094f5218d763c83997f5ab52d1ba22877f93092720f93fca6
Worker commit:
a88072eee5106b45b1f0b012bebdc9e1e738c43d
Merge commit:
38c3d71520a10944c11cafdc6946327bbc153b66
```

---

### Phase 2：Assignment、外部副作用 Saga 和统一 Settlement

状态：已完成并通过 Lead Review、合并和真实库迁移验证

目标：把一次执行收敛成可追踪的 attempt。

- [x] 将 `cluster_jobs` 扩展为统一 attempt ledger，而不是新增第二套 job 状态。
- [x] 建立 `external_operations` durable intent/outbox 基础表。
- [x] worktree create 使用 intent + recover。
- [x] App Server thread/start、turn/start 使用 attempt 关联。
- [x] exec 启动使用同一 attempt 关联。
- [x] 建立统一 `_settle_job_row()`。
- [x] 成功、失败、超时、取消、lost、context overflow 全部走 settlement。
- [x] failed/interrupted/lost job usage 写入 attempt 和 latest-job 投影。
- [x] App Server baseline 改读已完成 attempt ledger；Dashboard 增加 ledger totals。

验收：

- 每个活动 attempt 都有 task/job/runtime/thread/generation/correlation。
- 任意外部步骤失败后可在下一 tick 解释和恢复。
- 不再出现 failed Agent 0 用量但对应 job 有真实 token。

本轮验证：

```text
151 tests OK, 1 skipped
dashboard-ui-smoke: ok
compileall ok
schema 18
Core schema hash:
86e78eef5f5f631094f5218d763c83997f5ab52d1ba22877f93092720f93fca6
Worker commit:
397525b99eb67d2264373cdb5ba87247ab7b6d5e
Merge commit:
602e98ce7c11136dc7fbfb547ef819302aeb762d
Live schema after migration:
18
Terminal jobs missing settlement:
0
Jobs missing correlation:
0
```

---

### Phase 3：Prompt Contract 与 Agent Thread 统一

状态：代码与自动化验收完成，等待 Lead diff Review 和合并

- [x] 建立单一 Prompt Contract manifest；MCP instructions 直接由 manifest 加载。
- [x] AGENTS、角色 Markdown/TOML、工具 schema、policy 内容和运行时身份进入同一
  cache profile；实际文件或 instruction 变化都会失配。
- [x] cache profile 自动绑定 access mode、MCP instructions、manifest/contract、
  runtime version/identity、surface、sandbox、AGENTS 和角色 instructions。
- [x] 用 `cluster_agents.runtime=managed|native` 建立统一 Thread Registry，并由
  managed AgentPool 与 native session adapter 分别维护生命周期。
- [x] native Thread 默认 `same_task_only`，不会进入 managed 自动复用池或 managed
  reset 路径。
- [x] native spawn 通过 session JSONL 的 user message 验证任务正文送达；
  `missing/unknown` 阻断 claim，成功 claim 事件作为执行确认。
- [x] task_events 增加结构化 `thread_id/job_id/assignment_id/correlation_id`，
  Dashboard/API 同时返回这些字段。

验收：

- full/read_only、版本、AGENTS、instructions 任一变化都会使旧 profile 失配。
- native thread 可看、可追溯，但不进入 managed 自动复用池。
- `fork_turns=none` 不再出现“线程创建但任务未送达”。

本轮验证：

```text
161 tests OK, 1 skipped
dashboard-ui-smoke: ok
compileall ok
schema 19
Core tools: team_read, team_write
Core schema hash:
86e78eef5f5f631094f5218d763c83997f5ab52d1ba22877f93092720f93fca6
Worker commit:
a00038d2fb7960ffcb7cee3ffc5b7eee1316cb2d
Merge commit:
5c95b2a62cd3b4af373b5d78a40d4078d346ef9d
Live schema after migration:
19
Legacy managed agents:
21 / 21
Terminal jobs missing settlement/correlation:
0 / 0
```

---

### 后续施工顺序

1. Phase 4A 先封死旧 Agent、旧 worktree/branch 和 native Thread 的错误分配面。
2. Phase 4B 修状态机和 tick 阶段边界，保证调度结果可解释、可恢复。
3. Phase 4C 处理来源校验、归档事务、数据库初始化和脱敏等 P1 安全缺口。
4. Phase 4D 再拆分 Read Model，避免在状态机未稳定时过早扩大 Dashboard 读取面。
5. Phase 5 最后做 daemon 身份、安装回滚、日志和历史 worktree 清理。

Phase 4A 是低风险、高收益的分配安全门禁，应作为下一批唯一首选写任务。
Phase 4B 可以在全量基线通过后开始；Phase 4C 以后按共享文件范围串行执行，
不要让多个 Writer 同时修改 `service.py`、`cluster.py` 或 `dashboard.py`。

---

### Phase 4A：Assignment Boundary Hardening

状态：代码与自动化验收完成，等待 Lead diff Review 和合并

目标：不拆新版架构、不删除历史数据，只保证旧 Thread、旧 worktree/branch
和历史任务对象不会进入新任务的分配链。

- [x] 固化 Agent 分配边界：只允许 `runtime=managed`、状态可复用且 cache
  profile 与当前 Prompt Contract 完全一致的历史 Thread。
- [x] 增加旧 `legacy`、`v0.5` 和“只匹配部分 profile”的反向回归，确认
  `continuity_mode=reuse` 也不会退化为模糊匹配。
- [x] 固化 worktree/branch 分配边界：新任务只能使用自己的 task worktree；
  已被其他 task 使用的路径或 branch 必须拒绝。
- [x] 固化 native Thread 边界：同一 native thread/actor 只能绑定同一 task；
  prompt 未 delivered 时不能 claim。
- [x] Dashboard 默认明确标识旧 profile Thread 为不可复用历史；是否提供
  “显示历史 Thread”由展示筛选实现，不新增第二套 Thread 状态。
- [x] 用当前正式库上 8 个 `legacy`、13 个 `v0.5` Thread 做只读兼容性验证。

验收：

- 新任务无论使用 `auto` 还是 `reuse`，都不会选中旧 profile Thread。
- 旧 task/worktree/branch 不会被新 task 隐式接管。
- native Thread 不能跨 task 复用。
- 所有旧 Agent、job、usage 和事件继续保留，可查询但不参与新分配。

本轮验证：

```text
165 tests OK, 1 skipped
dashboard-ui-smoke: ok
compileall ok
Core tools: team_read, team_write
Core schema hash:
86e78eef5f5f631094f5218d763c83997f5ab52d1ba22877f93092720f93fca6
Live historical managed Threads:
21 / 21
Live current-contract managed Threads:
0
```

---

### Phase 4B：调度状态机与 Tick 流水线

状态：未开始

- [ ] 将 tick 拆成 settle/recover/control/prepare/dispatch/health/publish 阶段。
- [ ] 每个阶段输出耗时与计数。
- [ ] 修复 `planning` goal 在全部任务终态时直接尝试 `completed` 的非法转换；
  应由 prepare 阶段显式执行可审计的 `planning -> active -> completed` 收敛。
- [ ] 覆盖并发 tick、daemon 重启、空任务 Goal 和 terminal reconciliation。

验收：

- 每次 tick 的阶段顺序固定，任一阶段失败能定位到具体阶段。
- Goal 状态转换全部符合显式状态机，不再依赖调用方绕过非法转换。

---

### Phase 4C：安全与状态完整性

状态：未开始

- [ ] Dashboard 写接口校验 loopback Host、same-origin Origin 和
  `application/json`，无 Origin 的 CLI 客户端保持兼容。
- [ ] Goal resume 与 daemon 启动使用可回滚顺序，daemon 启动失败不留下
  active 假状态。
- [ ] 项目/任务归档与 claim 使用单一 `BEGIN IMMEDIATE` 事务。
- [ ] 数据库 URI 转义、并发迁移重试和只读连接回归。
- [ ] 脱敏覆盖未加引号的冒号格式，并增加凭据泄漏反向测试。

验收：

- 非法来源和错误 Content-Type 在业务写入前被拒绝。
- archive/claim 竞争不会产生 archived project + running task。
- 数据库路径包含 `#`、空格或 Windows 特殊字符时迁移和只读连接正常。

---

### Phase 4D：Read Model 与 Dashboard 扩容

状态：未开始

- [ ] split dashboard API：summary、tasks、goal、events、agents、attempts。
- [ ] 使用 version/ETag/cursor，避免每 2 秒读取全量 board。
- [ ] task detail 按 task 读取事件，不再依赖全局最新 500 条。
- [ ] unknown、真实 0、unavailable 分离。
- [ ] fresh/resume/fallback_exec 分离展示。
- [ ] 为大任务库和事件历史建立轮询大小、延迟和结果正确性门禁。

验收：

- 大任务库下 board 轮询大小和延迟有明确上限。
- task detail 不再依赖全局事件窗口。

---

### Phase 5：安装、进程与历史清理

状态：未开始

- [ ] daemon/job lock 绑定 process identity。
- [ ] taskkill 前验证 executable/start time/token。
- [ ] 安装器改为 preflight/apply/verify/rollback。
- [ ] watchdog 支持多 TeamHome。
- [ ] 日志轮转。
- [ ] 盘点并摘除已失效或已完成 task 的 worktree 注册；保留 branch、commit
  和审计证据，不删除 blocked 任务历史。
- [ ] 评估旧 `T-20261109-FE3227` 分支：已被新实现覆盖的部分标记
  superseded，仍有价值的语义拆成新测试或后续任务，不整体合并旧分支。

验收：

- 安装、升级和失败恢复均可重复执行或回滚。
- 不会因 PID 复用终止无关进程。
- 历史 worktree 不再占用活动分配面，但所有代码和审计记录仍可恢复。

---

## 3. 全局实现约束

### 3.1 单一入口

所有业务写入最终只能通过 Command/Application 层。CLI、MCP、Dashboard 和 native adapter 只做协议适配。

禁止：

- 在 Dashboard 新增一套独立状态机。
- 在 MCP handler 中直接写表。
- 在 CLI 绕开 TeamService 修改任务。
- 在 native adapter 中维护第三套 Thread 状态。

### 3.2 单一真相源

必须明确：

| 信息 | 唯一所有者 |
| --- | --- |
| Task 生命周期 | Task aggregate |
| Goal 生命周期 | Goal aggregate |
| Attempt/job 生命周期 | Assignment/Attempt |
| Thread 可复用性 | Thread registry + adapter |
| Job usage | job/attempt ledger |
| Agent 累计 usage | settlement 投影 |
| Dashboard | 只读 projection |

### 3.3 原子状态迁移

至少以下操作必须使用 `BEGIN IMMEDIATE + version CAS` 或同一事务：

- claim；
- submit；
- block/unblock；
- cancel；
- review；
- archive/restore；
- goal cancel/pause/resume；
- assignment finalization。

### 3.4 外部副作用

Git、进程、Thread、注册表和计划任务调用不得直接夹在业务事务中形成不可恢复状态。

统一模式：

```text
preflight
→ write durable intent
→ perform external action
→ verify
→ finalize state + event
→ cleanup intent
```

### 3.5 失败路径优先

每新增一个成功路径，必须同时覆盖：

- 参数非法；
- 权限失败；
- 数据库冲突；
- 进程启动失败；
- runtime disconnect；
- timeout；
- interrupt/cancel；
- restart；
- duplicate request。

### 3.6 缓存安全

- 动态 goal/task/board/checkpoint 只能进入 turn input。
- 稳定 prefix 只能由 manifest 生成。
- context unknown 不得视为 0 并自动复用。
- access mode 变化必须失配 profile。
- native 与 managed 不得共享未经验证的复用资格。

### 3.7 事件与身份

所有新增事件必须包含可用的：

```text
correlation_id
task_id
goal_id
job_id
thread_id
assignment_id
```

不存在的字段允许为 NULL，但不能用 actor 字符串猜测。

### 3.8 向后兼容

- schema 变更必须可重复执行。
- 新列必须有安全默认值。
- 旧 agent/job/event 数据必须继续可读。
- 多进程首次启动必须可重试。
- 新 read model 不得让旧 CLI/MCP 立即失效。

---

## 4. 禁止重复引入的风险

1. 不在新表中复制一份 status 后再手写双向同步。
2. 不把 request_id 当作日志字段却不做幂等。
3. 不在 review 中只检查 merge commit 存在，却不检查 Worker commit。
4. 不用 `bool(value)` 处理 MCP 布尔值。
5. 不为未知 action 提供默认成功分支。
6. 不把 context missing 归一化为真实 0。
7. 不让 failed Agent 丢失 job usage。
8. 不让 terminal/archived 对象继续参与 claim/review/archive。
9. 不让 worktree、thread、job 在失败后成为无主对象。
10. 不让成功路径和失败路径使用不同 status/event 语义。
11. 不把 native thread 直接加入 managed AgentPool。
12. 不让 Dashboard 从多个原始表自行推断 unknown/success。
13. 不在没有迁移测试时修改 DB schema。
14. 不把 token、claim、cookie 或认证信息写入日志、文档或最终回复。

---

## 5. 第一批验收命令

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

额外必须验证：

```text
Core tools/list = team_read + team_write
invalid schema => tool error and no state mutation
unrelated merge => review rejected
archived task/project => review rejected
unknown context => no automatic reuse
outside worktree_path => rejected
terminal goal => task creation rejected
```

---

## 6. 与 blocked 分支的协调

`T-20261109-FE3227` 的隔离分支已有部分候选修复。

施工要求：

1. 先阅读其 diff 和测试，只作为参考。
2. 不在 old path 上重复实现同一修复。
3. 新实现应吸收其有效不变量和测试。
4. 未经用户明确授权，不 unblock、不 claim、不修改、不合并该任务。
5. 如果两个分支修改同一文件，先比较语义，再选择唯一实现。

---

## 7. 完成定义

每个 Phase 只有在以下条件全部满足后才能完成：

- 代码与文档一致。
- 成功、失败、取消、超时、重启路径都有测试。
- 数据迁移可从真实旧库执行。
- 没有新增无主状态。
- 没有新增 unknown→0 或 missing→success。
- 完整回归通过。
- UI smoke 通过。
- Core MCP schema 保持稳定。
- 工作区干净。
- 审计文档记录变更和遗留风险。
