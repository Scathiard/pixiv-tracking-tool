# 设置统计只读审查结果

结论：本轮不建议实现 EXISTS 候选或 32MiB 连接缓存。前者工作量只少约 5.4%，后者进程逻辑读取少约 12.4%，但正反顺序对照没有稳定墙钟收益；两者均保留初始宽作品／版本读取。不能据此解释或解决持续负载末次 4.498 秒设置 SQL 取消。

## 固定边界与事实来源

- 生产基线 9eb03bc；收尾时 HEAD=7fe93c2，`git diff 9eb03bc HEAD -- app/src app/tests` 为空，工作树无未提交源码／测试改动。
- 唯一打开的库为已经正常关闭的 `../b20261004.3-reliability-20261008-08/check-workers-1/archive.db`，605646848 字节；result.json 的 runtime_closed=true，打开前没有非空 WAL。
- 使用 SQLite `mode=ro&immutable=1`、`query_only=ON`，不构造 ArchiveDatabase，不启动 Runtime；immutable 只用于已关闭、checkpoint 完成的固定证据，不是生产连接建议。
- 每臂关闭后均确认主库大小与纳秒 mtime 不变；测量进程写 IO 为 0，全部子进程退出。没有清 OS 缓存，Windows 进程读 IO 不等于物理盘 IO。
- 原 SQL／参数取自同结构 07 证据中实际处理器捕获的语句；本轮在 08 库重新 EXPLAIN 和执行。完整 SQL、参数、实际计划及逐次数据见 measurement-01.json。

## 当前查询与必须保留的边界

源码入口：工作树 `app/src/routes/settings_api.py:60–101`。

1. 60–64：MATERIALIZED current_works 一次扫描 Works，并逐作品按默认版本主键读取 WorkRevision；投影虽窄，Works 的 default_revision_id 位于 caption／source_metadata 之后，WorkRevision 的 type/page_count 也位于 caption 之后（models.py:60–78、98–107），不能把窄投影当作窄磁盘记录。
2. 69–73、85–90：全局默认版本的 RevisionAsset.asset_id 与 legacy NULL 分支 UNION 去重，再按 Asset.work_id 聚合；计划包含整个 revision_assets 的 asset_id 索引扫描、UNION 临时排序、选中资产 ID 自动索引，以及整个 assets 的 work_id 索引扫描。其成本依作品、所有资产与绑定数量增长，并含去重／临时索引成本；不能断言已是 N²。
3. 99–101：suppressed 只过滤最终统计所属作品，不能提前从“哪些默认版本选择了资产”中剔除。
4. `test_settings_projection.py:27–53、75–89` 要求跨作品引用仍按资产所属作品计算、多个默认版本引用同一资产只计一次、suppressed 选择者仍能让未抑制所属作品计入资产、hidden 不排除统计、dangling 默认版本沿用作品形状但不变成 NULL legacy；verified 空路径、unknown 空路径、invalid、动图 timing、越界页面的旧行为也不变。

初始读取宽表／版本页和其溢出页造成读取等待是可验证假设；本轮没有页命中率／物理设备读取归因，不能写成确定根因。此宽表阶段是两种 SQL 的共同路径。

## 最小等价候选与实际计划差异

候选保留 current_works、全部 CASE、owner 形状限制、按 owner 分组及最终 suppressed/gap 规则，只把选中资产全局 ID 的 UNION JOIN 改成：

```sql
FROM assets JOIN current_works ON current_works.work_id = assets.work_id
WHERE ((current_works.revision_id IS NULL AND assets.revision_id IS NULL)
    OR EXISTS (SELECT 1 FROM revision_assets
        WHERE revision_assets.asset_id = assets.id
          AND revision_assets.revision_id IN (SELECT revision_id FROM current_works)))
  AND (原有 owner type／role／page_index 限制)
GROUP BY assets.work_id
```

等价理由：每一 Asset 主键行仅访问一次；EXISTS 表示该 ID 至少被任一全局默认版本引用，对应 UNION 的集合成员语义，保留跨作品／suppressed 选择者且不要求 RevisionAsset 的 role/page_index 等于 Asset。NULL legacy 分支与旧分支完全相同；同一资产同时满足两分支仍只有一行。分组、计数与 gap 未改。

实际计划：

| 路径 | 原 SQL | EXISTS 候选 |
| --- | --- | --- |
| 作品形状 | SCAN works + WorkRevision 主键 SEARCH | 完全相同 |
| 选中成员 | MERGE (UNION)，revision_assets 索引全扫、legacy assets revision_id SEARCH，TEMP B-TREE ORDER BY，再自动 asset_id 索引 | 每资产 CORRELATED SCALAR SUBQUERY，`SEARCH revision_assets USING INDEX ix_revision_assets_asset_id (asset_id=?)`，默认版本 LIST SUBQUERY／BLOOM FILTER |
| 资产访问 | SCAN assets USING ix_assets_work_id | SCAN assets USING sqlite_autoindex_assets_2（work_id/revision_id/role/page_index 唯一索引） |
| 最终计数 | GROUP、current_works 全扫、archive_intents 列表、owner 聚合 LEFT JOIN | 保留相同结构 |

候选没有缺 asset_id 索引的逐资产全表扫描，但单资产历史引用很多时仍有对应索引命中行数成本，不能推广为所有库都更快。

## 测量入口与结果

入口：现有隔离 Python 调用本目录 `measure.py --output measurement-01.json`；脚本不导入生产模块。四臂独立子进程串行，顺序为原/默认→原/32MiB→候选/32MiB→候选/默认，再完全反序；每臂第一读和同连接两次暖读，共 24 次。

SQL 进度回调每 1000 VM 指令检查 4.5 秒预算。24 次均已结束，最长实测 5.1498 秒；单个底层读取不受 Python 回调即时中断，因此不能把 4.5 秒描述为严格墙钟上限。另有每臂子进程 25 秒退出保护；本轮未触发。后续若追加测量，应以每查询独立进程加 10 秒硬截止，超过记 failed，不持续等待，本轮不为此再追加查询。

- 前序 12 次中 11 次取消，仅最后一次候选／默认完成（1.0142 秒）；取消 CPU 为 0.0781–0.3906 秒，墙钟 4.5004–5.1498 秒。它表明主要时间不在进程 CPU，但不能独立证明物理盘等待；取消查询的 VM 工作量也不能与完成查询比较。
- 反序 12 次全部完成；原/32MiB 首读 0.6400 秒，其余首读及暖读 0.1504–0.1607 秒。两次暖读对照如下，VM 为 1000 回调量子的下界，实际不足另加 1000；逻辑读为 Windows 进程计数。

| SQL/cache | 两次暖读墙钟秒 | CPU 秒 | VM 约百万 | 每次逻辑读字节 | 该臂峰值工作集 MiB |
| --- | --- | --- | --- | --- | --- |
| 原/默认 -2000 | 0.1574 / 0.1607 | 0.1563 / 0.1563 | 3.081 / 3.081 | 248549376 | 25.93 |
| 原/32MiB -32768 | 0.1603 / 0.1563 | 0.1563 / 0.1563 | 3.081 / 3.081 | 217821184 | 59.49 |
| 候选/默认 -2000 | 0.1578 / 0.1525 | 0.1719 / 0.1406 | 2.915 / 2.916 | 249180160 | 25.64 |
| 候选/32MiB -32768 | 0.1533 / 0.1504 | 0.1563 / 0.1563 | 2.915 / 2.916 | 218451968 | 58.93 |

13 次成功完成的五项结果完全一致：downloaded_pages=5137、registered_originals=15002、verified_originals=5160、unverified_originals=9833、missing_originals=9。11 次取消没有结果，不能称 24 次结果通过。

候选减少约 5.4% VM，却默认缓存逻辑读多约 0.25%；32MiB 缓存使原 SQL 逻辑读少约 12.36%，但没有暖读墙钟收益，前序也未保证完成。扩大缓存带来约 33.56MiB 单进程峰值差；如七个读连接均设 -32768，建议页缓存目标总计 224MiB（原 -2000 总约 13.67MiB），还不含 ORM、临时表、Python 等内存，亦不是进程内存硬上限。本轮不建议采用。

## 等价验证与未解决项

若以后仍要实施候选，需先把相同两种 SQL 放入既有 6 作品异常夹具双跑，完整比较五项结果并保留现有 HTTP / EXPLAIN 断言，再补空库、NULL legacy 与版本绑定同时命中、多个跨作品选择者／suppressed owner、历史绑定密集的有限合成对照；不改数据约束和验收预算。本轮没有新建库或运行该测试，只做源码边界审查及关闭证据库结果对照。

当前没有已证实能改善日常长测的局部查询／缓存改动。读写并发、PASSIVE 周期与短 IO 交互、宽表读页成本在本轮静止只读模型中没有直接归因，仍待父会话按其他同期证据判断。这里不调整超时、不建议移动存储、不修改任何生产或现有测试文件。

## 追加：两项窄覆盖索引的只读设计评估

这是待用户授权的新增索引候选，不是已实施或已验证的性能修复；本轮仅读取 sqlite_master / table_info / index_list，完整元数据见 schema-01.json，没有执行 DDL。

建议评估的列组为 `works(default_revision_id, pixiv_illust_id, type, page_count)` 和 `work_revisions(id, type, page_count)`。不改变原统计 SQL。

- Works 的 id 是 `INTEGER PRIMARY KEY`，固定证据库也确认为普通 rowid 表；SQLite 二级索引携带 rowid，第一项已覆盖 current_works 需要的 Work.id 与其他四列，因此形态表扫描可以只读取这个窄索引。无需把 caption/source_metadata 放进索引，也无需另增显式 id 列。
- WorkRevision 的 id 是字符串主键，其既有主键自动索引只含 id 和 rowid，不含 type/page_count；第二项可让 `LEFT JOIN work_revisions ON id=default_revision_id` 成为覆盖 id/type/page_count 的等值索引读取，避免为了两个形态字段再回宽表。此处不读 WorkRevision.work_id，因此索引也不需要添加它。
- 当前模型及实际证据库没有等价覆盖项：Works 有 pixiv_illust_id 唯一自动索引、default_revision_id/latest_revision_id 单列、(type,posted_at,id)、(posted_at,id)、(artist_id,posted_at,id)；WorkRevision 有 id 主键、(work_id,ordinal) 唯一自动索引、work_id 单列及 (work_id,ordinal) 命名索引。已有项均缺少所需列组，不能消除两处宽表取列。
- 普通非唯一、非 partial 的覆盖索引包含 NULL，不改变 NULL 默认版本、dangling 旧引用、COALESCE fallback 或跨作品错误版本引用的既有结果。必须保留按版本 id 的 LEFT JOIN，不能为利用索引额外加 WorkRevision.work_id=Work.id 或把 NULL 改成内连接；没有改过滤顺序、集合成员资格或 owner 归属语义。
- 即便有覆盖项，规划器是否选择以及实际页读取降幅仍需实测。期望计划是 `SCAN works USING COVERING INDEX ...` 与 `SEARCH work_revisions USING COVERING INDEX ... (id=?) LEFT-JOIN`；本轮无新索引，不能伪造该计划或保证它必然出现。
- 代价是首次构建扫描和额外索引空间，新增／更新对应作品列和版本列需要维护索引，故需比较创建耗时／字节、资料更新事务成本、同 SQL 完整结果及新计划；不能只报告纯读取改善。适当的后续验证是获批后复制固定合成库到新目录，保留原库、只在副本创建两项索引，按相同有界串行冷暖顺序与当前查询比较，再验证异常语义和真实持续负载。
- 当前 248549376 字节约 237.99MiB 是整个原聚合的进程逻辑读取，未分解成形态、资产和临时表各自读量；不能承诺两项索引消除全部这部分读取。
- 后续 `derivatives.count` 超时来自长测仪器取证，不是本项设置统计 SQL；不为该仪器查询专加生产索引，也不把它当作两项覆盖索引必然改善的证据。

这两项覆盖索引是比已否定的 EXISTS/cache 更直接针对共同宽表形态路径的合理候选；父会话负责新增索引范围、用户授权、兼容升级和后续验证，本代理在此冻结，只交付设计评估。

源码 SHA-256：settings_api.py=`e5e5fc0d567708c8b1c3bda1aa4cc7cbcb464524d9d86a59495b22f81b4d0246`；test_settings_projection.py=`06ae0bacd04e87db3c61318c89fd1d270e6ead9a1ac3d34f1631519a321e6741`。
