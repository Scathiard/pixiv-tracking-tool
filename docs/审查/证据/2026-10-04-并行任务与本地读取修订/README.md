# 本轮验证证据

对应[修订记录](../../2026-10-04-并行任务与本地读取修订.md)，不同源码基线及验证边界分别记录。本目录不保存真实图库、配置或令牌，不自动清理。

| 文件 | 来源与边界 |
| --- | --- |
| `frontend-summary.json`、`frontend-browser.json` | `.devtools/test-runs/b20261004.3-frontend-03/`；96 项前端测试与 15 项实际 Vue 组件浏览器检查，响应为隔离替身 |
| `source-settings-1440.png`、`source-settings-390.png`、`source-settings-320.png` | 同批设置／日志截图，桌面 Chromium 的不同视口，不代表实体手机 |
| `independent-review.md`、`independent-review-tests.log`、`independent-review-callbacks.log` | `.devtools/test-runs/b20261004.3-backend-review-02/`；独立复审、43 项后端专项及最后 2 项回调探针 |
| `phase-statistics-before.json`、`phase-statistics-after.json` | `pipeline-02`／`pipeline-06` 的 `test_realistic_old_manifest_cl0/shape-evidence.json`；150,512 步的 SQL、扫描指令与单步时延定向对照 |
| `account-publication-result.json`、`account-publication-validation.md` | `b20261008-account-05`；74 项账号发布与相关回归，含 9 项专项，只使用合成凭据；原红反例保留在 `account-03` |
| `reliability-fixed-pilot-20261008.json`、`reliability-fixed-pilot-http-20261008.jsonl` | `b20261004.3-reliability-20261008-02/check-workers-1`；固定源码单路检查、原规模独占短测，120/120 HTTP 成功，但小任务提交超出 2 秒，整体仍失败；不是八小时持续验收 |
| `reliability-normative-pilot-20261008.json`、`reliability-normative-analysis-20261008.json` | `b20261004.3-reliability-20261008-03`；`9a467c7` 规范版本／时序图库，117/120 HTTP 成功，3 个设置超时；小任务受理及检查／下载／预览重叠成立，尚未通过整体验收；完整原始轨迹留在原测试目录 |
| `planning-fairness.md`、`claim-progress-merge.md` | `b20261008-planning-fairness-01`；保留规划挤占就绪核对的红绿证据、真实 ZIP 提交次数对照与回滚／并行验证；原有限重试测试的输入契约后由主会话修正，见后者 |
| `reliability-seed-contract.json`、`reliability-instrument-selfcheck.json` | `reliability-seed-selfcheck-20261008-05`／`reliability-instrument-final-20261008-06`；规范版本／时序结构与无多余新版本的小型自检、计时采样及持续覆盖守卫验证，不等于大规模通过 |
| `snapshot-sharing-related.xml`、`snapshot-sharing-final.xml`、`snapshot-sharing-final-result.json` | `b20261008-read-sharing-03`／`05`；44 项相关回归、最终 23 项专项；GET 在途共享与写后／账号／取消边界，无真实账号或图库 |
| `settings-isolated-20261008.json`、`resource-peaks-selfcheck-20261008.json` | `reliability-settings-isolated-20261008-07`／`resource-freeze-20261008-10`；完整静止读取及真实资源峰值封装自检，不等于维护中的响应时间 |
| `reliability-shared-pilot-20261008.json`、`operation-window-red-20261008.json` | `reliability-20261008-04`／`window-fairness-01`；共享读取后 120/120 HTTP 成功但一次小任务受理仍超标，以及第 65 个操作饥饿／暂停父图平方复制的独立红证据 |
| `operation-window-fix-20261008.json`、`operation-window-validation-20261008.md` | `b20261008-operation-fairness-fix-01`；最终 21 项专项及受影响回归、此前 29 项相关回归，红反例另保留 |
| `commit-wait-attribution-20261008.json`、`checkpoint-ab-20261008.json`、`checkpoint-reverse-20261008.json` | 诊断 run 11／12／14；真实等待分段及固定合成 SQL 的检查点对照，逆序组先同步副本；保留吞吐代价和边界，不等于正式任务通过 |
| `checkpoint-regression-red.xml`、`checkpoint-regression-green.xml` | `b20261004.3-checkpoint-regression-01`；小周期复用先红后绿，41 项数据库／恢复／读取相关回归通过；256 页候选随后撤回，属于实验结果 |
| `checkpoint-default-regression.xml` | `b20261004.3-checkpoint-regression-02`；撤回参数候选后，默认策略下的旧快照、WAL 复用、回滚和重开等 6 项数据库专项通过 |
| `byte-progress-http-red.xml`、`byte-progress-http-green.xml` | `b20261008-byte-progress-http-01`；实际任务在两次 GET 之间仅更新内存字节数，旧入口误共享旧值的反例先红；加入字节世代后全 24 项在途共享专项通过 |
| `byte-progress-validation.md`、`byte-progress-result.json` | `b20261008-byte-progress-01`；13 项字节专项、24 项相关回归及最后 14 项复验；旧退出测试需真实等待共享下载准入，保留原冷却及最终核验要求 |
| `observation-merge-validation.md`、`observation-merge-result.json`、`observation-merge-final.xml`、`observation-merge-metrics.jsonl` | `b20261008-observation-merge-07`；35 项实际处理器专项及同等正确事实的提交／WAL 对照；仅合成输入，不作墙时结论 |
| `task-test-seams.xml` | `b20261008-fairness-test-seams-01`；3 项既有清点与领取后容量测试修正注入位置后通过，生产逻辑未随之更改；此前两处失败在 `observation-merge-06` 的旧四文件基线也能复现 |
| `full-03-result.json`、`full-03.xml` | `b20261004.3-full-03`；固定 `b351dee` 完整执行 753 项，748 通过、5 失败，保留原结论；正式副本仅去除 XML 行尾空白，原始文件原位保留 |
| `filter-runtime-contract.xml` | `b20261008-filter-contract-01`；为三项旧 Runtime 替身补入正式读取边界需要的同一合成 database 后，原筛选断言 3 项通过；未修改生产代码 |
| `test-boundaries-validation.md`、`test-boundaries-targeted.xml`、`test-boundaries-final.xml` | `b20261008-test-boundaries-01`；清点与执行分层、内存预算进程隔离的 2 项定向及所属两文件全 20 项通过，2000 页及原 300 MiB 上限保留 |
| `full-03-rechecks.json` | 主会话逐一对照全套原 5 个失败用例与复验 XML；五项全部复验通过、生产代码未改，不表示第二次全套运行 |
| `reliability-program-only-check-1.json`、`reliability-program-only-check-2.json`、`reliability-program-only-comparison.json` | `b20261004.3-reliability-20261008-05`；固定 `e67f490` 在 D: 独占运行的两组规范规模短测，各 120 秒、120 个 HTTP 全成功；两组短测门槛均通过，仍有提交长尾，第二名检查者未改善整体延迟，继续保留 1 名 |
| `reliability-failfast-selfcheck.json` | `b20261004.3-reliability-instrument-failfast-20261008-16`；1000 件／1024 步的仪器自检，实际 HTTP 后显式注入失败；长测模式 2.56 秒后正常收尾仍判失败，短测仍跑满 12 秒，两组均正常关闭。不是大型验收，八小时和完整维护门槛未放宽 |
| `reliability-program-only-soak-failed.json`、`reliability-program-only-soak-http.jsonl` | `b20261004.3-reliability-20261008-06/check-workers-1`；同规模长测约 50 秒重现 7 个设置及 1 个图库 504，72/80 HTTP 成功后按失败收尾；原 SQL／完整轨迹保留在原目录。没有完成八小时或完整维护，服务和 Runtime 均正常关闭 |
| `checkpoint-owner-auto.json`、`checkpoint-owner-passive.json`、`checkpoint-owner-followup.json`、`checkpoint-owner-comparison.json` | `reliability-checkpoint-owner-20261008-17`；同一合成来源在 D: 上的有界机制模型，加入保存途中定时读。默认组 57／64 步后预算失败，独立组另续测完整 64 步；两组正常关闭、重开核验通过。仅共同前 57 步的提交可作前缀对照，不比较总吞吐；候选仍有读停顿和 WAL 增长，未改正式连接策略 |
| `checkpoint-noop-capability.json` | `checkpoint-noop-probe-20261008-18`；当前 SQLite 3.53.1 在一个全新微型合成库上确认 NOOP 不合并帧，FULL 保持、重开通过；仅能力探针，不是性能或完整程序验收 |
| `checkpoint-restart-gap-red.xml` | `b20261008-checkpoint-owner-implementation-01`；PASSIVE 后受控插入真实前台提交，RESTART 前出现 10 个未回写帧；反驳“零锁等待就不会持写锁整理”的假设，候选已决定弃用 RESTART。此文件是失败证据，不是最终候选回归结果 |
| `checkpoint-runtime-validation.md`、`checkpoint-runtime-hashes.json`、`checkpoint-runtime-final.xml`、`checkpoint-runtime-start-red.xml` | 同批正式 Runtime 候选：29 项整理专项及 55 项受影响回归共 84 项通过；主会话核对全部 XML 身份、零失败／错误／跳过及 5 个冻结文件散列。初始失败 XML 的正式副本仅去除一处行尾空格，原件原位保留。独立静态复审通过，尚未完成大库、八小时或 EXE 验收 |
| `checkpoint-runtime-pilot.json`、`checkpoint-runtime-pilot-http.jsonl`、`checkpoint-runtime-pilot-samples.jsonl` | `b20261004.3-reliability-20261008-07`；固定 `05e3c30`、规范规模独占 120 秒短测，120 个 HTTP 全成功，小操作受理均 < 0.3 秒；实际 owner/FULL 策略、压力探测及正常退出已记录。维护全轮和八小时未完成，媒体为微型合成输入 |
| `full-04-result.json`、`full-04.xml` | `b20261008-full-04`；固定 `05e3c30` 完整执行 803 项，802 通过、1 失败，1071.76 秒；运行中／后生产散列均与前项短测相同。设置失败后的后继 GET 未到读取入口；下项记录测试边界修正，原失败不覆盖；正式 XML 副本仅去行尾空白，原始日志／XML 原位保留 |
| `discovery-observation-validation.md`、`discovery-observation-result.json`、`discovery-observation-red.xml`、`discovery-observation-related.xml`、`discovery-observation-final.xml`、`discovery-observation-*-metrics.jsonl` | `b20261008-discovery-observation-01`；真实发现作品处理器的事实事务从 3 到 1，先红后绿；新增 21 项及既有 54 项最终全部通过。主会话按 XML 身份核对原两项新夹具失败的复验，非一次全套通过；账号／版本／删除、派生、回滚与提交后缓存保持，仅合成输入 |
| `storage-analysis.json`、`storage-comparison.json`、`storage-manifest.json` | `reliability-storage-20261008-15`；同合成数据库与 SQL 在 C: SSD／D: HDD 的定向对照及临时副本保留位置，不代表完整 APP 或真实库迁移验收 |
| `storage-inventory.json` | 用户要求核对后仅枚举真实图片目录的文件长度及数据库文件长度；没有读取图片／数据库内容，94.48 GB 图片与 0.823 GB 数据库分开统计 |
| `sharing-fixture-resolution.md`、`sharing-fixture-red.txt`、`sharing-fixture-green.txt`、`sharing-fixture-regression.xml`、`sharing-fixture-close-probe.py`、`sharing-fixture-baseline.py` | `b20261008-sharing-failure-01`；真实 HTTP 关闭时序反例确认失败 PATCH 连接干扰后继 GET；只隔离注入失败请求后同探针通过，相关 24 项全部通过。原全套未捕获 Future 异常，不将反例当作逐字证明；生产代码及原时限／断言未变。正式文本副本仅去行尾空白，原件保留 |
| `checkpoint-runtime-soak-failed.json`、`checkpoint-runtime-soak-failure-context.json` | `b20261004.3-reliability-20261008-08`；固定 `9eb03bc` 实际 9191.125 秒后失败，8200 个 HTTP 中 7 个设置 504，后继驱动简单统计也超时；正常收尾、源文件未变。原始 16MB 事件轨迹及 SQL 留在原目录；未完成整轮维护/八小时，不构建交付 |
| `settings-query-readonly/` | `b20261008-settings-query-review-01` 的冻结只读对照：保留脚本、24次测量、实际索引和审查。13次有结果且一致、11次预算取消；不支持改写SQL/扩大缓存。目录只存本次证据、不自动清理；未执行索引DDL |
| `settings-index-tests/` | 获批两条设置索引的源码专项；按 `01`～`03` 保留原始日志、XML、复验入口和摘要，不复制临时合成数据库、不自动清理。`01` 为 28 项通过；`02` 为 3 项 XML 通过但出现导入层异常且缺独立退出码；`03` 原样复验 3 项、真实退出码 0 且输出干净，保留 `02` 未归因边界 |
| `settings-index-comparison/` | 获批两索引的首次四臂受控对照；保留脚本、协议、报告、结果及进程退出复核，合成副本与全部逐臂日志仍留原 test-runs，不自动清理。24 次读取中 12 次完成且一致、12 次预算取消；作品索引被采用、版本索引未采用，不能据此交付 |

以下新增目录按批次保存原始文本、脚本、测量与散列清单；不复制合成数据库，不自动清理：

- `settings-shape-query/`：`b20261008-settings-shape-query-01` 的只读窄版本投影对照；12 次中 4 次完成且一致、8 次取消，结构读取量改善不等于整体通过。
- `settings-projection-tests/`：`b20261008-settings-projection-tests-01` 的 62 项相关回归与两万条历史／一万五千件作品指标；保留真实退出码和导入异常 stderr，不隐藏异常。
- `import-diagnosis/`：`b20261008-import-diagnosis-01` 的三个纯导入进程、有界后续收集及单次纯文件探针（`collection/`、`file-probe/`）；均未复现，原异常未归属，只用于解释测试环境异常边界，不代替程序验证。
- `settings-full-regression/`：`b20261008-full-05` 的完整后端回归、源码前后身份、真实退出码与分离日志；816 项全部通过，保留收集阶段两段访问异常，不能称为无异常输出。
- `settings-index-pilot/`：`b20261004.3-reliability-20261008-09` 固定 `ef797cb` 的原规模 120 秒短测及外层退出记录；120 次 HTTP 全成功，小操作最慢 0.415 秒，Runtime／HTTP 正常退出。全部原始轨迹仍留 test-runs；这里只存结果、HTTP／样本与驱动日志，不是八小时或完整维护验收。
- `settings-index-soak-failed/`：同一源码的 `10` 长测于 5942.375 秒失败；5280 个 HTTP 全成功，两条直接诊断计数超时、stderr 两段未归属原生异常，正常收尾。保存结果、外层退出、HTTP／样本、异常原文和原始路径散列清单；完整 SQL／时间线留在原 test-runs，不复制合成库。
- `soak-count-diagnosis/`：`b20261008-soak-count-diagnosis-01` 的有界同 SQL 只读对照，六次均在 4.5 秒内，首次 3.324／3.786 秒、暖读毫秒；没有复现超时，不当作修复证明。不复制源库、不自动清理。
- `native-exception-observer/`：`b20261008-native-exception-observer-01` 的有界新建进程树诊断；保存观察器、原规则、修订、四轮事件／退出记录与散列清单，不复制缓存或合成数据，不自动清理。两次实际收集各 816 项均未出现 Python AV；捕获到的控制台 RPC 异常不能解释旧 Python 转储，原异常仍未归属。
- `reliability-observation/`：验收工具的观测修订证据，保存旧耦合反例、定向测试、独立复审、小型实际 Runtime 自检及源码散列，不复制临时库或缓存，不自动清理。小型自检仅验证仪器接线，不能替代 15,000 件和八小时验收；具体运行状态以目录内记录为准。
- `observed-pilot/`、`observed-soak-interrupted/`、`observed-soak/`：固定 `d1187f0` 的短测 `11`、意外中断长测 `12` 与用户取消的长测 `13`；`11` 通过原规模 120 秒门槛，`12` 缺最终结果和正常退出证明，`13` 在 3572.813 秒正常收尾，3915 次 HTTP 全成功，但没有完成八小时或完整维护。用户已取消该长测前置门槛；原始结果不改判，详细轨迹仍留原 test-runs，按目录清单保存结果及散列，不复制合成库，不自动清理。

全套首次完成结果及五项失败的定向修订复验分开记录；大型对照、实际 8 小时持续运行及最终 EXE 证据尚待补入。本目录存在不代表已交付。中断的 `full-01` 和源码曾变化的 `reliability-01` 原位保留，仅作过程诊断。

- `packaged-minimal/`、`real-activation/`：20261008.1 一次最小 EXE 检查及获批真实应用，保留自检观察器竞态的原记录；真实数据库未复制进仓库，备份留在日常 data/upgrade 下，体验待用户共同复验。
