# v0.2.2 隔离验证证据

统一说明和通过/未验收范围见 [实施记录](../../2026-09-27-v0.2.2接口与实施记录.md)。仅含合成数据的结果和截图，没有真实图库、令牌或账号写入。

- `pytest-release-06.xml`：最终 172 项回归；旧 XML 保留供追溯，不作为最终结果。
- `source-smoke-01.json`：源码运行检查；不能替代 EXE。
- `browser-r4.json`：R4 布局与浏览器流程的较早构建证据。
- `touch-390-final.json`、`touch-320-final.json`、`bookmark-http-fallback.json`：最终前端构建的触摸、输入与模拟收藏往返。
- `load-final.json`：5000 件作品、2000 页单作品、后台下载及故障结果。
- `memory-measurement-control.json`：测试等待器保留 DOM 句柄的单变量对照；最终负载改用纯值轮询。
- `exe-browser-final.json`、`exe-smoke-final.json`、`exe-*.png`：最终 EXE 的独立服务、浏览器与合成图片截图。
- `build-manifest.json`：实现提交、构建环境、打包活动资源、EXE 与证据散列。

测试入口在对应提交的 `app/tests/` 与 `app/frontend/tests/`；详细私有测试工作目录为 `.devtools/test-runs/v022-*`，不自动清理。
