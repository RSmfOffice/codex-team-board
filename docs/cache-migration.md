# AGENTS 变更的缓存迁移规程

## 背景：为什么 AGENTS 变化会使缓存失效

`cache_contract.agent_cache_profile()` 与 `workflow_epoch()` 都把项目 AGENTS
策略文件的哈希（`policy_hash`）纳入模型可见前缀身份。这是**有意的 fail-closed
设计**：AGENTS 是 Agent 的系统级契约，一旦变化，旧 thread 的缓存前缀已不再代表
当前契约，继续复用会让 Agent 在过期契约下运行。因此 AGENTS 变化必然使旧 cohort
的 managed Agent 无法复用，需要冷启动。

实测（本工程副本）：

| 版本 | AGENTS SHA-256 |
| --- | --- |
| 基线 `3b431376` | `5282b0c16088598ba492ad758fee3efc5395e06d0239967fa358263dbd5a41c7` |
| 当前加固分支 | `b55955533ed8f05b673ca129b2fa20a923caecfd9ab25bdfef118ce04af9d6e8` |

哈希不同 → `policy_hash` 不同 → `agent_cache_profile` 与 `workflow_epoch` 不同
→ `select_agent` 按**完整 profile 精确匹配**（`agent_pool.py` 的
`WHERE … profile = ?`）不再命中旧 thread，新任务只能 fresh 启动。fail-closed
复用保护由完整 profile 匹配保证，**与契约状态分类无关**。

### 关键区分：契约级分类 ≠ 完整 profile 匹配

`cache_profile_contract_status()`（`cache_contract.py`）只比较
`contract / manifest / workflow / schema` 四个**版本级**字段，**不比较 policy**。
policy 是每 Agent 的身份字段（随项目/角色/worktree 而异），不存在全局唯一的
"当前 policy" 可供版本级分类器比对。因此 **policy-only 变化**（仅 AGENTS 内容
改变、四个版本字段不变）时：

| 观测项 | 实测结果 |
| --- | --- |
| 旧 policy 下 profile 的 `cache_profile_contract_status` | `current` |
| 新 policy 下 profile 的 `cache_profile_contract_status` | `current` |
| 新旧完整 profile 相等（`profiles_equal`） | `False` |
| `agent archive-stale` 候选数（`candidate_count`） | `0` |

即：旧 Agent **确实不会再被复用**（完整 profile 失配），但其契约状态仍显示
`current`，`agent archive-stale` 也**不会**把它当作 stale 自动归档——该命令只
归档 `failed`、`superseded` 或契约级 `historical/unknown` 的 managed Agent。
只有当 contract/manifest/workflow/schema 任一版本字段变化时，旧 profile 才会
被分类为 `historical` 并进入 archive-stale 候选。

## 迁移规程（把冷启动当作一次受控迁移，而非静默副作用）

1. **冻结新契约**：在一个独立提交中一次性确定新的 AGENTS/角色 prompt/MCP schema
   等模型可见文本。冻结后不再零散修改，避免反复触发 cohort 失效。
2. **旧 cohort 停止接新任务**：`workflow_epoch` 失配后，`_dispatch_next_job`
   对旧 epoch 任务抛 `WorkflowEpochMismatch` 并 block；新任务只进入当前 cohort。
   旧 cohort 自然 drained，不再接收新任务。
3. **清理旧 Agent**：现存入口是 CLI `teamctl agent archive-stale`（**没有**
   `team_agent_archive_stale` 这个 MCP operation）。按失效类型分别处理：

   - **版本级失效**（contract/manifest/workflow/schema 变化，旧 profile 被分类为
     `historical`）：用 archive-stale 批量归档，先 dry-run 预览再 apply：

     ```powershell
     teamctl agent archive-stale                 # dry-run，仅列出候选
     teamctl agent archive-stale --apply         # 实际归档
     teamctl agent archive-stale --apply --reason "AGENTS cohort migration"
     ```

     该命令只归档 `failed`、`superseded`、契约级 `historical/unknown` 且无活动
     引用（无 current goal/task/job）的 managed Agent；不触碰 busy/starting、
     归档项目下的 Agent。
   - **policy-only 失效**（仅 AGENTS 变化，契约状态仍 `current`）：archive-stale
     候选为 0，**不会自动归档**。这些 Agent 已因完整 profile 失配而不再复用，
     如需回收须**人工逐个 reset**（reset 后 `select_agent` 因
     `reset_requested_at IS NOT NULL` 不再选中，下次调度 fresh 启动）：

     ```powershell
     teamctl agent list                          # 找到旧 cohort 的 agent_id
     teamctl agent reset <agent-id> --reason "AGENTS policy changed"
     ```

     若不需要立即回收，也可不干预：旧 Agent 保持 idle 但永不复用，新任务一律
     fresh 启动，行为正确，只是占用线程槽位。
4. **新 cohort 冷启动**：新任务 fresh 启动 Agent，建立新的缓存前缀。
5. **测量稳定后的命中率**：冷启动完成后，用 `agent-cache-benchmark.py` 对比
   fresh/reuse 的 uncached input 与首轮延迟，确认缓存收益恢复。

## 冷启动成本与稳定后命中率（测量方法）

冷启动成本 = 新 cohort 首批任务的 uncached input token 总量 + 首轮延迟增量。
稳定后命中率 = 复用任务的 `cached_input_tokens / input_tokens`。

测量命令（需真实 Codex 运行时与模型）：

```powershell
python scripts/agent-cache-benchmark.py --fresh fresh.jsonl --reuse reuse.jsonl `
  --json bench.json --markdown bench.md
```

门槛：成功率不降，且 uncached input 中位数下降 ≥10% 或首轮延迟下降 ≥20%。

> 说明：本隔离环境没有真实 Codex 运行时与模型凭据，无法在此产出真实 token
> 命中率数值。上述哈希与 profile 失配是实测；token 级冷启动成本与稳定命中率
> 需在接入真实运行环境后按上述命令测量，**不以估算值冒充实测**。

## 回滚

若需回滚 AGENTS 变更，恢复基线 AGENTS 内容即恢复基线 `policy_hash`。由于
policy-only 变化下契约状态始终是 `current`（见上文区分），回滚不改变契约分类；
真正决定复用的是 `select_agent` 的**完整 profile 精确匹配**：基线 policy 下的
旧 Agent 其完整 profile 重新与当前期望相等，因而可再次被复用；已冷启动的新
Agent 仍按其自身 profile 运行。归档/复用全程由精确匹配自动裁决，无需手工干预。