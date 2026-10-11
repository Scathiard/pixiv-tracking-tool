# 图库超时定位证据

本轮仅保留与图库超时相关的脱敏日志片段、查询计划、合成回归和最终包核对结果；不保存真实数据库、令牌、作品正文或任意 SQL 参数。完整原始日志保持在原日常数据目录。

- `failed-requests.jsonl`：实际两次图库 504 请求及对应线程；原日志局部提取，不是全程复制。
- `writer-active.json`：任务投影处理的等待现场及进程累计 I/O；进程指标包含所有线程，不能全部归给一个查询。
- `database-plans.json`：真实只读查询计划及活动任务候选数量，不包含任务或作品身份。
- `projection-delta-checks.json`：投影原版／修后 SQL 工作量与语义验证。
- `ledger-red.xml`、`ledger-checks.xml`、`ledger-checks-02.xml`：无界账本复现、中间旧格式缺次数失败及最终两项通过；中间 XML 另外两项限流回归通过。
- `integration-checks-02.xml`：最终 3 项集成小回归通过。`read-regression-stack.txt` 是普通沙箱在本地 socketpair 初始化阻塞的现场，不是应用后端堆栈。
- `old-exit.json`、`frontend-reuse.json`：旧实例正常退出与原样复用前端的核对。
