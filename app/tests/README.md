# 测试目录约定

- `test_security.py`：网络边界、路径边界和 HTML 清洗。
- `test_jobs.py`：后台任务排队、去重、状态和错误保留。
- `test_file_index.py`：作品文件路径 JSON 编解码与单次目录扫描。
- `test_caption_state.py`：空简介、不可用作品和临时失败的状态区分。
- `test_migration.py`：数据目录预检、SQLite 快照、复制和失败回退。
- `browser_fixture.py` + `browser_smoke.py`：真实 Chromium 的桌面/手机冒烟，只使用隔离夹具，不读取真实 Pixiv token 或图库。
- `test_archive_database.py`、`test_legacy_archive_import.py`：新结构约束、旧 ID／偏好／路径映射、候选库失败回滚；只创建合成数据。
- `test_archive_media.py`、`test_archive_recovery.py`：文件解码、动图原包和时序、发布／删除中断恢复与单写入者。
- `test_archive_tasks.py`、`test_archive_workflows.py`：固定轮次、真实处理量、暂停边界、精确补下载；HTTP 使用本机模拟服务。
- `test_archive_api.py`、`test_archive_pages.py`：新核心接口、旧界面适配、离线可用性、请求边界与端口反馈。
- `test_archive_relocation.py`：连续目录切换、复制被破坏和配置提交失败时保留原运行实例。
- `test_archive_client_contract.py`：模拟 Pixiv 响应，验证鉴权重试、原件地址与错误结果；不访问真实账号。
- `run.py --smoke-test --smoke-output NEW_JSON_PATH`：源码与 EXE 共用的隔离冒烟，自动生成独立测试库和媒体，保留结果，不启动登录或托盘。
- `archive_browser.py`：新核心的桌面与手机尺寸浏览器验证；`--artifacts` 必须指定新的隔离目录，保留结果和截图；不是手机实机验收。
- 没有 `py` 启动器时可使用 `.devtools/v012-env/Scripts/python.exe`（Python 3.12）；指定 `.devtools/test-runs/` 下新的 `--basetemp`，不得重复使用会清理旧证据的路径。
- 每个测试自行创建临时目录；不得依赖或修改项目 `.env`、`data/`、`images/` 和 `release/`。
