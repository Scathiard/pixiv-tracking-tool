# 2026-10-16 诊断运行现状证据

2026-10-17 保留状态更新：用户已明确要求清理旧诊断日志。按 `log-cleanup-manifest.json` 清理 700 卷共 47,485,441,693 字节，逐项结果见 `log-cleanup-result.json`；报告直接引用的 4 卷及本目录证据保留。下文“不删除”描述观察当时的操作，不能解释为现在仍保留全部原始卷。

用途：只读检查 `0.2.0 / 20261013.4` 运行后的进程、日志和任务推进，供现状报告与重写经验使用。

- 原始日志留在 `D:\PixivTracker\data`，不删除、搬迁或复制整套日志。
- 本目录保留分析脚本、脱敏统计和有明确范围的事件节选；报告须标明抽样和完整读取的区别。
- 本轮不修改程序、数据库、设置或账号状态，不启动或重启日常实例，不跑压力测试。
- 文件按观察用途命名；这些是报告证据，不作为临时文件清理。

报告：[诊断运行现状](../../2026-10-16-诊断运行现状.md)。检查从 16 日晚开始，整理跨至 17 日零点。

| 文件 | 范围 |
| --- | --- |
| `inspection-context.json` | 进程、端口、磁盘、窄范围 Windows 事件和调度源码核对；由本轮工具结果汇总 |
| `sample_run.py`、`run-boundaries.json`、`sample-*.json` | 全部 314 卷首尾各 256 KiB 抽样，不能作为全程事件分布；摘要区分生命周期与业务活动 |
| `tail-review.py`、`tail-summary.json`、`tail-findings.md` | 完整末卷约两分钟：请求、停止现场、探针自身计量 |
| `incident-review.py`、`incident-summary.json`、`incident-findings.md` | 完整首两卷：约 111 秒图库与 244 秒设置请求的关联证据 |
| `slow-read-log.json`、`dated-slow-reads.json`、`ordinary-log-tail.json` | 普通日志固定字段提取；前者含历史，第二份才是本轮时间范围 |
| `read_task_state.py`、`persisted-tasks.json`、`automatic-checks.json` | 只读任务状态、自动检查与结算分组；后两组查询另次执行保存于 automatic-checks |
| `persisted-tasks-initial-read-error.json` | 首次受限环境读取失败；随后获准宿主只读核对成功，不覆盖失败证据 |
| `read_failure_state.py`、`failure-categories.json` | 细项失败分类的只读查询超过分析脚本五秒预算而主动中断；没有获得分类结果 |

`sample-summary.json` 从已保存的抽样 JSON 汇总；首尾时间按未去重的分卷标记修正。`application.run`／`wal.wait` 为生命周期与空闲等待，不计入业务最长活动摘要。脚本不需要启动程序；只读 SQLite 查询应在原数据文件可读取的环境执行，不转为写连接。
