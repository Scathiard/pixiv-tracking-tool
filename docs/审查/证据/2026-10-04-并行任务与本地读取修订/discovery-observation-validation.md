# discover_work 同次观察合并

用途：真实 ArchiveHandler、TaskQueue 与合成 SQLite 的事务计数和正确性验证；不连接外网、不读取真实图库或账号。

命名：red-01 为修复前反例，green-01 为修复后专项，regression-01 为直接相关回归；每次 pytest 使用新的 basetemp 子目录，结果保留 XML、输出和不含参数值的事务计数。

范围：core/archive.py、core/task_planning.py 与新增 test_discovery_observation_transactions.py；证明正常 discover_work 的资料／来源／收藏三个事实提交合为一个，保留派生、账号／版本和回滚边界。

保留：目录和所有结果不自动删除；阶段结束由主会话收录与决定后续清理。提交计数不构成墙钟性能或实际图库验收。

## 结果与冻结

- 基线 HEAD：89774fc；未修改 tasks.py/database.py，未提交或打包。
- red-01.xml：两个真实 discover_work 反例均只在提交数量处失败，变化／不变资料各成功提交 3 次；完整计数见 red-01-metrics.jsonl。
- green-01.xml：18 通过、2 失败；失败为新测试误认为夹具未预置资产，修成前后资产事实相等。
- green-02.xml：73 通过、2 失败，99.57 秒；既有 test_observation_transactions.py 35 项与 test_archive_pipeline.py 19 项全通过。剩余失败为新测试误认为真实 checkpoint 不缓存当前操作的空父链，修成预热后前后映射相等，同时保留不发布回滚子关系的断言。
- final-01.xml：新增专项 21 项全通过，9.86 秒；生产代码自 green-01 后未再变化，最终共 75 个独立相关用例通过。资料变化／不变、新作品、独立来源账号、动图正常／错误资料各 1 次事实提交；事实异常和提交异常各 0 次成功提交，见 final-01-metrics.jsonl。
- 覆盖：实际 handler/queue、已有 reservation、派生下载、bookmark_only、动图网络不占 writer、事实／提交回滚、删除／意图版本、来源／收藏版本、待写收藏、账号往返与旧持久页面、writer 等待后账号变化、提交后缓存发布。
- diff --check 通过；测试进程均已退出；生产／新增测试已冻结，不再运行负载。

最终文件 SHA-256：

| 文件 | SHA-256 |
| --- | --- |
| app/src/core/archive.py | 660efc0945082981f6921e66cadf7cba9238963cbf6e5bb7d038f79370ef1596 |
| app/src/core/task_planning.py | eb7007c4f00ef16824aad4d347ddb3f2afe9cb695df00f91b77072b9aee39ef2 |
| app/tests/test_discovery_observation_transactions.py | 263d704dd1194094272664fe7552773fe3091c8d275f0dbadd368b16b53e3161 |
