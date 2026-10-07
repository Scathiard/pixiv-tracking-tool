# 活跃下载字节展示内存快照

- 用途：真实 TaskQueue/TaskContext 的提交次数红绿对照、合成并行/重试/恢复专项及直接阶段/控制回归；按轮次隔离，保留、不自动清理。
- 基线：HEAD `f0df243943ae54bba3842b171dc65435d0efe398`；本代理只修改 `app/src/core/tasks.py`、新增 `app/tests/test_task_byte_progress.py`，及主会话追加授权的 `app/tests/test_shutdown.py` 一处旧测试准入 seam。
- 主会话拥有数据库、HTTP 共享读取接入和其他处理器修订；本证据不是其他改动的独立验收，也不表示 EXE/大库/用户体验已验收。

## 红绿事实

- `red-01.txt`：真实一步连续 report_bytes(10/50/100)，各报告后累计新增提交 `[1,2,3]`，预期零展示提交因此失败。
- `green-01.txt`：同一反例转绿，报告新增提交 `[0,0,0]`；步骤全程只有原有领取/阶段准备、真实结案、finalize 共 3 次提交。
- 字节到 100 时仍是 running、阶段 done=0；只有真实执行结果结案后才变 succeeded、阶段完成。
- `specialty-02.txt`：13 项专项全部通过；两个真实 _step 下载线程持续重扫、并行分别上报未知分母，响应同时保留两项字节快照，完成后清空。
- 每次执行以独立 token 绑定字节；发送后重试与未发送暂缓（包括退回相同 attempts）均不接受旧 token 的晚到字节/清理覆盖新尝试。
- 暂停、停止、失败、暂缓、恢复以及结案后清理；旧数据库 current_item/current_items 永不作为活跃字节展示，重新执行显示新字节。
- 上报测试禁止数据库 read/write，响应合成测试确认读连接已归还；字节重复值不改内存世代。
- 真实 ArchiveHandler + Publisher + 合成 PNG：字节上报没有新增提交，Asset verified/hash/byte_size、原件文件、PublicationJournal committed 和步骤成功事实全部保留；内存展示上报抛异常时也正常发布与结案。

## 回归与旧测试 seam

- `regression-01.txt`：24 passed、1 failed；通过项包含清点公平、原子开始进度、持久阶段/暂停/父控制、真实双下载/同资产互斥、有限实际请求重试、容量暂缓与阶段重启重计。
- 唯一失败为旧停机恢复用例假设重建后第一次同步轮次必定完成；实际库只有 pending + 未来 not_before，没有 failed。
- `baseline_shutdown.py` / `baseline-shutdown-01.txt`：进程内加载原 `f0df243` tasks（不覆盖仓库文件），相同断言仍失败，证明该断言失败不由本次 tasks 字节修订引入；其他模块沿用执行时工作树。
- 正式下载 task_scope 走共享 `_admit_request`，旧测试只 patch `_before_request`，重建 Runtime 不取消正常图片准入间隔。
- 主会话授权将该测试改为有截止的真实 run_round 继续驱动，未 patch 掉准入或放宽业务重试；最终两次响应全 closed、旧资产未误失败、新文件已 verified 的原断言保留。
- `final-01.txt`：13 项字节专项 + 修正的真实停机恢复用例，14 passed；与此前 24 项通过组合，全部 25 个选中直接回归均有通过证据，无未解决失败。
- 首次双线程专项只调用一次 _step，两个线程竞争同候选使后者合法让出；修正测试驱动为工作线程实际使用的重扫机制后通过，未改变调度业务。

## 边界与冻结

- 下载字节仅保存当前执行项；不新增写线程/服务/schema，不改变下载器 0.25 秒上报节奏、文件 fsync/发布协议、阶段/领取/结案/用户意图持久化或任何存储路径。
- 内存世代接口 `queue.byte_progress_generation` 由主会话接入 `/api/progress` 的正在执行 GET 共享键，HTTP 契约字段仍为 current_items/current_item。
- 没有运行全套、大库、EXE、真实图库/账号或外网；提交次数是事实对照，不宣称墙钟性能。
- 文件已冻结，测试进程已退出；未 commit/build/部署。
- tasks.py SHA-256：`66E635C7473E7D93B6FE8BE2225AE53D09BFC82630D51C4BBE9EA9FFCFBC093F`
- test_task_byte_progress.py SHA-256：`05E3BFC842025FF128523C9B3E493CB28AE8DEAE30E5DAD438F688E46A7E260F`
- test_shutdown.py SHA-256：`2F19B9875FE12A4BC5BA25F3D639285E2FA88970602DB73899B2E2421DAAAD38`
