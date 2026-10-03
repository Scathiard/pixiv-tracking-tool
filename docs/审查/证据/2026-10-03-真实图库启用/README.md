# 真实图库启用证据

对应[启用记录](../../2026-10-03-真实图库启用.md)。此处只保存聚合结果与合成测试输出，不含真实数据库、图片、令牌或配置。

- `preflight.json`：真实旧库只读数量与任务状态。
- `backup.json`：升级前一致性备份的数量、散列及完整性核验。
- `offline-gate.xml`：10 项离线等待、恢复与大清单本地处理通过。
- `task-regression-initial.xml`：原回归 98 通过、6 个旧夹具失败；保留原结果。
- `shutdown-final.xml`：明确模拟联网前提后，6 个失败及相邻退出场景共 11 项通过。

前端 80 项状态测试与生产构建通过；依赖由锁文件一致的 `.4` 本地开发环境复制，未安装全局依赖。离线安装因本机缓存缺项未成功，随后采用上述本地依赖并实际构建通过。

- `exe-upgrade.json`／`exe-offline.json`／`exe-reading.json`／`exe-smoke.json`：`.5` 最终包的旧库升级、离线保留及日常阅览验证；截图为合成数据。
- `activation-receipt.json`／`real-upgrade-build5.json`：真实升级的聚合凭据和 `.5` 启用后 38 项检查。
- `settings-before.json`／`settings-after.json`／`settings-before-plan.json`：真实库严格只读的源码查询剖析，无作品明细。
- `synthetic-settings-before.json`／`synthetic-settings-after.json`／`settings-semantics.xml`：15,000 件合成规模的 SQL／计划／耗时与最终 7 项统计语义检查；规模运行未另产 JUnit，保留原始指标及本轮执行回报。
- `exe-smoke-build6.json`／`real-final-build6.json`：最终 `.6` 的 25 项包内冒烟和 38 项真实离线检查。
- `settings-performance.json`／`environment.json`：最终 EXE 的真实接口重复测量、统计对照及凭据／配置未改动边界。

`.6` 沿用相同前端与 `0003_revisions` 结构，仅改变统计查询；`.5` 的 34 项阅览结果不冒充为 `.6` 重跑，`.6` 另有实际图库浏览器和包内验证。源码、最终包、真实账号、实体手机及用户验收分别记录。
