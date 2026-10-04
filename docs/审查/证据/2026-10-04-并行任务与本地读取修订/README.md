# 本轮验证证据

对应[修订记录](../../2026-10-04-并行任务与本地读取修订.md)，候选源码 `cc92f9aa03c55643dee399ef9090237349239952`。本目录不保存真实图库、配置或令牌，不自动清理。

| 文件 | 来源与边界 |
| --- | --- |
| `frontend-summary.json`、`frontend-browser.json` | `.devtools/test-runs/b20261004.3-frontend-03/`；96 项前端测试与 15 项实际 Vue 组件浏览器检查，响应为隔离替身 |
| `source-settings-1440.png`、`source-settings-390.png`、`source-settings-320.png` | 同批设置／日志截图，桌面 Chromium 的不同视口，不代表实体手机 |
| `independent-review.md`、`independent-review-tests.log`、`independent-review-callbacks.log` | `.devtools/test-runs/b20261004.3-backend-review-02/`；独立复审、43 项后端专项及最后 2 项回调探针 |
| `phase-statistics-before.json`、`phase-statistics-after.json` | `pipeline-02`／`pipeline-06` 的 `test_realistic_old_manifest_cl0/shape-evidence.json`；150,512 步的 SQL、扫描指令与单步时延定向对照 |

全套回归、大型对照、实际 8 小时持续运行及最终 EXE 证据尚待补入；本目录存在不代表已交付。中断的 `full-01` 和源码曾变化的 `reliability-01` 原位保留，仅作过程诊断。
