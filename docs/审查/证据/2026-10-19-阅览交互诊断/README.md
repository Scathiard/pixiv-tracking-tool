# 2026-10-19 阅览交互诊断证据

基线 `main / 30f8613`，日常 EXE `0.2.0 / 20261017.2`，运行 `973f0fc8794d`。这是只读日志分析和合成复现，不是修复、真实账号测试或新包验收。

- `log-summary.json`：最近 0039／0040／0041 三分卷的事件计数与接口耗时，13 次收藏刷新、3 次收藏写入受理、2 次游标 409。
- `log-events.jsonl`：499 条允许字段的脱敏请求／浏览器事件；不含 URL 查询、令牌、账号内容或图片。
- `media-events.jsonl`：同会话目标时段的 201 条媒体事件；浏览器时间与服务端收录时间分别保留，不能交叉相减计算延迟。
- `bookmark/slow-refresh-trace.json`：刷新受理 6094 ms，其中本地写锁等待 5672 ms。
- `bookmark/check-lock-holder.json`：同期后台检查的一次写事务 5828 ms，正文 3578 ms、64 项投影 2125 ms；该事务结束后刷新请求取得写锁。
- `navigation/README.md`、`*-result.json`：导航回放范围、结果与未完成尝试。
- `bookmark/execution-results.json`：已执行收藏回放的观察结果，由本轮工具输出整理；不是重新运行，也不是生产账号结果。

脚本按执行时原样保留。为避免在证据目录生成新库，重放时先复制到仓库根的 `work/tmp/reading-ux-20261019/` 对应位置（`bookmark/`、`navigation/`），创建这些临时目录后从仓库根执行；脚本中的相对层级依赖这一位置。复用已有依赖，不安装全局工具。

```powershell
node --experimental-vm-modules work/tmp/reading-ux-20261019/bookmark/probe.mjs
.\.devtools\v012-env\Scripts\python.exe -B work/tmp/reading-ux-20261019/bookmark/backend_probe.py
node work/tmp/reading-ux-20261019/navigation/sfc-replay.cjs
.\.devtools\v012-env\Scripts\python.exe -B -u work/tmp/reading-ux-20261019/navigation/library-replay.py
```

前三个命令在本基线上应退出 1，表示复现了预期交互缺陷；最后一个退出 0，表示观察序列符合“正常／失效／恢复／再次失效／无关检查时间写入不失效”。这些源逻辑回放使用真实业务模块与合成网络／媒体，不是 DOM 交互或真实手机验证。

本轮临时根仅放自建脚本、结果和小型合成库；归证据后清理，不保留或提交合成数据库。原有测试改动、旧包、真实日志、真实图库及在用 EXE 解包目录均未清理。
