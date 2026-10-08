# 有界纯导入诊断

用途：追查既有 pytest 收集阶段的 Windows access violation 输出；本目录仅保存纯导入脚本、三个独立子进程的 stdout/stderr、时序及真实退出码。

命名：import-01/02/03 对应同一脚本的顺序新进程；result.json 为汇总。使用本项目 `.devtools/v012-env/Scripts/python.exe`，开启 faulthandler，最多三次，仅导入 zoneinfo、alembic、gallery_dl 及诊断所需标准库；不导入项目源码、Runtime、配置、账号或数据库，不发网络请求。

每次子进程限 30 秒；超时会记录并终止该无数据库、无业务任务的纯导入进程。保留原始异常输出，不禁用异常报告，不安装或修改依赖、不修改系统配置。结果原位保留，不自动删除；后续更深入对照由主会话另行决定。
