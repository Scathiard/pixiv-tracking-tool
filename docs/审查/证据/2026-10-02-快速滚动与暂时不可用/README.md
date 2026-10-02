# 快速滚动修复证据

此目录只收同批合成验证的脱敏结果与代表性截图；不放真实图库、令牌或登录资料，不自动清理。结论和限制见[报告](../../2026-10-02-快速滚动与暂时不可用.md)。

- [旧实现复现](red-browser.json)：纯文本 503 解析错误、停止追加、静态图恢复失败；失败是预期红灯。
- [500ms 对照](red-delay500.json)、[2,000ms 对照](red-delay2000.json)：只变媒体响应等待，服务并发上限不变；没有实际 503。
- [新版故障恢复](green-browser.json)：35 项、1440／390／320px；有次数上限、原位恢复，0 失败／浏览器异常。
- [缓存回归](green-cache.json)、[动图回归](green-animation.json)：7／29 项，真实账号／图库均未使用。
- [前端测试](frontend-tests.txt)、[后端回归](backend-tests.txt)：57／359 项通过。
- [旧报错画面](red-list-error.png)、[桌面原批重试](green-desktop-retry.png)、[320px 恢复和永久失败占位](green-320-static.png)：永久失败卡片是故意设置的对照，不能把其占位解释为修复失败。

可复现入口为 `app/frontend/tests/api.test.ts`、`app/tests/client_fast_scroll_browser.py --artifacts 新目录 --recovery-suite`；使用项目现有 Node24、Python3.12 和 Playwright，临时目录只能取主仓库 `.devtools/test-runs/b10-*`，每次新建，不重复使用会覆盖证据的路径。原始运行及额外帧截图保留在该目录。

独立 EXE 结果待构建后补充；原始运行目录为 `b10-fast-scroll-red-20261002-02`、`b10-fast-scroll-red-delay500-20261002-03`、`b10-fast-scroll-red-delay2000-20261002-05`、`b10-fast-scroll-green-20261002-06`、`b10-cache-green-1`、`b10-animation-green-1`。失败的初始化与早采样运行也原位保留，不清理。
