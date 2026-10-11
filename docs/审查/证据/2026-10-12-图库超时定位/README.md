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
- `packaged-result-04.json`、`actual-activation-04.json`、`third-repair-failure.json`：第三轮 `.4` 的隔离基础核对与真实首屏失败，后者按启动时间只保留前80秒及相关SQL；媒体新路径尚未开始就已耗尽预算。
- `second-repair-exit-followup.json`：`.3` 正常退出等待超过五分钟后，仅结束本轮拥有的进程；保留原库。
- `exit-query-plans.json`、`exact-exit-query-plan.json`、`exact-exit-query-plan-02.json`：前两份中断留证及最终精确候选SELECT计划；非覆盖操作索引存在，不能把前两份当成功计划。
- `normalized-task-checks.xml`、`normalized-task-results.json`：共享父状态3项合成验证，960项父变化不读项目／成员、不写作品状态；400份无关父历史增长不增加所选作品读取指令。
- `gallery-card-checks.xml`：3项窄卡片对照、归属／SHA、事务与读取边界验证，仅合成数据。
- `cards-tasks-upgrade-checks.xml`、`card-integration-checks.xml`：0004升级／激活／回退、失败源保留、0003兼容及正式卡片与收藏即时更新共4项通过（8.88／1.81秒），不等同真实图库已升级。
- `packaged-result-05.json`：`.5`隔离最终EXE通过，空图库7ms、正常退出1.647秒；不是实际图库首屏证据。
- `actual-activation-05.json`、`read-structure-check-exit.json`、`read-structure-check-exit-followup.json`：`.5`获批升级停在备份前只读核验，正常退出等待后仅结束本轮进程；未产生备份/候选文件、未转换原库。
- `cards-tasks-upgrade-checks-02.xml`：`.6`仅0004新增读取结构路径采用快速检查，外键、源事实、结构和映射核验仍保留；3项通过（12.81秒），不等同全索引审计。
- `third-repair-exit.json`：`.4`正常退出，保存结果时旧PowerShell编码失败；耗时只记录工具返回的上界，不伪造精确退出计时。
- `read-upgrade-stall-05.json`、`read-upgrade-stall-06.json`：两种额外旧库审计停留的线程位置与读取量，不含作品或凭据。
- `packaged-result-06.json`、`actual-activation-06.json`、`read-structure-check-exit-06.json`、`read-structure-check-exit-followup-06.json`：`.6`隔离包通过，真实升级仍停在备份前；只结束本轮自有只读进程、未转换原库。
- `cards-tasks-upgrade-checks-03.xml`：测试临时路径同时作为系统临时目录和pytest清理目标，8项均在fixture建立前遇到目录占用；不是业务验证结果。改为两个独立临时路径后用新证据验证，旧失败不覆盖。
- `cards-tasks-upgrade-checks-04.xml`：8项升级与切换定向回归通过（59.54秒），包含新增读取结构、篡改候选拒绝、失败源保留、旧转换路径、源改变和回退边界；没有跑全量或长测。
