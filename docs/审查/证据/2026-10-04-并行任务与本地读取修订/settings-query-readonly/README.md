# 设置统计查询只读审查与有界对照

用途：固定源码 9eb03bc 的设置统计成本审查；只读取已关闭的 08 合成库，不触及真实图库、配置、现有测试或生产源码。

唯一数据源：D:\claude_sandbox\pixiv-tracking-tool\.devtools\test-runs\b20261004.3-reliability-20261008-08\check-workers-1\archive.db（605646848 字节）；result.json 标记 runtime_closed=true，打开前核对无非空 WAL。SQLite URI 使用 mode=ro&immutable=1 和 query_only=ON；immutable 仅用于已经关闭且 checkpoint 后无 WAL 的固定证据库，不表示建议程序日常采用该模式。

测量：原 SQL／全局成员资格 EXISTS 候选 × 默认 cache_size／连接级 32MiB，四臂独立子进程串行执行，正序和逆序各一遍，每臂首次加同连接两次暖读；共 24 次有界查询。候选仍按全局默认版本成员资格计资产归属作品，包含被抑制选择者，不改变 schema、统计事实或接口。

记录：五项聚合完整结果、EXPLAIN QUERY PLAN、墙钟／进程 CPU、VM 指令上下界（1000 指令回调量子）、进程读写操作和字节、工作集／峰值。使用现有 4.5 秒取消边界；不启动后台、不清 OS 缓存、不把进程读 IO 叫作物理盘 IO，也不把单次改善叫作通过长期验收。

文件：measure.py 为只读入口；measurement-01.json 保存逐臂结果与计划，measurement-01.txt 保存过程输出，schema-01.json 保存两表现有索引只读元数据，review.md 保存结论及待授权覆盖索引设计评估。命名按轮次加序号；所有材料保留，不自动删除或覆盖旧输出。
