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
→ 旧 cohort 全部 managed Agent 的 `cache_profile_contract_status` 变为
`historical`，`select_agent` 不再命中，新任务只能 fresh 启动。

## 迁移规程（把冷启动当作一次受控迁移，而非静默副作用）

1. **冻结新契约**：在一个独立提交中一次性确定新的 AGENTS/角色 prompt/MCP schema
   等模型可见文本。冻结后不再零散修改，避免反复触发 cohort 失效。
2. **旧 cohort 停止接新任务**：`workflow_epoch` 失配后，`_dispatch_next_job`
   对旧 epoch 任务抛 `WorkflowEpochMismatch` 并 block；新任务只进入当前 cohort。
   旧 cohort 自然 drained，不再接收新任务。
3. **归档旧 Agent**：用 `team_agent_archive_stale`（dry-run 先预览）归档
   `cache_profile_contract_status=historical` 且无活动引用的 managed Agent。
   该操作只归档 stale，不触碰 busy/starting 或归档项目下的 Agent。
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

若需回滚 AGENTS 变更，恢复基线 AGENTS 内容即恢复基线 `policy_hash`，旧 cohort
的 cache profile 重新变为 current；但已冷启动的新 Agent 仍按其 profile 运行，
归档/复用由 `select_agent` 的精确匹配自动裁决，无需手工干预。