# 候选冻结

- 基线：d50d0dd45a81f0d8b50b5f70e18d9cc7ae3805e3；只改 database.py、runtime.py、tasks.py、新 wal_maintenance.py 与 test_wal_maintenance.py，未触碰 archive.py/task_planning.py。
- 实施：唯一非 daemon 维护线程/其专属连接；Runtime 就绪后业务 FULL/auto0，独立数据库 FULL/auto1000；重连沿用已启用策略。NOOP/PASSIVE-only，没有 WAL 二进制解析。
- 压力：32MiB/16MiB 按实际 page_size 换算总帧水位，另记未回写帧；单个世代绑定许可由真实候选原子消费，空 lane 不消费，round 与计划/处理分别计推进单位。在途文件实际发布与结案不中断，用户新意图仍可保存。
- 关闭：期限覆盖启动锁等待；恢复后检查关闭意图；任务与收藏→owner join→DB/租约。迟到连接、I/O 边界与重复关闭专项保留所有权，正常恢复后可收尾。
- 诊断：snapshot() 只复制有界 RAM 状态；错误类型首次/变化记录一次，整个整理周期成功再记录恢复，NOOP 单独成功不造成假恢复刷日志。
- 红绿：初始启动红例与 RESTART 回写间隙红例原样保留；最终 related-final.xml 84 项通过，含29项新增专项。git diff --check 通过。
- 风险/未测：软压力不保证物理 WAL 硬上限，也不保证永久长读条件下的完全回写或机械盘 SQL/同步耗时；前台持续写、大事务和已有在途单元可越水位。未跑全套、大型负载、8小时、浏览器/EXE或真实数据，未提交/构建/日常启用。规范规模与长期验收由主会话串行执行。
