# 本轮证据

诊断版交付补充：`diagnostic-checks-02.xml` 为 8 项开启探针专项；`package-10-*` 保存再次失败的包内／实际读取和退出初次等待结果；`package-11-*` 为全面诊断初版的包内、真实读取、线程／SQL摘要及正常退出；`package-12-daily-after.json` 为最终诊断版的实际启动与源码位置核对。`.12` 仅调整打包调用位置，没有重复 `.11` 的包内 25 项；最终 EXE 使用实际日常启动和日志检查。完整原始诊断日志保存在 `D:/PixivTracker/data/diagnostic-*.jsonl`，不自动清理。

范围：`main / 6f26420` 的进度超时、周期保存报错及 `.8` 实际使用时后台无响应。仅保存合成回归与脱敏结果，不含真实图库、令牌或任务标识。

- `progress-diagnostic-red.xml`／`progress-diagnostic-green.xml`：固定阶段诊断的修前／修后回归；修后 3 项通过，不代表超时已解决。
- `package-smoke-result.json`：`.8` 同散列 EXE 在独立目录的 25 项基础检查；仓库 release 原路径的两次启动退出 -1，见交付记录。
- `package-daily-after.json`：`.8` 初次日常启动后五项只读 HTTP 成功。用户随后报告全面超时，这份记录只证明初次启动，不证明持续可用。
- `runtime-failure-summary.json`：后续故障、日志摘要和退出事实；进程 I/O 数是累计文件读写，不能解释成图库大小或磁盘实际吞吐。
- `progress-parent-red.xml`／`progress-parent-green-05.xml`：`.9` 中间候选的小型写放大复现及 4 项回归；逻辑变更 84→39、SQL 86→19，非现场吞吐。该候选实际仍失败，时间戳快路径随后撤回。
- `progress-event-green.xml`：`.10` 源头修正的 4 项最终任务回归，覆盖进度落盘、候选不扩散、事件时间、暂停、重试及重启。
- `progress-settings-green.xml`：所选版本覆盖索引查询的 3 项既有设置回归；未执行历史规模测试。
- `package-9-smoke-result.json`、`package-9-daily-after.json`、`package-9-daily-residual.json`：`.9` 包内基础检查通过，实际设置仍 504；后继进度超时见报告。
- `package-9-active-scale.json`、`package-9-daily-stop.json`：活动任务 15,431 项／15,316 候选的只读结果及正常退出记录。
- `settings-candidate-real-read.json`：后台仍大量重写时的候选统计查询超时，保留失败，不提前记为实际性能改善。

相同 SQLite 3.53.1 的只读任务快照曾为 7.061 ms（1 个当前任务、最近 20 项历史），两类任务记录主键命中；没有据此修改真实数据或扩大读取超时。该单次探针未复现慢点，查询收窄候选已撤回。

证据来源为本轮临时目录 `C:/Users/princ/AppData/Local/Temp/PixivTracker-repair-20261011-8/`；必要结果转存后，按用户新授权清理该目录，保留本目录及正式 release。后续写放大回归与新包结果在完成后追加，不预填成功。
