# 图库超时定位证据

本轮仅保留与图库超时相关的脱敏日志片段、查询计划、合成回归和最终包核对结果；不保存真实数据库、令牌、作品正文或任意 SQL 参数。完整原始日志保持在原日常数据目录。

- `failed-requests.jsonl`：实际两次图库 504 请求及对应线程；原日志局部提取，不是全程复制。
- `writer-active.json`：任务投影处理的等待现场及进程累计 I/O；进程指标包含所有线程，不能全部归给一个查询。
- `database-plans.json`：真实只读查询计划及活动任务候选数量，不包含任务或作品身份。
- `projection-delta-checks.json`：投影原版／修后 SQL 工作量与语义验证。
- `ledger-red.xml`、`ledger-checks.xml`、`ledger-checks-02.xml`：无界账本复现、中间旧格式缺次数失败及最终两项通过；中间 XML 另外两项限流回归通过。
- `integration-checks-02.xml`：最终 3 项集成小回归通过。`read-regression-stack.txt` 是普通沙箱在本地 socketpair 初始化阻塞的现场，不是应用后端堆栈。
- `old-exit.json`、`frontend-reuse.json`：旧实例正常退出与原样复用前端的核对。
- `packaged-result.json`：第一轮 `.2` 隔离 EXE 的基础核对通过；不能替代实际图库结果。
- `actual-activation.json`、`first-repair-failure.json`：第一轮 `.2` 正常启用后首页仍 504，以及资料／版本阶段和同期后台候选查询的现场；前者 `active_tasks` 取错字段，以后者状态补录为准。
- `first-repair-status.json`：随后真实状态补录，已有登录正常、自动暂停为否、活动操作数 1。
- `candidate-query-plans.json`：真实库只读 EXPLAIN，不实际执行全库缺项查询；比较匹配后限量与限定身份后的主键定位。
- `candidate-green-checks.xml`、`candidate-commit-green-checks.xml`：5 项及后续单项提交失败回滚通过；分次来源见 `candidate-query-checks.json`。`candidate-window-injection-checks.xml` 保留错误引擎注入的中间失败，不作为产品缺陷。
- `candidate-query-checks.json`：仅合成库的六类检查计划及分次验证结果，未运行全量或长测。
- `first-repair-exit.json`：第一轮 `.2` 正常退出，无剩余实例，无强制结束。
- `packaged-result-03.json`、`actual-activation-03.json`、`second-repair-failure.json`：第二轮 `.3` 隔离基础核对通过、真实首屏仍 504，以及媒体阶段和同期线程现场；包含首屏前八秒，不是整份日常日志。
- `correlation-check.xml`：内层计数必须关联当前作品的微型合成回归，不能让其他作品的版本补上本作品缺页。
- `card-media-checks.json`、`card-media-checks.xml`、`card-media-existing-checks.xml`：3 项卡片新回归和 1 项原完整读取回归通过；涵盖默认／显式版本、槽与归属、窄字段、首图回退、动图 SHA、有限 SQL 和 241 动图分支上限，仅合成数据。
