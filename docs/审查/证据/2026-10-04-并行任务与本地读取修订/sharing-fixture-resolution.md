# 保存失败后的读取共享测试边界

用途：仅用 test_snapshot_read_sharing.py 的真实 localhost HTTP 与合成 Runtime，确认失败 PATCH 后的 later GET 是否在应用准入前发生协议异常；不启动 Runtime.start，不访问真实图库、配置或账号。

范围：基线 05e3c30，仅可修改上述测试文件；保留三秒等待、成功／失败保存代际和写请求不共享断言，不修改生产代码。

命名：red-XX 捕获原因，green-XX 验证夹具修正，regression-XX 为该文件相关回归；每次使用新的 basetemp、输出和 XML，不覆盖旧结果。

保留：临时诊断材料及测试结果只保留在本目录，不自动删除；阶段结束冻结文件，由主会话收录。不得以小测试结果宣称大型运行或真实体验通过。

## 根因证据与限定

完整 full-04 记录只显示失败保存 PATCH 返回 500 后，later GET 的 received==2 等待失败，未采集 later Future 的异常；不能据此声称原全套异常类型就是 ReadError。

隔离的真实 HTTP 时序反例捕获以下路径：未处理的合成 OSError 已形成 500 响应（没有 Connection: close），HTTPX 将连接留在原池的 idle 状态；后续 GET 选中该连接后，原 Uvicorn 异常路径关闭 transport，later Future 抛出 httpx.ReadError / WinError 10053，读取准入 received 与实际快照 calls 均仍为 1。red-04.txt:66–72、red-05.txt:66–72 保存 HTTP trace 与 Future 事实。该反例支持与 full-04 相同的失效路径，不逐字证明原错误类型。

探针只在合成测试中将 ASGI 异常收尾暂时让出，随后仍运行原 Uvicorn run_asgi 的日志与关闭路径；HTTPX trace 在 GET 选中连接后等待这一关闭完成，未修改读取控制器或共享逻辑。先前 red-01 单项自然运行通过；red-02/03 为不成功的同步日志探针，不能作为根因证明，其输出保留但不用于结论。

## 最小修正与验证

- 仅失败保存 PATCH 使用独立短生命周期 httpx.Client；成功 PATCH 和两个 GET 继续使用原 client。
- 原保存成功／失败代际、PATCH 返回状态、三个／一个快照调用、两个 GET 入站，以及所有三秒等待和其他原时限均保留，无重试或异常吞掉。
- red-04：1 failed / 5.62s；从 05e3c30 提取的 baseline_repro.py 在 red-05 重复确认：1 failed / 18.15s，均取得实际 ReadError 与入站计数保持 1。
- green-01：同一关闭时序探针，1 passed / 2.30s；原 GET 池没有失败 PATCH 的 idle 连接，后续 GET 建立自己的连接，失败 PATCH transport 仍按原路径关闭。
- regression-01：正常夹具的文件全部 24 passed / 21.58s；未加载诊断插件。
- git diff --check 通过；app/src 无差异，仓库测试无残留 DEBUG 标签；所有 Python 测试进程退出。未提交、未跑全套或大库。

冻结文件：app/tests/test_snapshot_read_sharing.py；SHA-256：0971f2a9e150e9ba797651001f28795f54feea45e601437cf87e4f5ba9ab0f00。

## 独立红反例入口

从工作树 app/ 运行；预期结果为失败，最终异常为 ReadError，received/calls 仍为 1。每次生成新证据名，不覆盖旧文件：

```powershell
$evidenceRoot = 'D:\claude_sandbox\pixiv-tracking-tool\.devtools\test-runs\b20261008-sharing-failure-01'
$env:PYTHONPATH = $evidenceRoot
$env:PYTHONUTF8 = '1'
$runKey = [guid]::NewGuid().ToString('N')
& 'D:\claude_sandbox\pixiv-tracking-tool\.devtools\v012-env\Scripts\python.exe' -m pytest -q -s -p close_race_plugin "$evidenceRoot\baseline_repro.py::test_port_patch_fences_only_successful_save_and_is_never_shared[False]" "--basetemp=$evidenceRoot\red-replay-$runKey-tmp" "--junitxml=$evidenceRoot\red-replay-$runKey.xml" *> "$evidenceRoot\red-replay-$runKey.txt"
```

同一命令把用例路径换为 tests/test_snapshot_read_sharing.py 的同名 False 用例即为候选绿对照；为其使用另一组全新结果名。
