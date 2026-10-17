# 两条超慢读取的关联证据

范围：完整解析同轮 `part0001` 与 `part0002`，分别 257,501 / 250,280 行，均 0 坏行；两卷大小 67,366,594 / 67,359,296 字节。未扫描其他卷、读真实数据库、联网、启动程序、跑测试或修改业务。固定字段链路及来源 SHA-256 见 [incident-summary.json](incident-summary.json)，复核脚本见 [incident-review.py](incident-review.py)。本文件时间均为北京时间，JSON 保留 UTC。

## 已确认事实

### 图库首屏读取

关联 trace=`6e368863ea0b`；HTTP `/api/library/cursor` 在 2026-10-13 22:01:56.465 完成，状态 200、耗时 111,297 ms；对应本地读在 22:01:56.407 完成，outcome=ok、总耗时 110,844 ms。

| 层次 | 耗时 |
| --- | ---: |
| 读许可排队 | 0 ms |
| 线程派发等待 | 16 ms |
| 连接获取 | 93 ms |
| database_ms | 110,719 ms |
| 19 条语句的 execute_ms 合计 | 105,172 ms |
| library_works 阶段 | 3,281 ms |
| library_bookmarks 阶段 | 2,610 ms |
| library_states 阶段 | 103,890 ms |
| task_winners.execute | 102,937 ms |
| task_winners.consume | 16 ms，240 行 |
| work_states.execute | 672 ms |
| work_states.consume | 265 ms，240 行 |
| library.apply_states | 0 ms |

这些计量存在包含关系，不能逐行相加。主要语句指纹 `3e3844ff2f2c15fe`，定义记录为 WITH，调用位置 `src/core/normalized_task_projection.py:230:winners`。提取的引用名包括实体表 `read_operation_scope`、`read_task_item_fact`、`read_task_parent`、`read_task_scope_best`、`read_task_work_parent`，以及 `active/candidates/ranked/resolved/selected/wanted` 等 CTE 名；不是 SQL 原文。

该语句自 22:00:12.509 的 execute.begin 起，至 22:01:55.451 的 sql.slow / execute.end 结束，耗时 102,937 ms，约占本地读总耗时 92.9%。51 次 watchdog 观测都关联到该 SQL 指纹，最后一次已持续 102,062 ms；22:00:13.654 的工作线程 `asyncio_2` / 29724 完整栈顶部为 `default.py:952:do_execute`，经过 `normalized_task_projection.py:230:winners` → `projection_maintenance.py:846:read_work_states` → `library.py:391:_hydrate_cards`。之后消费该结果仅 16 ms。

### 设置读取

关联 trace=`ffd35ba4d335`；本地读自 2026-10-13 22:08:38.823 至 22:12:43.089，总耗时 244,266 ms；HTTP `/api/settings` 于 22:12:43.091 返回 200，记录总耗时同为 244,266 ms。

| 层次 | 耗时 |
| --- | ---: |
| 读许可排队 / 线程派发 / 连接获取 | 均 0 ms |
| database_ms | 244,141 ms |
| 14 条语句的 execute_ms 合计 | 244,125 ms |
| settings.counts | 33,125 ms |
| settings.totals.execute | 211,000 ms |
| settings.totals.consume | 0 ms |
| settings.interval | 0 ms |

计数语句指纹 `b3fb9c492e150fff`，SELECT、引用 `works`，定义调用位置 `src/routes/settings_api.py:58:snapshot`；22:09:11.942 的 sql.slow 记 33,125 ms。16 次 watchdog 观测关联该指纹，最后已持续 31,703 ms；22:08:40.289 的同一工作线程完整栈顶部为 `default.py:952:do_execute`，调用链到 `settings_api.py:58:snapshot`。

汇总语句指纹 `75047a4365732b03`，WITH、提取引用名 `archive_intents/assets/current_works/revision_assets/work_revisions/works`（含 CTE 名），定义调用位置 `src/routes/settings_api.py:108:snapshot`。执行阶段自 22:09:11.944 至 22:12:42.952，sql.slow / execute.end 记 211,000 ms；105 次 watchdog 观测关联该指纹，最后已持续 210,297 ms。22:09:12.556 的完整栈同样停在 `default.py:952:do_execute`，调用链到 `settings_api.py:108:snapshot`。消费汇总结果为 0 ms。

## 证据支持的判断

- 这两次延迟主要发生在数据库语句执行调用内；读许可排队、连接获取和工作线程派发无法解释约 111 秒 / 244 秒等待。
- 图库读取的主要慢点是 240 件作品的任务状态赢家查询；设置读取的主要慢点是作品计数和资源汇总两个查询。两者均有 trace、阶段 begin/end、sql.slow、sql.definition 和工作线程栈交叉对应。
- 结果消费确有少量时间（图库作品/收藏阶段也曾出现 fetchone/fetchall 栈），但三条主要慢 SQL 的结果消费不是这些长延迟的主因。
- 这两次最终返回 200，说明取消固定期限后它们确实完成；不能把“最终完成”写成性能合格或 BUG-108 已修复。

## 仍不知道的事

`database.py:107–137` 将 SQL 计时挂在 before_cursor_execute / after_cursor_execute，消费结果另有 span；所以可定位为 cursor 执行调用的等待。但 Python 的 `do_execute` 栈不能继续区分 SQLite 内部扫描/排序、执行计划、物理磁盘等待、锁或其他并发活动。没有 query plan、原生采样或同负载对照，不能由此断言机械盘、某个索引、探针或某类锁就是根因。

源码对照：`app/src/core/database.py:107`、`app/src/core/read_control.py:82`、`app/src/core/normalized_task_projection.py:224`、`app/src/core/projection_maintenance.py:834`、`app/src/routes/settings_api.py:56/107/109`。没有实施修复。
