# 设置窄投影受影响回归

- 用途：覆盖 statement-local revision_shapes 窄投影和两条覆盖索引的受影响回归。
- 测试范围：settings_projection、settings_scale、daily_management、snapshot_read_sharing、settings_index_preparation、task_index_preparation、upgrade_revisions 七个确切测试文件。
- 新夹具：单作品两万条历史版本，每批五百条，每历史简介/JSON各五百一十二字节，只四条资产；数据库文件加WAL/SHM不足100MiB。
- 证据：保存实际GET结果、默认版本切换、自然SQL计划、SQL/API耗时和进程内存快照；4.5s/5s与原scale1s门槛不变。
- 仪器修正：scale的查询证据捕获补认WITH；没有改产品查询、增加缓存、强制SQL提示或扩大门槛。
- 捕获：父Python subprocess.run独立捕获子pytest返回码与stdout/stderr，不关闭faulthandler。
- 保留：脚本、输出、指标和合成数据库保留，不自动清理；不代替八小时或EXE验收。
