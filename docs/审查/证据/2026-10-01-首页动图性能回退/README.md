# 首页动图性能回退证据

当前范围：BUG-086，基线 main/e7bff64，公开版本 0.2.0、内部构建 20261001.1。状态以同级报告和交付清单为准。

- baseline-final / candidate-final：同合成 GIF、80 件作品、固定 30 次真实滚轮、AB/BA 两轮，最终同脚本对照；包含 CPU、帧间隔、请求与滚动暂停采样。
- size-only-control / original-decode-control：只缩小尺寸的控制及原尺寸解码迹线摘要；原始 rAF、完整迹线仍在主仓库 `.devtools/test-runs/b9-perf-*`，不清理。
- mobile-original-control / mobile-size-control：旧前端固定，在 390/320px 仅改图尺寸；最终候选同时覆盖 1440/390/320px。
- animation-browser / cache-browser：故障恢复、离屏／模拟后台、601 件连续浏览与本地收藏；仅合成数据。
- backend-first-failed：保留三个旧契约／新增 SQL 回退的首轮失败；library-targeted 保留根因修正后原查询预算与新批量测试。
- source-smoke：实际本机下载器、1280×960 原 GIF 和 360×270 动画缓存，详情仍读取原 GIF。

真实库仅只读聚合 GIF 的尺寸与字节数，没有账号操作或真实库写入；手机尺寸模拟不能代替实体手机验收。失败采样和原始运行保留，不把不同轮次的数字混作同一次实验。
