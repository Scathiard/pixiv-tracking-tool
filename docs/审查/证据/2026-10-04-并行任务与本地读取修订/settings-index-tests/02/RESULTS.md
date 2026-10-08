# 提交失败专项补验结果

- 三项用例：新 test_settings_commit_failure_rolls_back_and_runtime_releases_lease，既有联合 DDL 回滚，final receipt 回滚。
- pytest：3 passed，6.49 秒；JUnit XML：tests=3，failures=0，errors=0，skipped=0。
- 注入：SQLAlchemy Connection 的 commit 事件；两条设置索引已在事务中建立，再在 DBAPI SQLite COMMIT 前抛受控 RuntimeError；不是操作系统 I/O 故障或断电。
- 断言：故障点确已被调用一次；原索引与所有表数据不变；备份与故障前快照完全一致；receipt.state=failed、error_code=RuntimeError，无私密异常正文；Runtime 释放租约。
- 日志异常：pytest.log 第 1 和 105 行各出现 Windows fatal exception: access violation，堆栈位于 importlib get_data / zoneinfo / Alembic 导入，之后测试完成；本轮不标为完全干净。
- 退出码边界：外部程序经 Tee-Object 管道收集输出，宿主命令 exit_code=0 不独立证明 Python 原始退出码；没有补记或推定。
- 状态：生产代码与 settings SQL 未再变化，tests README 已登记新文件，git diff --check 通过；源码冻结，未再次自行运行测试。
- 证据：pytest.log、pytest.xml、pytest/ 合成库全部保留；导入异常后续处理由主会话决定。
