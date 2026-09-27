# 当前文件审查覆盖

对应[全面审查](../../2026-09-08-全面审查.md)。当前 app 共 60 个维护文件，文本共 9,195 行；文件大小、行数与原始 SHA-256 见 [source-inventory.json](source-inventory.json)。下表记录实际职责与审查判断，不将“已阅读”写成“已证明没有问题”。

## 实现与后台

| 文件 | 阅读后的判断 |
| --- | --- |
| app/run.py | 启动分流与异常兜底；smoke 与真实托盘生命周期不同，windowed 模式 input 兜底需日后验证 |
| app/src/auth.py | 完整通读，固定认证常量脱敏查看；原子 token 写入已改善，认证重试与任务状态仍分散；真实 OAuth 本轮未测 |
| app/src/caption_state.py | 区分合法空简介、不可用和临时失败；既有 tests 通过；复用 caption 字段的命名与迁移语义需整理 |
| app/src/client.py | 元数据／收藏接口封装；BUG-040 HTTP 失败误判；连接复用、限流预算和错误分类应统一 |
| app/src/config.py | 路径与配置持久化；BUG-015 两套迁移流程，BUG-045 当前／下次端口混同；两个配置项未被消费 |
| app/src/data_migration.py | 有空目录预检、快照和旧数据保留；BUG-015 验证只到长度，运行环境激活不在完整恢复边界内 |
| app/src/file_index.py | 单次扫描与自然排序合理；BUG-036 没有完整性验证，BUG-037 URL 直接拼接 |
| app/src/models.py | 三张业务表、SQLite WAL 与外键；关系混合远端身份和追踪状态；每次启动执行全量 Tag 回填；BUG-043 时间语义 |
| app/src/networking.py | 地址展示辅助；IPv4／IPv6 基础测试通过；多网卡地址在移动设置页需要友好呈现 |
| app/src/progress.py | 单一重任务队列、同类去重、终态保留已有测试；运行任务不会按年龄消失；033／034 计数不闭合，缺恢复／取消契约 |
| app/src/query_state.py | 已统一 URL 编码和 absent／empty 区别，现有测试通过；浏览器未完全沿用同一状态模型 |
| app/src/scheduler.py | 定时任务进入统一队列；调度器生命期需要与迁移、退出协调；不要把这里的串行排队当作全部写入串行 |
| app/src/security.py | 路径归属、来源／同源检查与白名单清洗；现有测试通过；BUG-037 重复解码与合法文件名冲突 |
| app/src/smoke.py | 能检查部分打包资源和状态路由；不是完整 lifespan、托盘或联网验收 |
| app/src/tombstones.py | 持久化阻止作品重新加入；原子保存与损坏时拒绝静默回退合理；与文件、DB 的跨资源恢复仍需持续回归 |
| app/src/tracker.py | 核心同步、存档、转换与删除编排；完整阅读 949 行。BUG-036／022／038／039／043／044；多条重复流程是主要维护风险 |
| app/src/tray.py | 托盘与服务退出控制；BUG-045 修改端口后入口过早换址；启动定时等待、繁忙拒绝退出与日志生命周期需明确 |
| app/src/version.py | 单一版本来源 0.1.1，构建复用，保留 |
| app/src/web.py | 全局运行对象、启动与文件服务；BUG-015 重载异常恢复；初始化副作用和路由注册时间不利于完整生命周期测试 |
| app/src/routes/__init__.py | 空包入口，无业务逻辑 |
| app/src/routes/artists.py | 画师管理与刷新流程；私有方法调用和流程复制；单画师刷新不检查新作品列表，需明确产品含义 |
| app/src/routes/settings.py | 配置／迁移／任务入口；BUG-015 切换失败恢复；移动端本机目录选择语义不清 |
| app/src/routes/works.py | 图库查询、详情与操作；BUG-040／044；导航全量读取，筛选逻辑多份，Tag 的 500 限制实际作用于关联行 |

## 模板与素材

| 文件 | 阅读后的判断 |
| --- | --- |
| app/src/templates/base.html | 共用布局、登录与任务轮询；错误文本安全输出；轮询完成整页刷新、只显示最近失败，缺任务历史操作入口 |
| app/src/templates/index.html | 716 行；BUG-021 连续筛选／分页状态，BUG-042 收藏重复绑定；原件直接作缩略图 |
| app/src/templates/artist_works.html | 571 行；BUG-041 初始化发生在画廊 DOM 之前；与主页重复实现后产生行为差异 |
| app/src/templates/gallery_fragment.html | 主页追加卡片；与完整页卡片平行维护，宜共享模板 |
| app/src/templates/artist_fragment.html | 画师追加卡片；未下载作品缺详情入口，和主页表现不同；部分角标与完整页也不一致 |
| app/src/templates/illust_detail.html | 图片、简介、收藏与导航；一次插入所有页且非懒加载；固定边缘导航、全局触摸滑动需要在真机阅读场景验证 |
| app/src/templates/artists.html | 图库管理与操作入口；手机按钮文字挤压；头像失败直接隐藏，没有一致兜底；暂停文案其实已实现 |
| app/src/templates/settings.html | BUG-046 长地址溢出；桌面路径操作放到手机需要解释；“校验”说明比实际验证能力更强 |
| app/src/templates/error.html | 安全转义错误页；部分业务不存在返回 200，由调用者的响应状态约定改进 |
| app/static/style.css | 702 行通读；主题变量与基础手机规则可保留；缺统一焦点／弹层／动效减弱约定，后续规则存在覆盖前面移动规则的情况 |
| app/static/placeholder.svg | 四行静态 SVG，无脚本；深色占位可用，No Image 可后续统一中文及失败语义 |
| app/static/icon.png | 已目视查看；现有托盘图标资源。应用图标与托盘尺寸应在真实系统显示中验收，未重新制作 |

## 测试

以下 17 个 Python 测试／夹具文件及说明均已阅读。58 项 pytest 与原浏览器脚本通过，但缺失路径与连续操作仍需本轮补充复现；单元测试通过没有消除报告中的问题。

| 文件 | 覆盖／遗漏判断 |
| --- | --- |
| app/tests/conftest.py | 隔离 DATA_ROOT；没有配置根目录导入路径，所以依赖在 app 下运行 |
| app/tests/test_auth_tokens.py | token 原子读写与刷新回退；未覆盖真实 OAuth 交互 |
| app/tests/test_caption_fetch.py | 简介获取状态及失败分流；属于合成响应验证 |
| app/tests/test_caption_state.py | 空、不可用、失败状态的重试判断 |
| app/tests/test_config_persistence.py | 单项配置持久化与非法值；未覆盖端口尚未重启时的入口一致性 |
| app/tests/test_file_index.py | JSON 索引、自然排序、目录改名／冲突相关保护；未证明多页完整与损坏识别 |
| app/tests/test_filter_routes.py | 后端筛选、URL、详情导航等；不执行完整前端连续操作 |
| app/tests/test_jobs.py | 串行、去重、TTL、异常、排他与维护租约；不覆盖下载库的非零返回结果 |
| app/tests/test_migration.py | 路径预检、复制、SQLite；损坏样例改变了文件长度，漏掉同长度差异与运行时重载失败 |
| app/tests/test_networking.py | 双栈地址格式与过滤；未覆盖页面长地址布局 |
| app/tests/test_query_state.py | absent／empty、受限值、编码与本地跳转边界 |
| app/tests/test_security.py | 图片路径、HTML 清洗、网络来源、Origin；是基础防回归，不是完整渗透审计 |
| app/tests/test_template_safety.py | 模板危险写法的文本断言；能守一部分约束，但不能替代浏览器行为测试 |
| app/tests/test_tombstones.py | 永久删除、重下抑制和文件／DB 回滚场景，值得保留 |
| app/tests/test_web_boundary.py | Web 来源、请求保护与有关接口隔离；不启动真实账号 |
| app/tests/browser_fixture.py | 两位合成画师与 24 件作品，独立 FastAPI；绕开主程序 lifespan，是测试边界而非产品缺陷 |
| app/tests/browser_smoke.py | 桌面／手机、基础导航、跨站请求拒绝和控制台错误；数据不足一页，无法发现多页缺陷 |
| app/tests/README.md | 隔离规则清楚；测试文件列举不完整，执行目录由根 AGENTS 已更正 |

## 构建、启动与配置样例

| 文件 | 阅读后的判断 |
| --- | --- |
| app/build.spec | 读取单一版本源，打包模板和素材；收集整个 gallery-dl 模块，体积优化属后续取舍；没有执行构建 |
| app/release.bat | 清理构建目录并覆盖同版本 EXE；缺目标存在保护、复制错误检查、测试与散列门槛；本轮未运行 |
| app/start.bat | 使用环境中的 python，未统一到已验证的 Python 3.12 环境；本轮未启动正式服务 |
| app/requirements.txt | 最低版本声明；方便安装，但不能代表可重复构建锁定 |
| app/requirements-lock.txt | 锁定直接依赖与 PyInstaller；未完整锁传递依赖，且发布脚本不强制使用它 |
| app/requirements-dev.txt | 引用浮动运行依赖并增加 pytest/httpx；当前测试环境已记录，不新装依赖 |
| app/.env.example | 脱敏配置样例已存在；DOWNLOAD_WORKERS／REQUEST_DELAY 当前没有使用者 |

## 根目录、历史与非文本文件

- 当前 AGENTS、README、三份主文档、团队通信、.gitignore、.pylintrc 已阅读。规则明确；此次修正错误的测试工作目录，新增审查材料位置。用户已编辑的愿景／职责文本保留。
- 旧组的 agent／claude、开发计划、BUG、备忘录、GPT 审核、两份目录迁移设计与实施文档，在接手和本轮定向复核中用于历史对照。旧文档原文不修改，不当成当前指令。
- 已有旧工作树、工具与生成缓存仍归档。它们不是当前维护实现，不逐份重审第三方程序和旧版重复代码；Git 元数据只读状态／历史，不逐个解析对象库。
- 现有 release 八个版本目录按清单核对；六个 EXE 保留，v0.0.7／v0.0.8 当前目录没有 EXE。只对现有 v0.1.1 的临时副本运行基础冒烟，不宣称旧版本都经过动态验收。
- 根 token、实际 .env、数据库和图库的值／正文没有纳入本报告；依据原保护清单核对散列，不读取正式账号内容。项目外正式图库未扫描。

保留文件散列、语法与依赖版本、文档链接检查统一记录在 [review-verification.json](review-verification.json)。
