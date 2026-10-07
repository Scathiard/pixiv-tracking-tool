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

全套回归、大型对照、实际 8 小时持续运行及最终 EXE 证据尚待补入；本目录存在不代表已交付。中断的 `full-01` 和源码曾变化的 `reliability-01` 原位保留，仅作过程诊断。
