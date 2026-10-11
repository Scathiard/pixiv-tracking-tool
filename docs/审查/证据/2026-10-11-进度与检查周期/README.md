# 本轮证据

范围：`main / 6f26420` 的进度超时、周期保存报错及 `.8` 实际使用时后台无响应。仅保存合成回归与脱敏结果，不含真实图库、令牌或任务标识。

- `progress-diagnostic-red.xml`／`progress-diagnostic-green.xml`：固定阶段诊断的修前／修后回归；修后 3 项通过，不代表超时已解决。
- `package-smoke-result.json`：`.8` 同散列 EXE 在独立目录的 25 项基础检查；仓库 release 原路径的两次启动退出 -1，见交付记录。
- `package-daily-after.json`：`.8` 初次日常启动后五项只读 HTTP 成功。用户随后报告全面超时，这份记录只证明初次启动，不证明持续可用。
- `runtime-failure-summary.json`：后续故障、日志摘要和退出事实；进程 I/O 数是累计文件读写，不能解释成图库大小或磁盘实际吞吐。
- `progress-parent-red.xml`／`progress-parent-green-05.xml`：小型写放大复现及最终 4 项回归，修后 1.54 s 全通过；计数为同一合成清单的逻辑变更 84→39、SQL 86→19，非现场吞吐。

相同 SQLite 3.53.1 的只读任务快照曾为 7.061 ms（1 个当前任务、最近 20 项历史），两类任务记录主键命中；没有据此修改真实数据或扩大读取超时。该单次探针未复现慢点，查询收窄候选已撤回。

证据来源为本轮临时目录 `C:/Users/princ/AppData/Local/Temp/PixivTracker-repair-20261011-8/`；必要结果转存后，按用户新授权清理该目录，保留本目录及正式 release。后续写放大回归与新包结果在完成后追加，不预填成功。
