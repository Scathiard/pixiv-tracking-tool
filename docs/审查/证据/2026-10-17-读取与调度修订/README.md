# 读取与调度修订证据

用途：保存 R-071／BUG-108、114 的定向复现、修复核对、有限读取对照和 EXE 启用结果；范围以功能路线图的 2026-10-17 获批执行包为准。

文件按 `scheduler-`、`diagnostics-`、`settings-`、`progress-`、`gallery-`、`package-`、`enabled-` 命名。只保留脱敏结果和必要复核脚本，不放令牌、真实库、原作或依赖缓存。这些交付证据长期保留；临时数据统一进入工作树 `work/tmp/read-path-repair/`，完成后核对归属再清理。

- `scheduler-diagnostics-validation.md`、`diagnostics-synthetic-window.json` 与对应 XML：调度、持续分卷与探针减负的定向结果。
- `settings-progress-validation.json`：配置／统计隔离、并行完成反馈和前端检查；统计 SQL 本身未优化。
- `gallery-results.md`、`gallery-focused-tests.txt`：状态语义、VM 工作量与两项旧断言缺口。
- `gallery-real-baseline.json`、`gallery-real-candidate.json`：同一页真实只读对照；旧侧中断，不伪造完成时间。
- `failure-categories-narrow.json`：用窄父摘要选择后再读取失败项；`read-plan-and-failures.json` 保存此前宽查询中断。
- `idle-wakeup-final.xml`：提交后通知、重试、依赖与退出共 6 项通过。
- `gallery-read-comparison.py`、`failure-category-read.py`：只读复核脚本；先核对原路径再使用，不启动程序或提交账号任务。

候选生产前端构建：`index-CbVcoYAA.js`、`index-BiyP4cED.css`；类型检查和构建退出 0。最终包和界面结果随交付补充。

最终结果：`package-smoke.json` 为最终 EXE 合成验证，`package-browser.json` 与三张设置截图为实际浏览器交互；`enabled-reads.json` 和 `enabled-launch.json` 为日常原图库启用。首次启动权限错误保留于 `enabled-first-launch-error.txt`，不得只看早先失败的启动 PID。`package-build.log` 为实际构建日志。归档脚本反映执行时的目录；复核旧基线须使用报告所列旧提交，不能将更新后的 main 当作旧实现。
