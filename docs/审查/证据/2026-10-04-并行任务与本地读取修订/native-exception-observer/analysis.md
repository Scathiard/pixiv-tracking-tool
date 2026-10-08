# 有界原生事件观察结果

2026-10-08。本轮全部结束。无附加现有进程、注入、强杀、依赖/系统/生产改动；不继续尝试。

| 轮次 | 调试树耗时 | 实际退出码 | Python AV | conhost AV | 收集 |
| --- | --- | --- | --- | --- | --- |
| stdlib（原规则） | 0.344 秒 | launcher / Python / conhost 全 0 | 0 | 2 first-chance | 无 pytest |
| stdlib-02（批准修正） | 0.172 秒 | launcher / Python / conhost 全 0 | 0 | 2 first-chance | 无 pytest |
| collect-01 | 17.078 秒 | launcher / Python / conhost / cmd 全 0 | 0 | 2 first-chance | 816 项，未执行 |
| collect-02 | 4.391 秒 | launcher / Python / conhost / cmd 全 0 | 0 | 2 first-chance | 816 项，未执行 |

所有轮次 `completed_tree=true`、`observer_error=null`、`live_at_detach=[]`，没有超时或 detach。两次 collect stderr 均为空，正常插件保持。DATA_ROOT / TEMP / TMP / pytest cache / basetemp 均合成隔离；dotenv 加载禁止，子进程 PIXIV_REFRESH_TOKEN 为空，socket 连接/监听/DNS 禁止。没有执行测试函数/fixtures、项目 Runtime 或业务负载。

## 实际取得的原生地址

各轮捕获的两次 AV 都属于本轮新建的 `C:/Windows/System32/conhost.exe`，不属于 Python。

- first-chance=true，异常代码 0xC0000005，访问类型为读，目标地址 6。
- 指令地址 140726903731892，模块 `C:/Windows/System32/rpcrt4.dll`。
- 模块基址 140726903177216，SizeOfImage=1146880，指令偏移 554676（0x876B4）。
- 同一轮两次 AV 的线程、地址和偏移一致；没有 last-chance AV。两次都以 DBG_EXCEPTION_NOT_HANDLED 继续，随后该新建控制台及目标树退出 0。

模块加载时立即查询 size 尚未就绪，记录 unknown；异常前重查成功取得 path/size/base，按实际映像范围定位，没有使用最近基址猜测。证明定位能力在这些事件上有效，但并未捕获到旧 Python AV 的地址，因此不能给旧异常指认 rpcrt4 或判定无害。

## 初始断点修正及证据保留

首轮发现系统断点不在 DbgBreakPoint 导出地址。主会话批准仅对 CREATE_PROCESS 初始 TID 的首个 first-chance breakpoint、且地址属于已核实 ntdll 映像范围，作为系统初始断点；其他异常仍 NOT_HANDLED。修正后的 stdlib-02 及两次 collect 均对所有进程识别一次初始断点，其他 breakpoint / AV 没有被吞掉。

原 `observer.original.py` / SHA-256、首轮 stdlib 与 review 全部保留。修正纯逻辑检查实际 exit=0，覆盖非初始 TID、非 ntdll、后续/last-chance breakpoint 与 AV 均拒绝。最终脚本 SHA-256 记录于 `final-source.sha256.json`。

## 结论边界

只证实本次调试树可完整观察 venv 启动器、实际解释器及本轮后代，并取得控制台原生异常地址/模块；实际 pytest 收集两次未出现 Python AV。不能把 conhost 的 RPC 异常拿来解释旧 Python 内部 faulthandler 输出，也不能因阴性排除间歇异常。调试观察会影响进程时序，不把收集耗时当性能结论。

旧长测 AV 仍未归属；本轮上限已到，停止，不再扩大收集或长测复跑。每个子目录的 events.jsonl / result.json / stdout.log / stderr.log 保留完整事件、时序、真实退出码和目标输出。
