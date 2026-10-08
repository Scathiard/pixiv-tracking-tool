# 最小 pytest 收集诊断

2026-10-08。用于区分先前 Windows access violation 与 pytest 收集、原工作目录因素；不运行业务测试。

- 本目录仅保留 `test_imports.py`、运行脚本、两次独立 stdout/stderr 与实际子进程结果；不覆盖旧证据、不自动清理。
- 目标文件仅导入 zoneinfo、alembic、gallery_dl，测试函数只用于收集，不执行；不导入项目 src/conftest，不使用 Runtime、真实配置、数据库、令牌、账号或网络任务。
- 使用既有 `.devtools/v012-env/Scripts/python.exe`，开启 faulthandler，`-B -m pytest --collect-only -q`；显式 `--confcutdir` 指向本目录，保留正常插件及原环境，不安装依赖或调整全局设置。
- 严格两次新子进程：第一次 cwd 本目录，第二次 cwd 当前工作树 app；目标始终是本目录的同一文件。每次最多 30 秒；超时记录后终止该纯收集进程。
- 两次后停止；阴性不代表排除原异常，也不代表业务验证通过。不加 debugger/injection、不关闭异常报告。
