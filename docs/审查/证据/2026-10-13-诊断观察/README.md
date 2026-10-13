# 2026-10-13 诊断观察证据

对应[取证方案与经验记录](../../2026-10-13-诊断观察与重写经验.md)。

- `observed-window.json`：本次实际读取的已封卷统计及当前卷可读性；只覆盖约四分钟，不是压力测试或整段运行统计。
- 原始分卷保存在 `D:\PixivTracker\data`，文件名与运行标识在摘要中。本目录不复制全套真实日志、图库、图片或凭据。
- 用户已批准实施：`backend-observation.xml`（3 项）、`observer-tests.xml`（7 项）、`settings-observation.xml`（1 项）及 `backend-test_*.jsonl` 是本轮合成验证；不含真实图库或凭据。
- `frontend-browser-evidence.json`：正式 dist、真实诊断接收器、1440px／390px Edge 关联结果；不代表实体手机或打包程序验收。
- `observer_cost_probe.py`、`observer-cost.json`：100 次合成温热 SELECT 1 的一次开关对照与日志自身开销，不能外推真实图库性能。
- 此目录是保留证据，不作为临时项清理；最终打包与启用证据完成后追加。
