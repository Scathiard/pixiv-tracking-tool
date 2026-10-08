# 持续试验两条直接计数的只读最小诊断

- 用途：判断正常关闭的 -10 合成失败库上的两条精确原计数是否在无同期任务时本身就重；不是重新运行大型负载。
- 来源：b20261004.3-reliability-20261008-10/check-workers-1/archive.db 与 result.json，原库和原证据保持不动。
- 前置条件：result.runtime_closed为true，没有原试验活动进程及sidecar；不满足时停止，不强行immutable。
- 读取：mode=ro&immutable=1；原SQL/索引/SQLite缓存/统计保持，仅EXPLAIN与两个原计数各首次和后两次暖读。
- 预算：每条4.5秒，整个查询批次最多30秒；合作式progress handler和连接interrupt计时，不强制终止进程或清理缓存。
- 指标：结果、自然计划、完整wall/CPU、1000步采样的近似VM工作量、Windows进程read_bytes逻辑读量差分；进程逻辑读不等于物理硬盘读量。
- 缓存局限：第一次只是这个新进程/连接的首次读取，操作系统缓存未清空；后两次为同连接暖读，不代表持续读写争用。
- 保留：脚本、前置核验、stdout/stderr、真实退出码和结果全部保留，不自动删除；不修改生产或测试仓库文件。
