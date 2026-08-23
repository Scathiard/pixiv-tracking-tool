# 测试目录约定

- `test_security.py`：网络边界、路径边界和 HTML 清洗。
- `test_jobs.py`：后台任务排队、去重、状态和错误保留。
- `test_file_index.py`：作品文件路径 JSON 编解码与单次目录扫描。
- `test_caption_state.py`：空简介、不可用作品和临时失败的状态区分。
- `test_migration.py`：数据目录预检、SQLite 快照、复制和失败回退。
- `browser_fixture.py` + `browser_smoke.py`：真实 Chromium 的桌面/手机冒烟，只使用隔离夹具，不读取真实 Pixiv token 或图库。
- 每个测试自行创建临时目录；不得依赖或修改项目 `.env`、`data/`、`images/` 和 `release/`。
