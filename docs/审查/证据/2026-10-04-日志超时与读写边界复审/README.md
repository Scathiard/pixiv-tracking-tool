# 日志超时与读写边界复审证据

- 基线：`main / 6caefe6`，`0.2.0 · 20261004.2`，日期 2026-10-04。
- [主报告](../../2026-10-04-日志超时与读写边界复审.md)管理判断与限制，候选执行范围在[功能路线图](../../../功能路线图.md)。
- `manifest.json` 列出本轮 24 份原样副本的来源、大小和 SHA-256；本目录不自动清理。
- 脚本原样保留取证时的路径约定；复跑使用 manifest 的 `.devtools/test-runs/` 来源路径，在仓库根运行。不要在本证据目录直接运行或覆盖结果。

| 材料 | 范围与限制 |
| --- | --- |
| `initial-http.json`、`probe.py` | 日常实例只读接口探测；仅保存状态、时间与活动数，不保存作品、账号、日志正文或令牌。复跑会产生真实读取负载，不能重复启动长日志请求 |
| `runtime-after-client-timeout.txt` | 现有日常进程调用栈，不含局部变量；线程 ID 不能替代 HTTP 请求关联 ID |
| `log-cost-report.md`、`log-cost-summary.json`、`log-*-warm.json` | 纯内存合成 SQLite，数据库工作量、字段与完整输出对照；暖读时间不代表真实硬盘或 EXE 的改善 |
| `log-small.json` | 初始小样本，含冷初始化影响，不用于宣传提速比 |
| `log_read_cost_probe.py`、`candidate-semantics-result.json` | 查询候选仅在诊断进程内替换；现行应用文件未改。3 条合成日志合同通过，不等于整个应用回归 |
| `frontend-audit.md`、`frontend-boundary-*` | 实际请求函数及提取的组件脚本，用界面／HTTP 替身复现竞争；不是完整 Vue 页面或手机体验测试 |
| `chromium-native-timeout*` | 私有 Chromium 空白页确认原生 `signal timed out` 文案；未访问真实应用／账号页面 |
| `server-disconnect-*` | 实际设置路由与真实本机 HTTP；可控读取替身、安全上限 1.5 秒，客户端断开后函数仍活动，最终正常退出；没有运行真实 SQLite 查询 |
| `architecture-audit.md`、`lock_order_probe.py`、`lock-order-result.json` | 两线程纯内存，刻意缩小连接池验证锁顺序缺陷；不能认定线上此刻死锁 |
| `remote_scope_probe.py`、`remote-scope-result.json` | 实际生产代理与客户端方法、无网络传输替身，证明任务上下文未进入；不推算真实主站任务总耗时 |

本轮没有修改应用源码、真实图库结构、配置或登录信息，没有提交收藏、下载、暂停、重启或强制关闭操作。现行包仍待可靠性修订；本目录的“passed”可能表示成功复现缺陷，不能解释为故障已修。

补充原始 HTTP 工具输出：一次原生客户端 `/api/status` 为 `200 / 0.010807s`，`/api/logs` 在 `31.008985s` 超时且未收到正文。该输出作为本轮工具观察记录，未伪装成第二份自动采集 JSON；独立 Python 探测结果见 `initial-http.json`。
