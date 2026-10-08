# 两条直接计数只读诊断结果

- 来源：已关闭的 b20261004.3-reliability-20261008-10/check-workers-1/archive.db，维护 operation_id=8888b42d0d484697883dcac4f885a6bc；runtime_closed=true，psutil无匹配原试验/其他reliability进程，未见WAL/SHM/journal。
- 读取方式：mode=ro&immutable=1，只读原合成库；文件大小/mtime/sidecar元数据前后一致，未修改查询、索引、缓存、统计或生产/测试仓库文件。
- 运行环境：原 .devtools/v012-env/Scripts/python.exe，Python 3.12.13，SQLite 3.53.1；连接默认cache_size=-2000、page_size=4096、synchronous=2(FULL)，只读取这些值。
- 预算和退出：每条4.5秒、批次30秒；六次均完成，批次wholewall 7.218075300101191秒；真实子进程returncode=0，stderr为空，faulthandler启用，无访问异常。

| 精确原SQL | 自然计划 | 结果 | 首次wall / CPU秒 | 暖读1 / 暖读2 wall秒 |
| --- | --- | --- | --- | --- |
| SELECT count(*) FROM operation_items WHERE operation_id=? AND state IN ('succeeded','failed','skipped','not_executed') | SEARCH operation_items USING COVERING INDEX ix_items_candidate (operation_id=? AND state=?) | 19461 | 3.324213600018993 / 0.03125 | 0.00150969997048378 / 0.0011506997980177402 |
| SELECT count(*) FROM assets WHERE state='verified' | SCAN assets | 3133 | 3.785561600001529 / 0.03125 | 0.004644300090149045 / 0.0026263000909239054 |

- 任务计数近似VM：首次77,000，暖读各78,000；Windows进程逻辑read_bytes为1,351,680 / 0 / 0，read_calls为330 / 0 / 0。
- 资产计数近似VM：三次各48,000；逻辑read_bytes为2,834,432 / 2,822,144 / 2,822,144，read_calls为692 / 689 / 689。
- VM为每1000步progress handler采样，最后不足1000步不能计入，不能视作精确总指令数；CPU取process_time，Windows计时分辨率使暖读记录0.0，不表示完全不耗CPU。
- 判断：没有同期大任务时，两条查询的本进程首次读取已经各耗3.3/3.8秒，接近4.5秒门槛；后续暖读均为毫秒级，本轮没有复现ReadDeadline，也没有显示持续高CPU计算成本。
- 解释边界：首次之前已读EXPLAIN/连接元信息，且未清系统缓存，不能称物理冷盘；暖读同连接/无同期写入，不代表持续试验争用状态。进程逻辑读不等于物理磁盘读量；不能据此诊断磁盘故障或认定长测根因。
- 证据：preflight.json、result.json、process.json、stdout.log、stderr.log与count_diagnosis.py全部保留；没有追加试验或修改源码。
