# 纯导入三次诊断结果

2026-10-08。本轮已完成并停止，没有启动项目、数据库、配置、令牌或网络任务。

## 已观察到

- 既有 projection-tests-01/stderr.log 含两次 Windows access violation，栈停在 importlib 的路径 stat/get_data，经 gallery_dl 到 src.auth；同批 stdout 显示 62 passed，process.json 记录实际子进程 returncode=0。
- 既有 index-tests-02/pytest.log 也含两次该输出，栈经 zoneinfo 到 Alembic；末尾 3 passed，但原 Python 退出码没有单独记录，外层管道退出 0 不足以证明它实际退出 0。index-tests-03 精确独立复验记录实际 returncode=0、3 passed，且没有该异常输出。这些 Python 栈是报告发生时的位置，不能据此确定本机原生故障模块或故障指令。
- 本轮三个新进程均开启 faulthandler，依次纯导入 zoneinfo、alembic、gallery_dl；均有完成标记，真实 exit=0，无超时、无 access violation 输出。总进程墙钟分别 1.413、1.074、1.354 秒。
- Python 为 CPython 3.12.13、MSC v.1944、64 位 AMD64；venv 和底层 Python 均为本项目 .devtools 下既有运行时，未调用系统 Python。
- zoneinfo 从 `.devtools/python/cpython-3.12-windows-x86_64-none/Lib/zoneinfo/` 加载，原生 `_zoneinfo.pyd` 位于同运行时 DLLs；Alembic 1.20.0、gallery_dl 1.32.1 均从 `.devtools/v012-env/Lib/site-packages/` 加载。
- 三次 Alembic 导入 0.694–0.943 秒，gallery_dl 0.053–0.112 秒，zoneinfo 0.014–0.032 秒。完成时未加载任何 src 项目模块。stdout/stderr 分别保留；stderr 的 PROBE_PHASE 是诊断时序标记，不是异常。

## 解释边界

纯导入条件未复现，不能据三次干净结果宣布原异常无害。导入次序、工作目录、pytest 收集/插件、进程先前加载状态及间歇原生异常均尚未区分；没有证据归咎某个依赖、硬盘或执行平台。

CPython 3.12 官方 faulthandler 实现为 Windows 安装 vectored exception handler：它打印 access violation 和 Python 栈后返回 EXCEPTION_CONTINUE_SEARCH，后续处理者仍可能处理该异常。因此“打印 fatal 字样而最终 exit=0”在处理顺序上可能发生；原两批究竟是不是已处理的 first-chance 异常，本轮没有原生 exception address/module 或处理者记录，仍只是待证假设。

参考：https://github.com/python/cpython/blob/3.12/Modules/faulthandler.c （faulthandler_exc_handler / AddVectoredExceptionHandler）。本轮不关闭报告、不加 debugger/injection、不更改依赖。

## 最小后续建议，尚未执行

若主会话继续授权，先用同一纯导入脚本对照原工作目录/导入顺序，以及仅依赖的最小 pytest collect-only 框架（不加载项目 conftest/源码）；最多少量新进程，仍保留 faulthandler 和真实退出码。只有此最小入口复现后，再由主会话选择同脚本的权限执行环境对照；一次正反结果仍不能直接识别原生根因。不重复 62 项或大库测试。

机器结果见 result.json；每次完整时序见 import-01/02/03.stdout.log 和独立 stderr.log。原始失败日志原位保留。
