# 专项结果

- 执行目录：D:/claude_sandbox/pixiv-tracking-tool/.worktrees/v0.2.0-build20261004.3/app。
- 运行时：主仓库 .devtools/v012-env/Scripts/python.exe；没有安装或修改全局依赖。
- 结果：28 passed，47.41 秒；仅现有 anyio BlockingPortal 弃用警告。
- 范围：test_settings_index_preparation.py、test_task_index_preparation.py、test_settings_projection.py、test_upgrade_revisions.py。
- 已核验：新库两条非唯一完整索引；FULL 写连接；只读 ArchiveDatabase 打开不补建；Runtime 在 TaskQueue 前补建；包含已提交 WAL 的原库备份；重复打开不重复 DDL/备份；已有一条索引保持；两个同名错误定义拒绝且不替换；任务/设置联合 DDL 失败整体回滚；最终 receipt 保存失败回滚；备份失败不创建索引并释放租约；新库 DDL 失败连表回滚；严格 0002 库继续显式升级，目标 0003 候选具备两索引。
- 语义：现行设置投影与跨作品复用回归通过；统计 SQL 未改，没有新增结果缓存或强制索引提示。
- 兼容：现有 prepare_task_indexes、TaskIndexPreparationError/TaskIndexDefinitionMismatch、receipt kind/字段保持；仅用户提示泛化为本地查询索引。
- 边界：全部合成数据；未接触真实图库、令牌、配置、路径或 release，未打包、提交或运行完整/长压力验证。
- 尚待：自然计划/成本 A/B、完整维护、八小时混合负载、EXE 和真实应用门槛；本专项通过不能视作性能问题已解决。
- 证据：pytest.log、pytest.xml 和 pytest/ 合成数据库保留；git diff --check 通过。
