# PixivTracker

一个本地优先的 Pixiv 存档与阅览工具。下载追踪画师的作品，在电脑和手机浏览已有图库；断网时仍可查看本地内容。

源码当前版本为 **0.2.0**。可下载的正式版本以 [GitHub Releases](https://github.com/Scathiard/pixiv-tracking-tool/releases) 为准。

## 使用

Windows 版只需下载一个 EXE，放到独立目录后直接运行，无需解压或安装 Python、Node.js、PowerShell。程序驻留系统托盘，默认网页地址是 `http://127.0.0.1:1919/`；手机地址在设置页查看。程序目录及设置中配置的图库属于持久数据，更新时保留原有配置、数据和图片。

- 图库支持搜索、画师／标签筛选、排序、连续滚动与页码浏览。
- 普通详情与流式阅览支持多页作品、附近作品预取及局部缩放；流式可切换顺序／随机。
- 可追踪画师并检查新作；设置中的“检查并补齐图库”用于完整维护与补缺。
- 收藏对应当前 Pixiv 账号的主站收藏，需要联网核对；本地阅读不依赖登录。
- 动图保留原包与时序，阅览预览可重建。已无法访问的主站作品仍保留本地存档。

第一次联网使用时在设置中登录。自动登录使用系统 Chrome／Edge，无需额外安装 Playwright 浏览器。

退出使用托盘菜单。Release 页面提供使用说明和文件校验值，下载附件只有 EXE。

## 从源码构建 Windows x64 EXE

需要 Python 3.12 x64、Node.js 22.12+（本次使用 Node 24），以及构建用 SQLite 3.51.3 或更新版本。构建结果为 Windows 程序，前端静态文件一同打包。

可下载 GitHub 源码 ZIP，或使用 `git clone --depth 1 https://github.com/Scathiard/pixiv-tracking-tool.git` 获取当前源码，避免下载保留的完整历史。进入仓库根目录后执行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r app/requirements-lock.txt
npm.cmd --prefix app/frontend ci
.\.venv\Scripts\python.exe -c "import sqlite3; print(sqlite3.sqlite_version)"
```

`requirements-lock.txt` 固定直接依赖版本（含 PyInstaller），不是完整的传递依赖锁。前端使用 `package-lock.json` 与 `npm ci` 安装。

如果上面的 SQLite 版本已达到 3.51.3：

```powershell
.\.venv\Scripts\python.exe app/build_release.py
```

如果 Python 自带 SQLite 较旧，从 [SQLite 官方下载页](https://www.sqlite.org/download.html) 下载 Windows x64 DLL 包，解压到本地构建依赖目录，例如 `.build-deps/sqlite/`，再执行：

```powershell
.\.venv\Scripts\python.exe app/build_release.py --sqlite-dll .build-deps/sqlite/sqlite3.dll
```

这只为本次 EXE 指定 SQLite，不修改系统目录或 Python 安装。脚本会先检查所选 DLL 的版本与可加载性，再构建前端和 EXE。DLL 架构必须与 Python 一致。

本地构建输出位置（只有 EXE 作为 Release 下载附件，其余为开发记录与运维工具）：

```text
release/v0.2.0/build-<内部构建号>/
  PixivTracker_v0.2.0_build<内部构建号>.exe
  start_release.ps1
  stop_release.ps1
  build-manifest.json
  SHA256SUMS.txt
```

公开版本与内部构建号统一定义于 `app/src/version.py`。为避免覆盖旧包，输出目录已存在时构建会停止；再次构建可用 `--output-root` 和 `--work-root` 指定新的位置，或为新构建修改内部构建号。

Git 克隆与 GitHub 源码 ZIP 都可构建。克隆构建会记录源码提交；没有 Git 元数据时 manifest 的来源字段为 `null`。

源码中的 `start_release.ps1` 和 `stop_release.ps1` 供开发者启动、记录退出码及自动更新时正常退出程序，需要 PowerShell 7；普通用户直接运行 EXE、从托盘退出即可，这两个脚本不随 Release 分发。

## 配置与升级

配置示例位于 `app/.env.example`。真实 `.env`、登录令牌、数据库、图片和日志不属于公开源码，构建脚本不会把它们复制进包。

需要更新日常 EXE 时，先从托盘正常退出旧程序，确认退出后再运行新版 EXE。不要同时让两个实例写入同一图库。旧图库需要结构升级时，以程序提供的备份、转换及确认流程为准。

## 源码范围

本仓库当前树只保留构建／运行需要的后端源码、前端源码、静态资源、依赖与构建配置、配置示例和启动脚本。内部规划、审查记录、测试夹具、真实数据、构建缓存和历史安装包不随当前源码树分发。历史提交和已有 Release 保留。
