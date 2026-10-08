# 首次真实观察器验证

2026-10-08。主会话释放窗口后仅执行一次 `--observe stdlib`；未启动 collect-only。

- 观察器 0.344 秒正常结束，无 observer_error、无 live_at_detach。启动器 PID 69560、实际解释器 PID 57748、新建 conhost PID 59396 的 EXIT_PROCESS 均为 0。
- 实际解释器路径是 `.devtools/python/cpython-3.12.13-windows-x86_64-none/python.exe`；不将 pyvenv.cfg 内旧别名路径当作实际映像路径。目标输出完成且 faulthandler 开启，stderr 为空。
- 捕获两次 first-chance AV，均在 conhost（59396）的线程 69552，指令地址 140726903731892，读地址 6。模块 `C:/Windows/System32/rpcrt4.dll`，基址 140726903177216，大小 1146880，偏移 554676。所有 AV 继续状态为 DBG_EXCEPTION_NOT_HANDLED，未出现 last-chance AV。
- 这些 AV 属于本次控制台后代进程，不属于 Python；不能据此解释旧 Python faulthandler 输出或宣称原异常无害。
- 44 个 DLL 加载事件已有路径，加载瞬间 K32GetModuleInformation 尚未取得 size；在异常事件前重新取模块资料成功，从而得到准确的 rpcrt4 地址范围及偏移。未知大小未按最近基址猜归属。
- 发现初始断点地址识别过窄：三进程都在 ntdll 偏移 0x122239 的初始线程出现 first-chance breakpoint，脚本对精确 DbgBreakPoint 导出地址的假设未命中，所以均 NOT_HANDLED。三进程仍正常退出。已向主会话报告并暂停 collect，等待处理该边界；证据不覆盖，观察器未擅自修订或重跑。

完整事件及真实退出码见 `stdlib/events.jsonl` 与 `stdlib/result.json`。无附加现有进程、无注入、无强杀、无生产/依赖改动。
