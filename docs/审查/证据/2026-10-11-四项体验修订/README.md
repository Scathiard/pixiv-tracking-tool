# 四项体验修订证据

对应[报告](../../2026-10-11-四项体验修订.md)，基线 `1d2640f`；仅保存合成验证与脱敏汇总，不保存真实图库、令牌或任务身份。

- `artist-navigation-results.json`：真实组件导航与主页核对完成反馈的红／绿复现。
- `gallery-summary.json`、`gallery-animation-*.json`、`gallery-paint-cpu.json`：动画重播、节点保留和短绘制对照；截图是本地合成颜色帧。
- `task-browser-results.json`、`task-320.png`、`task-390.png`、`task-1440.png`：当前阶段任务显示与手机顶栏几何；图中为合成图片区域，非真实图库。
- `packaged-results.json`、`packaged-smoke.json`、`packaged-390.png`、`packaged-1440.png`：最终 EXE 的合成基础检查和实际打包页面。
- `actual-activation.json`、`old-exit.json`：日常新进程、页面资源、登录与自动检查、局部读取和旧进程正常退出的脱敏记录；不保存真实作品列表。
- `backend-source-results.json`：来源核对实际合成任务链的 6 项窄回归。
- 实体手机和真实长动图连续浏览仍待用户复验；最终包与日常启动不构成整体后台超时已解决的证据。
- `cleanup.json`：本轮自建临时项约 95 MiB、依赖联接和浏览器调试残留已清理；共享依赖目录、交付物、正式证据及真实日志保留。
