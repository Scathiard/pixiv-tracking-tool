# 设置覆盖索引提交失败专项补验

- 用途：补充 SQLAlchemy Connection commit 事件异常路径，运行新用例和两条受影响 DDL/receipt 回滚用例。
- 注入层级：成功创建两索引后、实际 DBAPI SQLite COMMIT 前的提交事件抛异常；不等于操作系统 I/O 故障、真实磁盘失败或断电验证。
- 数据和命名：仅微型合成库，轮次 b20261008-settings-index-tests-02，pytest 使用新子目录。
- 保留：合成数据库、结果和日志保留，不自动删除。
