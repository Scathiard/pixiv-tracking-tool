# 外部报告核验的隔离证据

本目录保存本次自行编写的合成探针、输出和同包只读代码核验；不包含用户真实图库、令牌、完整外部报告或新程序包，不自动清理。

- `queue-probe.py`、`queue-result.json`：源自 `.devtools/test-runs/20261003-external-audit-queue/`；生产队列同来源公平性、SQLite 3.53.1 大清单查询与探针内候选查询对照。
- `conflict-probe.py`、`conflict-result.json` 和场景 JSON：源自 `.devtools/test-runs/20261003-external-audit-conflict/`；页数差异、旧缺页补齐和迁移内容比较。
- `packaged-migration-disassembly.txt`、`packaged-migration-summary.json`：从构建 20261003.3 的代码对象读取，未执行包内模块。

脚本按原样存档，`ROOT = parents[3]` 依赖原运行目录层级；不得直接在此证据目录执行。复现时在当前仓库 `.devtools/test-runs/` 下创建一个全新子目录，将对应脚本复制过去；从 `app/` 使用 `.devtools/v012-env/Scripts/python.exe` 执行队列脚本，或 `python -m pytest <新目录>/conflict-probe.py -q --basetemp <新目录>/pytest-data`。`pytest-data` 必须是全新路径，避免 pytest 清理旧结果。原目录内已有数据库，不直接覆盖或重跑。

复现源码基线为 `45563fe`；当前脚本依赖该基线的生产代码与合成测试夹具。汇总中的十个现有专项名称列在 `conflict-result.json`，其输出汇总为 21 passed / 55.43 秒；本次未执行全部项目回归。3 秒是慢查询探针的中止预算，不是生产超时或自然完成时间；候选 SQL 不是修复后的产品。
