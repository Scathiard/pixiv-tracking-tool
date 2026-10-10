# 验证记录

- 状态：隔离验证通过，日常启用复核失败；不是已验收交付。
- 版本：0.2.0 · 20261010.1；源码 `9ab6c275d0048d4f3d3d13643e5012e106035ed5`，构建时干净。
- EXE：75,142,609 字节，SHA-256 `19b2cc3ffd1ab77bedf442646765b4ba866450ec8810bb8ab92b306f0a48c10f`。
- 环境：Windows 11、Python 3.12.13、PyInstaller 6.20.0；schema 0003_revisions，无本轮升级。
- 包内 26 项基础检查通过，真实打包前端在 1440／390px 的 12 项隔离回放通过，无浏览器异常；隔离 EXE 正常退出。
- 日常 `D:\PixivTracker` 无测试参数启动后出现启动等待；就绪后 startup/progress 可读，library/settings 均约 4.5 秒返回 504，因此继续定向修复。
- 保留本包与现场失败证据，不覆盖 EXE；未公开发布，真实手机和用户体验未验收。

证据：[包内检查](../../../.devtools/test-runs/20261010-performance-report/packaged/smoke.json)、[浏览器回放](../../../.devtools/test-runs/20261010-performance-report/packaged/browser/results.json)、[日常读取失败](../../../.devtools/test-runs/20261010-performance-report/daily-after-startup.json)。
