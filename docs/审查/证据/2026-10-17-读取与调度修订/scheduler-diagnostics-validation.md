# 调度与诊断定向验证

- 范围：`scheduler.py`、`core/diagnostics.py` 与对应测试；不读取真实数据库、账号、配置或历史诊断卷。
- 基线：`main / e68fcfb`，公开版本 `0.2.0`；本子范围未改版本、打包或提交。
- 调度：显式允许迟到执行、合并多周期、同一回调至多一个实例；仍提交 `check / {} / automatic`，不改增量发现范围。正常 TaskQueue 的任务受理与 `next_check_at` 更新共用一个写事务，受理或提交失败一起回滚；保存的新周期和更晚到期时间优先。自动暂停仍由持久队列延后开始，已有任务去重。升级维护早退沿用原行为且不推进持久到期点，未扩展为自动重试机制。
- 诊断：`application.run` 保留开始、结束与父子关联，`wal.wait` 汇总等待；二者不充当超期业务触发全线程栈。慢业务与心跳滞后仍触发栈，正常队列入队／出队／释放及快速准入按采样窗口汇总；长准入、慢操作、异常和取消链路继续逐条记录。连续分卷、成本计量、丢失计数及脱敏保持。

实际复现先运行了旧代码：迟到／持久到期与生命周期四个场景观察到失败；第五个异步夹具在没有产生任何读取事件前等待，随后中断，改用真实 `ArchiveDatabase.read` 的同步准入路径。`scheduler-diagnostics-red.xml` 是此前一次收集器参数错误，不作为完成的业务红测证据。`diagnostics-synthetic-window.json` 保留旧生命周期样本：仅预期生命周期／等待也触发 4 条线程栈；候选相同初始采样 `active_spans=0`，随后专门注入超期业务和心跳滞后仍产生栈。三次真实同步读取的入队／出队／释放各汇总为一条 count=3，慢读与取消证据保留。

最终命令从工作树 `app/` 执行，进程 `TEMP`、`TMP` 均设为工作树 `work/tmp/read-path-repair/scheduler/`：

```powershell
& 'D:\claude_sandbox\pixiv-tracking-tool\.devtools\v012-env\Scripts\python.exe' -m pytest -q tests/test_scheduler_catchup.py tests/test_diagnostics.py tests/test_observation_transport.py::test_span_hierarchy_uses_enclosing_span_not_itself tests/test_observation_transport.py::test_fast_aggregation_slow_active_work_and_probe_failure tests/test_daily_management.py::test_interval_persists_without_changing_pause_or_active_operations -k 'not sqlite_commit_rollback_and_http' -o faulthandler_timeout=30 --basetemp ../work/tmp/read-path-repair/scheduler/green-02 --junitxml '../docs/审查/证据/2026-10-17-读取与调度修订/scheduler-diagnostics-targeted.xml'
```

结果：**12 passed、1 deselected，10.79 秒**。覆盖真实 APScheduler 晚到 2 秒及跨 3 周期只受理一次、自动暂停与重复去重、持久到期点和调度器重建、维护早退、同事务失败回滚后可重试、生命周期与慢业务抓栈边界、真实同步数据库准入和取消、原日志分卷／写入失败／隐私／父子关联与周期保存。唯一 deselected 是既有 HTTP TestClient 测试，避免本范围异步环境等待导致扩测；不代表该 HTTP 场景本轮通过。仅出现既有 Starlette/AnyIO 弃用警告。

首轮修复定向结果为 5 passed，14.34 秒，见 `scheduler-diagnostics-green.xml`；随后补齐维护早退／重复提交断言与新保存时间优先处理，再完成上述最终回归。`git diff --check` 本范围通过。没有运行全套、长测、EXE 或日常实例；真实日常负载、实际 EXE 重启和每天十点行为仍由最终包与后续日常体验复核，不据本结果宣布整体性能问题关闭。
