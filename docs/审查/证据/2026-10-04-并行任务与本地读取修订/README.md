# 本轮验证证据

对应[修订记录](../../2026-10-04-并行任务与本地读取修订.md)，候选源码 `cc92f9aa03c55643dee399ef9090237349239952`。本目录不保存真实图库、配置或令牌，不自动清理。

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
| `discovery-observation-validation.md`、`discovery-observation-result.json`、`discovery-observation-red.xml`、`discovery-observation-related.xml`、`discovery-observation-final.xml`、`discovery-observation-*-metrics.jsonl` | `b20261008-discovery-observation-01`；真实发现作品处理器的事实事务从 3 到 1，先红后绿；新增 21 项及既有 54 项最终全部通过。主会话按 XML 身份核对原两项新夹具失败的复验，非一次全套通过；账号／版本／删除、派生、回滚与提交后缓存保持，仅合成输入 |

| `storage-analysis.json`、`storage-comparison.json`、`storage-manifest.json` | `reliability-storage-20261008-15`；同合成数据库与 SQL 在 C: SSD／D: HDD 的定向对照及临时副本保留位置，不代表完整 APP 或真实库迁移验收 |

| `storage-inventory.json` | 用户要求核对后仅枚举真实图片目录的文件长度及数据库文件长度；没有读取图片／数据库内容，94.48 GB 图片与 0.823 GB 数据库分开统计 |

全套首次完成结果及五项失败的定向修订复验分开记录；大型对照、实际 8 小时持续运行及最终 EXE 证据尚待补入。本目录存在不代表已交付。中断的 `full-01` 和源码曾变化的 `reliability-01` 原位保留，仅作过程诊断。
