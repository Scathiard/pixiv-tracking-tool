# 本轮评估证据

对应[实现架构重新评估](../../2026-09-20-实现架构重新评估.md)，基线 `master / 3705098ff64791c2f6d6f89a149696e8cb0f5506`，源码 `0.1.1`。目录只存本轮审查材料，不作为产品实现或第二套测试入口；后续正式回归应按批准的执行包进入 `app/tests/`。

## 文件说明

| 文件 | 用途与边界 |
| --- | --- |
| [pytest.txt](pytest.txt) | 本轮现有测试输出：58 passed，354 warnings；警告主要涉及依赖弃用接口与无时区 UTC 时间 |
| [browser-smoke.txt](browser-smoke.txt) | 本轮现有浏览器冒烟输出，最终 `BROWSER_SMOKE_OK`；部分子进程中文输出存在编码显示问题 |
| [architecture_probes.py](architecture_probes.py) | 离线本地操作、实际 JobManager 去重、事件循环阻塞对照和合成查询实验；只操作临时数据 |
| [architecture-probes.json](architecture-probes.json) | 上述实验的实测结果及每项限制 |
| [browser_race_probe.py](browser_race_probe.py) | 使用现有浏览器 fixture 与实际主页模板，按明确事件顺序释放两次筛选响应 |
| [browser-race.json](browser-race.json) | 后请求显示 18 件，先请求随后把结果覆盖为 6 件；不依赖随机网速 |
| [stale-filter-response.png](stale-filter-response.png) | 乱序响应后的合成图库截图；已目视核对，无真实作品 |
| [protected-before.json](protected-before.json) | 验证前的 69 项文件路径、大小和 SHA-256，不含凭据内容 |
| [protected-verification.json](protected-verification.json) | 结束时与以上快照逐项比较；覆盖范围见下文 |
| [document-verification.json](document-verification.json) | 本轮维护文档的 UTF-8 和相对文件链接核对 |
| [communication-append-verification.json](communication-append-verification.json) | MSG-20260920-001 使用追加写入；原有 3,913 字节前缀散列保持一致 |

## 如何复核

现有回归在 `app/` 执行：

```powershell
py -3.12 -m pytest -q tests
py -3.12 -B tests/browser_smoke.py
```

审查实验在仓库根执行：

```powershell
py -3.12 -B docs/审查/证据/2026-09-20-实现架构重新评估/architecture_probes.py
py -3.12 -B docs/审查/证据/2026-09-20-实现架构重新评估/browser_race_probe.py
```

后两项会重新写本目录的同名结果，重现历史证据前应另存当次结果。`architecture_probes.py` 在导入业务代码之前安装临时 `src.config`，使用系统临时目录；设置写入接口主动拒绝调用，Requests 的外部请求被阻止。浏览器实验只访问临时本机服务并拦截非本地请求，关闭自己启动的服务与浏览器。脚本没有读取根令牌或实际运行 `.env`；临时库、合成图片和运行缓存不自动删除。

原 pytest 和原浏览器脚本遵循当前 `app/tests/README.md` 的隔离约定。本轮在运行前只检查真实开发配置中是否存在 `DATA_ROOT` 键，未输出配置值；测试使用临时数据根。没有运行真实 Pixiv 操作、数据迁移或 release 构建脚本。

## 覆盖与证据强度

重新评估覆盖当前配置／启动、认证与 Pixiv 客户端、Tracker、文件索引、删除记录、迁移、模型、任务与调度、Web／安全、全部路由类型、主要模板交互、依赖与构建入口，以及现有测试的职责。比较接口的独立分析聚焦最小接口、扩展模型和常见调用三种方案。本目录不提供“所有文件每行均已重新审阅”的统计声明。

现有 58 项测试通过只说明原有断言成立。新实验分别证明：

- 实际路由在无 Tracker 时跳过本地删除；同一隔离库的取消隐藏成功，作为对照。没有运行真实应用的登录生命周期或 EXE。
- 当前 JobManager 按相同去重键合并不同 kind；路由静态核对确认下载和刷新确实使用相同画师键。
- 在实际异步主页路由的同步调用处注入 200ms 延迟会阻塞事件循环；同步路由线程池对照不造成相同停顿。这不是实际数据库延迟基准。
- 实际主页在先响应延迟时会应用旧结果；只覆盖本次主页、类型筛选与 Chromium 场景，不泛化为所有页面都已复现。
- 查询实验只比较 SQL 形状：非空时间、热缓存、7 次运行、不含 ORM／API／UI、总数和位置计数。临时 SQLite 表／索引不属于产品 schema 迁移。

以上是缺陷或方案论据，脚本退出 0 不等于产品修复通过。新架构的完整性能、真实 Pixiv 兼容性、用户体验与 EXE 交付仍待实施后验证。

## 文件保护范围

本次快照为 `rg --files --hidden app release data images` 按当前忽略规则列出的文件，加根 `pixiv_token.json`。共 69 项：60 个 `app/` 文件、7 个 release 文件、1 个根数据文件、1 个根令牌文件。仅计算散列，不输出凭据内容。

该清单**不覆盖被忽略的真实 `.env`、release 私有配置、外部图库或所有缓存**，不能沿用旧审查的“83 项”口径。未覆盖不表示改动；本轮工具操作没有对这些配置或真实图库发起写入。核对结果保存在 `protected-verification.json`；文档与本证据目录属于本次允许的新增／维护范围。

官方资料链接及选型推论位于报告相应段落，文件路径、测试结果和基线均来自本次本地检查。旧审查只是历史问题索引，其已复现问题不在本轮自动标成重新复现或修复完成。
