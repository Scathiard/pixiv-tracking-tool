# 2026-10-13 诊断观察证据

对应[取证方案与经验记录](../../2026-10-13-诊断观察与重写经验.md)。

- `observed-window.json`：本次实际读取的已封卷统计及当前卷可读性；只覆盖约四分钟，不是压力测试或整段运行统计。
- 原始分卷保存在 `D:\PixivTracker\data`，文件名与运行标识在摘要中。本目录不复制全套真实日志、图库、图片或凭据。
- 用户已批准实施：`backend-observation.xml`（3 项）、`observer-tests.xml`（7 项）、`settings-observation.xml`（1 项）及 `backend-test_*.jsonl` 是本轮合成验证；不含真实图库或凭据。
- `frontend-browser-evidence.json`：正式 dist、真实诊断接收器、1440px／390px Edge 关联结果；不代表实体手机或打包程序验收。
- `observer_cost_probe.py`、`observer-cost.json`：100 次合成温热 SELECT 1 的一次开关对照与日志自身开销，不能外推真实图库性能。
- `package-*.json`、`daily-*.json`：`.2` 中间包的空库和日常现场；真实首页 504 与诊断父级错误保留，不作为最终 `.3` 的性能结论。
- `hierarchy-observation.xml`：`.3` 唯一新增的诊断父级专项，1 项通过；`final-package-*.json`：最终 EXE 启动、HTTP、上报、正常退出和层级记录核对。
- `activation-first-*.json`：`.3` 首次正常启动未恢复登录及退出证据；`final-daily-*.json`：一次正常重启后实际就绪、登录、自动检查与诊断记录，分别保留，不覆盖成一次启动即成功。
- `previous-daily-exit.json`、`intermediate-daily-exit.json`：先前 `.1`／`.2` 实例正常退出；`cleanup.json`：本任务清理范围与保留的当前运行目录。
- 此目录是保留证据，不作为临时项清理；真实业务超时仍开放，不代表整体性能或用户体验已验收。
