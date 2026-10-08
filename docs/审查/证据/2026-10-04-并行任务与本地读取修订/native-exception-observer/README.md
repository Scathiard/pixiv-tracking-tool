# 新建隔离子进程的原生异常观察器

2026-10-08。只用于捕获本轮新建 Python 诊断树的 Win32 调试事件：异常地址、线程、模块路径/基址/偏移、first-chance 标记、时序和实际退出码。不是生产调试入口。

- 目录保存观察器、纯逻辑结构自检、短命标准库目标、合成隔离 collect-only 入口和事件日志；按 stdlib / collect-01 / collect-02 分隔，每次新目录，旧证据不覆盖、不清理。
- 只以 CreateProcessW + DEBUG_PROCESS 创建本轮树，不用 DEBUG_ONLY_THIS_PROCESS，不附加现有进程，不注入，不改系统/依赖/生产，不关闭 faulthandler。
- CreateProcess 后、首次 Continue 前设置 DebugSetProcessKillOnExit(false)。主会话批准的修正规则：仅本次 CREATE_PROCESS 初始 TID 的首个 first-chance breakpoint，且地址位于已核实 ntdll 范围，作为系统初始断点返回 DBG_CONTINUE；其余 breakpoint、AV 及其他异常均返回 DBG_EXCEPTION_NOT_HANDLED，绝不吞 AV。普通非异常事件继续。
- 每个新进程树最多观察 60 秒。截止后对仍存活的本轮 PID 调用 DebugActiveProcessStop，正常 detach，不强杀；已 detach 的活进程不伪称退出，不继续启动后续诊断。真实已退出进程记录 EXIT_PROCESS_DEBUG_EVENT exit code；未知退出明确 null。
- 映像 hFile 由观察器 CloseHandle；系统交付的进程/线程调试句柄由 ContinueDebugEvent 的退出事件收回，避免双关。观察器自有 CreateProcess 句柄及 DuplicateHandle 进程副本自行关闭。只读本轮树模块路径和映像大小，不读取任意进程内存、配置、账号或令牌，不打开真实数据库/网络。
- 自检只核验 ctypes 结构、合成事件/地址映射逻辑，不调用创建/调试进程 API。首次真实启动前由主会话审查并放行；先验证标准库短命子进程，再最多两次实际项目 pytest collect-only，阴性不扩增。
- collect-only 入口先阻断 dotenv 配置加载、清空新子进程内 PIXIV_REFRESH_TOKEN，设定本轮合成 DATA_ROOT/TEMP/TMP，并将 tempfile 的进程内临时根限制到本轮目录；socket 的连接/监听/DNS 调用明确拒绝，正常插件保持，不执行测试函数/fixtures，不加载真实配置/凭据。仅修改本次子进程环境，不改配置文件或系统环境。
- 不以恢复执行或退出 0 宣称 AV 无害；若模块大小不可取得，地址归属记 unknown，不靠最近基址猜测。

参考：[Microsoft 异常处理](https://learn.microsoft.com/en-us/windows/win32/debug/debugger-exception-handling)、[DebugSetProcessKillOnExit](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-debugsetprocesskillonexit)。

## 准备阶段已检查

`observer.py` 默认仅执行纯逻辑自检；真实调试必须显式传 `--observe stdlib` / `collect-01` / `collect-02`。在既有 v012-env Python 执行默认入口，实际 exit=0，self_test=passed，Win32 调试 API 调用为 0。

结构尺寸：ExceptionRecord 152、ExceptionInfo 160、CreateProcessInfo 72、CreateThreadInfo 24、LoadDllInfo 40、DebugUnion 160、DebugEvent 176（union offset=16）、Startup 104、ProcessInfo 24、ModuleInfo 24 字节。合成首/末次异常字段、模块边界/未知大小、初始与后续断点、AV NOT_HANDLED 均核验通过。

真实 API、venv 双进程树及正常退出语义尚未运行验证。collector 启动前强制检查 stdlib 已完整退出 0；任何既有结果含未确认停止的 detach 活进程则拒绝新启动。事件数量上限 10000，树总观察窗口 60 秒；到界标记不完整并 detach，不续跑。

系统句柄回收参考：[ContinueDebugEvent](https://learn.microsoft.com/en-us/windows/win32/api/debugapi/nf-debugapi-continuedebugevent)。

## 已批准的初始断点修正

首轮 stdlib 完整结果原样保留；原 observer 源码保存在 `observer.original.py`，SHA-256 在 `observer.original.sha256.json`。`stdlib-review.md` 记录原精确导出地址假设未命中的问题；主会话批准更正后使用新 `stdlib-02` 目录，不覆盖旧轮。

修正的纯逻辑自检记录为 `self-test-02.*`，实际 exit=0；非初始 TID、非 ntdll、后续 breakpoint、last-chance breakpoint 及 AV 均 NOT_HANDLED。stdlib-02 通过后才允许 collect-01/02；conhost AV 与 Python AV 分开统计，不能互相解释。pytest 自身缓存改放本轮合成目录，不写工作树缓存。
