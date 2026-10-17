# 启动恢复查询：R-072 / BUG-108

## 根因与候选

旧版虽然先选 SQLite rowid、再取实际 running 项，仍然只有 `state='running'` 约束。真实库现有索引均以 `operation_id` 为首列，SQLite 3.53.1 的执行计划因此是 `SCAN operation_items USING COVERING INDEX ix_task_item_running`，必须遍历已完成历史项的索引。强制另一个现有索引、普通父子 JOIN 都仍会全扫，不能解决问题。

本次只给原子查询增加 `operation_id IN (SELECT operations.id)`，利用全部父项身份逐个查找 `(operation_id,state)` 前缀。真实库同条件的候选计划为 `SEARCH operation_items USING COVERING INDEX ix_task_item_running (operation_id=? AND state=?)`，父身份取自现有主键索引。没有按父状态筛选，已完成父项的 running 子项也包含在内；保留原有 rowid 物理定位、ORM 更新和整次恢复写事务。

不新增索引、不改 schema、不删历史、不跳过恢复，也没有在真实库执行恢复 SELECT 或写操作。

## 证据与验证边界

- `startup-real-query-plan-before.json`：只读真实库的索引元数据和旧版／强制索引／普通 JOIN 的 EXPLAIN。
- `startup-real-query-plan-candidates.json`：只读真实库的父 ID IN、CROSS JOIN、递归前缀候选 EXPLAIN；实施选择最小的父 ID IN。
- `startup-red.txt`：修订前，通过实际 `TaskQueue.recover()` 捕获 SQL，再在同模型索引、16,000 条合成 succeeded 历史项／1 个 finished 父项上回放。空结果仍消耗至少 48,000 SQLite VM 步，访问预算断言失败。
- `startup-green.txt`、`startup-green.xml`：同一回放条件变为小于 100 VM 步（计数器每 100 步一次，因此报告 floor=0），3 项通过，pytest 运行 5.52 秒。另两项覆盖 finished 父项恢复、不改变已完成／待运行子项、attempts/result 保留、幂等，以及 item 自动 flush 后父状态处理失败的回滚与重试。

上述 VM 步数仅对比同一合成条件下的访问量，不换算成真实图库启动耗时。真实日常实例 PID 14680 在核对期间保持运行；此次真实库只使用 `mode=ro`、`query_only` 和 2 秒进度预算查询 sqlite_schema／EXPLAIN，未读业务正文、凭据或执行重型扫描。最终 EXE 与日常重新启用由主会话验证。

## 文件与临时项

实现：`app/src/core/tasks.py`；小回归：`app/tests/test_recovery_query_plan.py`。

临时项仅在本工作树 `work/tmp/tailscale-startup-dock/startup/`，包括 pytest basetemp 和 conftest 创建的隔离 DATA_ROOT；主会话统一收尾。原有根仓库未跟踪测试、版本文件、主文档和其他子任务文件未由此子任务编辑。
