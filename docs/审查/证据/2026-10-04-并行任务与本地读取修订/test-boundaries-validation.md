# full-03 两项测试边界修订

用途：只修正纯清点与调度的测试分层、内存预算的独立进程边界；保存原 full-03 红证据，不改变生产实现或预算，目录保留、不自动清理。

- 生产基线：`b351dee801b960d15c845a33c7e3ae38fcf85c1a`。
- 文件范围：`app/tests/test_task_control_delivery.py`、`app/tests/test_task_progress_remote_inventory.py`。
- 原红来源：`../b20261004.3-full-03/junit.xml`。
- 清点保持 2000 页、每批新增≤64、依赖≤65、暂停持久、64→128 游标恢复、纯清点 attempts=0，另验证真实本地领取并结案一项。
- 原 100000 条纯内存查询在独立子进程运行；仍测进程自身 lifetime peak，仍要求 <300 MB，baseline VM>500000、candidate VM<5000 和索引计划断言原样保留，不改 RSS 增量。

## 结果与冻结

- `targeted-01.xml` / `targeted-01.txt`：两个原失败项 2 passed；child peak=149.2265625 MB、baseline VM=1000000、candidate VM=100。
- `files-01.xml` / `files-01.txt`：两个文件全部 20 passed；child peak=149.20703125 MB，原 SQL VM 与索引断言再次通过。
- 原 2000/20000/100000 项领取 p95≤200ms、写边界<1000ms 断言均保留并通过；本轮不据此宣称大库性能或墙钟改善。
- 原 full-03 两项红证据未改；不重复另外 748 项，不跑大库/EXE/外网/真实账号。
- 两个测试文件已冻结，父 pytest 及内存查询子进程均正常退出；没有修改生产源码。
- `test_task_control_delivery.py` SHA-256：`2DC1B35F5618C4B21118496BEEADDEA89FE11070465EC1FB6CC361C5E8D00327`。
- `test_task_progress_remote_inventory.py` SHA-256：`78F5B1F4D216782CD7DD131A954B277A4027C4B8EAEFF9261971D509BAFD2375`。
- 生产 `tasks.py` SHA-256 保持 `66E635C7473E7D93B6FE8BE2225AE53D09BFC82630D51C4BBE9EA9FFCFBC093F`。
- Diff 仅两文件：67 insertions、7 deletions；清点用例新增暂停时真实规划不推进、逐批上限及实际领取结案断言，内存用例新增独立 main 入口与进程隔离。
