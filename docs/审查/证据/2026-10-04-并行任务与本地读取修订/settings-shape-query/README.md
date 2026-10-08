# 版本形态窄投影只读查询对照

用途：一次有限查询对照，原索引四臂报告与结果冻结不变。唯一打开数据库为 ../b20261008-settings-index-comparison-01/run-01/copies/both.db；核对无非空 WAL，mode=ro&immutable=1、query_only=ON。不得复制库、DDL、ANALYZE、INDEXED BY、改生产或扩造大库。

状态：唯一一轮已结束并冻结，十二次中四次完成/八次取消，无结果不等、无硬超时；父/四子退出0、库size/mtime保持。完整结论见 report.md，机器结果见 run-01/result.json，父退出码见 parent-exit-01.json。

原 SQL 保持不变；候选仅追加 `revision_shapes AS MATERIALIZED (SELECT id,type,page_count FROM work_revisions)`，将 current_works 的 LEFT JOIN 与 COALESCE 引用改为该窄投影；成员 UNION、owner 归属、suppressed 最终过滤及五项统计保持。

按原→候选→候选→原串行四个子进程，各新连接首读及两次同连接暖读，共 12 查询；每次进度预算4.5秒、父进程硬截止10秒，无无限重试。记录真实计划、结果、VM量子、进程逻辑IO、CPU/墙钟/Windows工作集，父子退出码。首读不称冷盘，逻辑IO不称物理盘IO，不清系统缓存。

候选扫描所有历史版本，成本随总版本量增长；本轮仅固定库，不额外生成历史负载。材料按 run-01 独立存放，已存在即停止；原库文件大小/mtime结束复核，日志和结果保留，不自动删除或覆盖。协议与probe.py仅此目录；report.md交父会话决定是否落实，不把查询改善等同长测通过。
