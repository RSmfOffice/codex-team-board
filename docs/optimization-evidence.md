# 结构优化量化证据

> 所有数字为本机实测（Python 3.14.7 / SQLite 3.50.4，Windows）。
> 测量脚本见工作区 `work-logs/measure_perf.py`。

## 1. list_tasks：SQL 执行次数与耗时

旧实现对每个任务执行 `_blocking_tasks`（1 查询）+ `_is_ready`（2 查询）= 3 查询/任务；
新实现用分块批量查询（每批 1000 个 task_id）。

| 任务数 N | 旧 SQL 次数 | 新 SQL 次数 | 旧耗时 | 新耗时 | 加速 |
| --- | --- | --- | --- | --- | --- |
| 100 | 300 | 1 | 1.4 ms | 0.2 ms | ~7× |
| 1000 | 3000 | 1 | 78.1 ms | 2.6 ms | ~30× |
| 5000 | 15000 | 5 | 2447.6 ms | 16.6 ms | ~147× |

结论：查询次数从 `3N` 降为 `ceil(N/1000)`，大看板耗时下降两个数量级。
分块同时规避了 SQLite 32766 参数上限（实测 32767 个 ID 旧写法报
`too many SQL variables`，新写法正常）。

## 2. exec 日志：扫描遍数

- 旧 `_parse_codex_log`：先 `usage_from_exec_log` 全文件扫一遍取用量，再独立
  全文件扫一遍取 thread_id/completed/error/present = **2 次全文件扫描**。
- 新 `_parse_codex_log`：调用 `scan_exec_log` **1 次全文件扫描**同时采集用量与
  生命周期事实。

结论：文件扫描遍数 2 → 1（设计层面确定，单遍函数只打开并遍历文件一次）。
对 3000 条 turn.completed 的样本，单遍解析约 60 ms；I/O 与 JSON 解析工作量减半。
`test_exec_log_differential.py` 用独立两遍参考实现逐字段差分，证明输出等价。

## 3. 源码行数与文件数变化（权威 git diff --stat）

基线 `3b431376` → 当前加固分支（含三轮加固 + 结构重构 + 评审修复）：

```
11 files changed, 1094 insertions(+), 542 deletions(-)
新增文件：src/codex_team/read_model.py（181 行）
```

**诚实结论**：本轮整体是**净增加约 552 行、新增 1 个文件**，因为叠加了安全修复、
有界重试、一致性逻辑与大量回归测试。因此**不能称为"整体更精简"**。准确的表述是：

- **减少重复**：summary/snapshot 的同构派生逻辑集中到 `read_model.py`，删除两处
  内联副本；App Server 三处重复清理块合并为一个辅助方法。
- **集中职责**：纯函数读模型与 DB 查询分离，别名解析与池存储语义边界清晰。
- **效率提升**：list_tasks 查询次数与耗时大幅下降，exec 日志扫描遍数减半。

若仅看"结构重构"本身（不含新增修复/测试），重复代码确实减少；但交付物总体积
因正确性与测试投入而增加，这是有意取舍，不以"精简"误导。

## 4. 行为等价证明（不以测试全绿替代）

- Agent 池用量归一：`test_agent_usage_compat.py` 对独立旧版参考实现逐输入差分
  （27 用例 / 24 子测试），覆盖全部旧别名（含 `cached_tokens`）、嵌套与推导边界。
- exec 日志单遍：`test_exec_log_differential.py` 对独立两遍参考逐字段差分
  （9 样本 + 不可读日志）。
- 批量查询：`test_blocking_chunking.py` 断言批量结果与逐任务旧实现输出完全一致。
- 读模型一致性：`test_read_model_consistency.py` 覆盖 global/project ×
  include_archived true/false，断言 summary==snapshot 且 canceled 可见性统一。