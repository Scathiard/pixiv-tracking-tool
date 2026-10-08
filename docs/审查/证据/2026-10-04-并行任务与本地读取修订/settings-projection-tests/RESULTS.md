# 设置窄投影受影响回归结果

- 首次执行：七个指定测试文件，62 passed / 77.35 秒；JUnit XML：62 tests、0 failures、0 errors、0 skipped。
- 真实退出码：父 subprocess.run 捕获子 pytest returncode=0；stdout/stderr 分别保存，不通过 PowerShell 管道推定。
- 异常边界：stderr.log 再现两段 Windows fatal exception: access violation，堆栈在 importlib 的路径读取和文件读取，经 gallery_dl / auth 导入进入设置模块；随后所有测试完成。保留首次异常并停止，不自动重跑，不记为完全干净，也不宣称运行时根因已修。
- 本次测试改动：增加历史密集设置回归；scale 的证据捕获补认 WITH 并断言真实CTE已留证；这是仪器修正，原1秒断言保持。tests README 更新约定，子代理未再改产品代码。
- 历史夹具：单作品、20,000条旧版本+2条当前版本，每批500条，历史简介/source各512字节、四条资产；数据库/WAL/SHM合计60,781,184字节，小于100MiB固定门槛。
- 统计核验：历史ugoira(1页)、作品fallback(illust/97页)、当前manga(3页)形态不同；原件为历史所有，但只在当前版本绑定，未绑定历史原件排除。两次manga请求、切换ugoira(8页但需1个原包)、切回manga，共4次实际GET均200且全部预期统计匹配。
- 自然SQL计划：每次 revision_shapes MATERIALIZED 扫描 ix_revisions_settings_shape 覆盖索引，连接自动覆盖临时投影；没有逐件 SEARCH work_revisions 回宽记录。
- 历史密集性能：API最慢0.041444499976933秒；SQL最慢0.0248114001005888秒，4.5秒SQL/读取与5秒完整GET门槛保留。
- 进程内存：夹具前RSS112,422,912字节，夹具后115,167,232字节，结束119,504,896字节；每次GET前后RSS/VMS及进程生命周期工作集峰值均保存，未把生命周期峰值称为查询独立峰值。
- 15,000作品既有规模测试：API最慢0.2998403001111秒（含计划捕获），小于原1秒门槛；共16条查询证据，包含4条真实WITH聚合，聚合SQL最慢0.265418599825352秒。
- 静态检查：git diff --check 通过；不运行全套、长压力或EXE，不触碰真实图库、配置或令牌。
- 保留：process.json、stdout.log、stderr.log、pytest.xml、projection/指标及pytest/合成库全部保留；源码和测试文件再次冻结。
