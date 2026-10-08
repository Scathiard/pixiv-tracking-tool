# 两项设置形态索引的隔离副本对照

用途：用户已批准的两项窄覆盖索引合成验证；仅此目录保存脚本、四份独立副本及机器结果。原始 08 失败库与此前证据原样保留，不自动清理、不覆盖旧输出。生产和测试由另一执行者负责，本目录不导入或修改它们。

当前阶段：收到主会话开始通知后已完成唯一一次四臂测量；20 子进程和 runner 全退出0，12 次查询完成/12 次取消，未重跑。report.md 为完整结论，run-01/result.json 为机器结果，run-01/process-exits-02.json 为退出复核；原始 08 证据不变，副本全部保留。

唯一源库：../b20261004.3-reliability-20261008-08/check-workers-1/archive.db；固定运行结果 runtime_closed=true、无非空 WAL，约 606MB。固定 SQL 与参数来自 ../b20261008-settings-query-review-01/measurement-01.json 的 baseline，不使用 EXISTS 改写、不扩大连接缓存。

四臂：baseline / works_only / revisions_only / both；每臂从同一源库独立复制，候选名称和定义严格为：

```sql
CREATE INDEX ix_works_settings_shape ON works(default_revision_id,pixiv_illust_id,type,page_count);
CREATE INDEX ix_revisions_settings_shape ON work_revisions(id,type,page_count);
```

协议：所有操作串行；先复制四库，复制时计算源流 SHA-256，再分别流式回读目标文件并与源流摘要比较；复制与目标回读校验的耗时、CPU、逻辑 IO、工作集分别记录，摘要不等就停止。随后分别建索引并记录建索引 CPU、墙钟、进程逻辑 IO、工作集、主库页数变化及 dbstat 索引占用字节。正常关闭写连接后核对无 WAL，以 mode=ro&immutable=1、query_only=ON 运行不变统计 SQL。四臂正序和反序各一遍，各臂使用独立子进程且保留同连接第一读和两次暖读，共 24 次查询。每次记录 EXPLAIN、完整五项结果、VM 指令量子上下界、CPU/墙钟、逻辑 IO、Windows 工作集（驻留内存）/峰值。

每查询进度回调保留原 4.5 秒预算；父进程从 query_started 标记起设 10 秒硬截止，超过即终止该臂并记 failed，不无限重试。连接／计划准备和子进程退出有 30 秒截止；复制单库 180 秒、每臂建索引 180 秒、写成本臂 30 秒硬截止，超时副本保留且不再用于读取。

完成读取后，对每臂一个相同 ID 的作品与其默认版本，在独立写子进程中做三轮“小事务更新 type/page_count、默认引用换成 NULL，再以第二事务恢复原值”；保持 synchronous=FULL，逐事务分开记录 DML、commit 和总耗时、VM、IO、工作集。这只是有关列变更的定向合成成本，不模拟整个资料观察网络／派生链，也不宣称微小耗时差就是写入回归。最后确认值已恢复，正常关闭并 checkpoint。

不清 OS 缓存，不把进程读 IO 当成物理盘 IO；复制／目标回读校验／建索引会预热 OS 缓存，“第一读”仅表示新进程／新连接首读，不称冷盘读取。顺序和首次/暖读分别记录。不得触及真实账号、图库、token、配置或启动全维护。索引大小不能用主库增长简单代替，因为原库自由页可能被复用；优先 dbstat，每项不可用则明确记录缺口。

命名：probe.py 为入口，protocol.json 为准备期固定输入，run-01/ 为唯一首轮输出（已存在即停止）；四份副本在 run-01/copies/ 下，子进程日志与 result.json 保留。后续轮次使用全新目录，不覆盖、不删除。使用主仓库现有 .devtools/v012-env/Scripts/python.exe；运行后由父会话审查结果和门槛，不能用本项替代长测或 EXE 验收。
