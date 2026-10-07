# 账号发布修复隔离验证

- 日期：2026-10-08；工作树 `.worktrees/v0.2.0-build20261004.3`，分支 `codex/v0.2.0-build20261004.3`，基线 `888315a21883ed268a11b0d457207996e8eca9c5`。
- 当前候选：未提交的 `auth.py`、`client.py`、`core/runtime.py`、`web.py` 与新增 `test_account_publication.py`；本代理只新增/修订 `core/runtime.py` 与专项测试，未触碰其他生产文件。
- 凭据读取/等待在 client 锁外；生产路径按 token→client 锁序发布，只比较已加载的代际并赋本地账号；更换客户端立即清空账号，失败连接使用 compare-and-clear。
- 补充反例：同一 PixivClient 合成凭据 A→B→A 会复活旧收藏可信缓存，`b20261008-account-03/stdout.txt` 保留 `1 failed, 7 passed`；现按已发布账号/登录代际判断变化，所有 client/token 锁释放后清理内存可信缓存，不写数据库事实。
- 最终验证：`74 passed in 84.85s`；账号发布专项 9 项，另覆盖正式并行控制、凭据读写、收藏、收藏锁边界与启动登录恢复。
- 执行：在工作树 `app/` 使用主仓库 `.devtools/v012-env/Scripts/python.exe` 运行本目录 `run_tests.py`；启动器首次运行创建新 TEMP 与 pytest basetemp，不得在此目录重复执行。
- 输入：全部合成图库/凭据；无外部 socket 连接；仅允许 stdlib socketpair 创建 Windows asyncio 自唤醒管道，禁止任何应用外部连接；凭据文件访问限制到本轮目录。
- 证据：`stdout.txt`、`junit.xml`、`result.json`、`source-hashes.json` 与 `pytest-temp/` 保留；`git diff --check` 通过。
- 先前轮次：01 为禁凭据守卫误拦 stdlib tokenize 的启动器错误；02 为测试禁网误拦 Windows asyncio 自唤醒 socketpair，5 项通过/1 项测试夹具失败；03 为上述真实同客户端缓存反例；04 最小缓存修复后 8 项通过；05 为最终候选的相关完整回归。旧目录不删除、不复用。
- 限制：没有真实账号、真实图库、浏览器、EXE、发布或长期性能验收；本记录只证明本次源码专项与相关回归。
- Freeze：本代理已完成约定文件修改及相关验证，不再修改候选，交主会话统一整合。
