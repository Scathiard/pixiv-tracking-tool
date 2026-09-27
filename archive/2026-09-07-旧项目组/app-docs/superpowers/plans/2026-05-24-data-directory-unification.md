# 统一数据目录 & Bug 修复 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 DATA_DIR 和 IMAGES_DIR 合并为单一 DATA_ROOT，修复 3 个已知 bug（re.error 500、scheduler 崩溃、新作品漏检）

**Architecture:** `DATA_ROOT` 是顶层配置项，`DATA_DIR` 和 `IMAGES_DIR` 从其推导（`DATA_ROOT/data/`、`DATA_ROOT/images/`）。默认值 `PROJECT_ROOT`，与现有行为完全兼容。启动时检测旧配置自动迁移。

**Tech Stack:** Python 3.12, pathlib, SQLAlchemy, FastAPI

---

### Task 1: 重构 config.py — DATA_ROOT + 启动迁移

**Files:**
- Modify: `src/config.py`

引入 `DATA_ROOT`，`DATA_DIR` 和 `IMAGES_DIR` 从其推导。首次启动时若检测到旧格式 `.env`（有 `IMAGES_DIR` 无 `DATA_ROOT`），自动迁移到新结构。

- [ ] **Step 1: 添加 `_resolve_data_root` 和 `_migrate_to_data_root`，重构配置解析**

将 `src/config.py` 的配置部分（第15-75行）替换为以下内容。关键变化：
1. 新增 `_resolve_data_root()` — 从 `DATA_ROOT` env 变量解析，默认 `PROJECT_ROOT`
2. `DATA_DIR` 和 `IMAGES_DIR` 从 `DATA_ROOT` 推导
3. `_migrate_to_data_root()` — 首次启动检测旧配置，移动文件，写入新配置
4. `_resolve_images_dir` 函数删除，逻辑已被 `DATA_ROOT` 覆盖

```python
import os
import sys
import shutil
from pathlib import Path
from dotenv import load_dotenv

# PyInstaller 打包后资源在 _MEIPASS 临时目录，数据在可执行文件目录
if getattr(sys, 'frozen', False):
    BUNDLE_DIR = Path(sys._MEIPASS)
    PROJECT_ROOT = Path(sys.executable).parent
else:
    BUNDLE_DIR = None
    PROJECT_ROOT = Path(__file__).parent.parent


def resource_path(relative_path):
    """获取资源文件路径，兼容 PyInstaller 打包。"""
    if BUNDLE_DIR:
        return str(BUNDLE_DIR / relative_path)
    return str(PROJECT_ROOT / relative_path)


env_file = PROJECT_ROOT / ".env"
if not env_file.exists():
    env_file.write_text("""\
# Pixiv Tracking Tool configuration
# First run will guide you through browser OAuth login

HOST=0.0.0.0
PORT=8000
# DATA_ROOT=.
CHECK_INTERVAL_HOURS=6
""", encoding='utf-8')

load_dotenv(env_file, encoding='utf-8')


def _resolve_data_root():
    v = os.getenv("DATA_ROOT", "").strip()
    if v:
        p = Path(v)
        if p.is_absolute():
            return p
        return PROJECT_ROOT / p
    return PROJECT_ROOT


def _migrate_to_data_root():
    """首次用新代码启动时：若 .env 有旧的 IMAGES_DIR 但无 DATA_ROOT，自动迁移。"""
    content = env_file.read_text(encoding='utf-8')
    if 'DATA_ROOT=' in content:
        return

    old_images = os.getenv("IMAGES_DIR", "").strip()
    old_data = os.getenv("DATA_DIR", "data").strip()

    # 解析旧路径
    old_images_path = Path(old_images) if old_images else PROJECT_ROOT / "images"
    if not old_images_path.is_absolute():
        old_images_path = PROJECT_ROOT / old_images_path

    old_data_path = Path(old_data)
    if not old_data_path.is_absolute():
        old_data_path = PROJECT_ROOT / old_data_path

    # 目标路径（DATA_ROOT 默认 PROJECT_ROOT）
    new_images = PROJECT_ROOT / "images"
    new_data = PROJECT_ROOT / "data"

    new_images.mkdir(parents=True, exist_ok=True)
    new_data.mkdir(parents=True, exist_ok=True)

    # 迁移 images：从旧位置移动到 PROJECT_ROOT/images/
    if old_images_path.resolve() != new_images.resolve() and old_images_path.exists():
        for child in old_images_path.iterdir():
            dest = new_images / child.name
            if not dest.exists():
                shutil.move(str(child), str(dest))

    # 迁移 data：从旧位置复制到 PROJECT_ROOT/data/
    if old_data_path.resolve() != new_data.resolve() and old_data_path.exists():
        for child in old_data_path.iterdir():
            dest = new_data / child.name
            if not dest.exists():
                if child.is_dir():
                    shutil.copytree(str(child), str(dest))
                else:
                    shutil.copy2(str(child), str(dest))

    # 更新 .env：添加 DATA_ROOT，清理旧配置项
    lines = content.split('\n')
    lines = [l for l in lines
             if not l.startswith('IMAGES_DIR=')
             and not l.startswith('DATA_DIR=')]
    content = '\n'.join(lines).rstrip('\n') + '\nDATA_ROOT=.\n'
    env_file.write_text(content, encoding='utf-8')

    # 重载环境变量
    os.environ["DATA_ROOT"] = "."
    os.environ.pop("IMAGES_DIR", None)
    os.environ.pop("DATA_DIR", None)


# 执行迁移（必须在 _resolve_data_root 之前）
_migrate_to_data_root()

DATA_ROOT = _resolve_data_root()
DATA_DIR = DATA_ROOT / "data"
IMAGES_DIR = DATA_ROOT / "images"

# 确保目录存在
DATA_DIR.mkdir(parents=True, exist_ok=True)
IMAGES_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_URL = f"sqlite:///{DATA_DIR / 'pixiv_tracker.db'}"


def set_data_root(new_path):
    """修改数据根目录并持久化到 .env，同时迁移已有数据。"""
    new_path = new_path.strip()
    new_root = Path(new_path)
    if not new_root.is_absolute():
        new_root = PROJECT_ROOT / new_root

    old_root = DATA_ROOT

    # 相同路径无需迁移
    if new_root.resolve() == old_root.resolve():
        return

    # 创建新目录结构
    new_data = new_root / "data"
    new_images = new_root / "images"
    new_data.mkdir(parents=True, exist_ok=True)
    new_images.mkdir(parents=True, exist_ok=True)

    # 复制 data/ 到新位置
    if DATA_DIR.exists():
        for child in DATA_DIR.iterdir():
            dest = new_data / child.name
            if not dest.exists():
                if child.is_dir():
                    shutil.copytree(str(child), str(dest))
                else:
                    shutil.copy2(str(child), str(dest))

    # 移动 images/ 到新位置
    if IMAGES_DIR.exists():
        for child in IMAGES_DIR.iterdir():
            dest = new_images / child.name
            if not dest.exists():
                shutil.move(str(child), str(dest))

    # 持久化到 .env
    content = env_file.read_text(encoding='utf-8') if env_file.exists() else ""
    new_val = str(new_root.resolve())

    if 'DATA_ROOT=' in content:
        # 用字符串替换，避免 re.sub 在 Windows 路径上的转义问题
        lines = content.split('\n')
        lines = [f'DATA_ROOT={new_val}' if l.startswith('DATA_ROOT=') else l for l in lines]
        content = '\n'.join(lines)
    else:
        content += f'\nDATA_ROOT={new_val}\n'

    env_file.write_text(content, encoding='utf-8')
    os.environ["DATA_ROOT"] = new_val

    # 运行时更新模块级变量
    import src.config
    src.config.DATA_ROOT = new_root
    src.config.DATA_DIR = new_data
    src.config.IMAGES_DIR = new_images
```

**注意**：`set_images_dir` 函数完全删除，由 `set_data_root` 替代。

---

### Task 2: 修复 scheduler.py — 删除崩溃代码

**Files:**
- Modify: `src/scheduler.py`

- [ ] **Step 1: 删除 `tracker.downloader` 三行**

```python
# 当前 scheduler.py 的 check_and_download 函数（第9-18行）替换为：
def check_and_download(tracker):
    """定时任务：检查更新并下载新作品。"""
    try:
        tracker.check_updates()
    except Exception:
        pass  # 定时任务静默处理错误，避免影响主进程
```

`tracker.check_updates()` 内部已经对新作品执行下载，不需要额外调用 `download_pending`。

---

### Task 3: 修复 tracker.py — _fetch_new_illusts 全量比对

**Files:**
- Modify: `src/tracker.py`

- [ ] **Step 1: 替换 `_fetch_new_illusts` 方法**

将 `_fetch_new_illusts`（第128-140行）替换为：

```python
def _fetch_new_illusts(self, session, artist):
    """检查并保存画师的新作品。全量拉取后比对，不依赖 API 返回顺序。"""
    existing_ids = {
        row[0]
        for row in session.query(Illustration.pixiv_illust_id)
        .filter_by(artist_id=artist.id)
        .all()
    }

    count = 0
    for illust_data in self.client.get_all_artist_illusts(artist.pixiv_user_id):
        if illust_data["illust_id"] not in existing_ids:
            self._save_illust(session, artist, illust_data)
            count += 1
    return count
```

核心变化：先一次性查询已有的所有 `pixiv_illust_id` 放到 set，然后逐个比对。不依赖 API 顺序，不会因 break 漏检。

---

### Task 4: 修复 web.py — 动态读取 IMAGES_DIR

**Files:**
- Modify: `src/web.py`

- [ ] **Step 1: serve_image 改为通过 config 模块动态读取路径**

当前 `serve_image`（第63-70行）使用 `from .config import IMAGES_DIR`，这会在模块缓存层面缓存旧值。改为通过模块引用：

将 web.py 顶部的 import 加上 `config` 模块引用：

```python
# 第7行，在现有 import 后追加
from . import config as app_config
```

然后将 `serve_image` 函数改为：

```python
@app.get("/images/{path:path}")
async def serve_image(path: str):
    file_path = app_config.IMAGES_DIR / path
    if file_path.exists() and file_path.is_file():
        return FileResponse(str(file_path))
    from starlette.responses import Response
    return Response(status_code=404)
```

---

### Task 5: 更新 settings 路由和模板

**Files:**
- Modify: `src/routes/settings.py`
- Modify: `src/templates/settings.html`

- [ ] **Step 1: 重构 settings.py — 路径相关改为 DATA_ROOT**

`_settings_context` 中的 `images_dir` 改为 `data_root`，`change_path` 改为调用 `set_data_root`，去掉 DB 路径更新逻辑（web 前缀不变）。

将整个 `src/routes/settings.py` 替换为：

```python
from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse

from ..web import get_tracker, templates
from ..models import Session, TrackedArtist, Illustration
from ..config import DATA_ROOT, set_data_root

router = APIRouter(prefix="/settings", tags=["settings"])


def _settings_context():
    from ..config import CHECK_INTERVAL_HOURS, CHECK_INTERVAL_MINUTES, PORT
    session = Session()
    artist_count = session.query(TrackedArtist).count()
    illust_count = session.query(Illustration).count()
    downloaded = session.query(Illustration).filter(Illustration.file_paths != None).count()  # noqa: E711
    session.close()
    return {
        "artist_count": artist_count,
        "illust_count": illust_count,
        "downloaded": downloaded,
        "check_interval_hours": CHECK_INTERVAL_HOURS,
        "check_interval_minutes": CHECK_INTERVAL_MINUTES,
        "port": PORT,
        "data_root": str(DATA_ROOT),
    }


@router.get("")
async def settings_page(request: Request):
    return templates.TemplateResponse(request, "settings.html", _settings_context())


@router.post("/check-now")
async def check_now():
    tracker = get_tracker()
    if tracker:
        tracker.check_updates()
        tracker.download_pending()
    return RedirectResponse("/settings", status_code=303)


@router.post("/download-pending")
async def download_pending():
    tracker = get_tracker()
    if tracker:
        tracker.download_pending()
    return RedirectResponse("/settings", status_code=303)


@router.post("/change-path")
async def change_path(request: Request, new_path: str = Form(...)):
    new_path = new_path.strip()
    if not new_path:
        return RedirectResponse("/settings", status_code=303)

    try:
        set_data_root(new_path)
    except Exception as e:
        return templates.TemplateResponse(
            request, "settings.html",
            _settings_context() | {"error": f"迁移失败: {e}"}
        )

    return RedirectResponse("/settings", status_code=303)
```

- [ ] **Step 2: 更新 settings.html — 表单文字**

将模板中 "图片存储路径" section（第39-45行）替换为：

```html
<div class="settings-section">
    <h3>数据存储目录</h3>
    <p>所有数据（数据库、token、作品图片）存放在此目录下。当前路径：<code>{{ data_root }}</code></p>
    <p style="color:#888;font-size:13px;">目录内部结构：<code>data/</code>（数据库） + <code>images/</code>（作品图片）</p>
    <form method="post" action="/settings/change-path" class="add-form">
        <input type="text" name="new_path" placeholder="输入新路径，如 D:/pixiv_data" value="{{ data_root }}">
        <button type="submit">修改路径</button>
    </form>
</div>
```

同时把模板中所有 `images_dir` 引用改为 `data_root`（检查是否有其他地方用到）。

---

### Task 6: 提交并验证

- [ ] **Step 1: 提交所有改动**

```bash
git add src/config.py src/scheduler.py src/tracker.py src/web.py src/routes/settings.py src/templates/settings.html
git commit -m "feat: 统一数据目录 DATA_ROOT + 修复 3 个 bug

- 新增 DATA_ROOT 配置项，DATA_DIR/images 从其推导
- 首次启动自动迁移旧 IMAGES_DIR 配置
- 修复 set_images_dir 中 re.error（Windows 路径含反斜杠）
- 修复 scheduler 中 tracker.downloader 不存在导致的静默崩溃
- 修复 _fetch_new_illusts 按顺序 break 可能漏检新作品"
```

- [ ] **Step 2: 验证 — 默认启动**

```bash
python -m src.web
```

确认：
- 服务正常启动，无报错
- `DATA_ROOT` 未设置时，`data/` 和 `images/` 在项目根目录下
- Web 界面正常显示，已有数据可见

- [ ] **Step 3: 验证 — 迁移已有安装**

模拟升级场景：
```bash
# 1. 备份当前 .env
cp .env .env.bak

# 2. 将 .env 改成旧格式（有 IMAGES_DIR 无 DATA_ROOT）
echo "IMAGES_DIR=images" > .env  # 仅在 Linux/bash
# Windows 下手动编辑 .env，删掉 DATA_ROOT 行，加 IMAGES_DIR=images

# 3. 重启，观察迁移日志（如有）
python -m src.web

# 4. 检查 .env 已自动添加 DATA_ROOT=.
cat .env

# 5. 恢复
mv .env.bak .env
```

确认 `.env` 自动添加了 `DATA_ROOT=.` 且移除了 `IMAGES_DIR=`。

- [ ] **Step 4: 验证 — settings 页修改路径**

1. 启动服务，打开 http://localhost:8000/settings
2. 修改数据存储目录为一个新路径（如 `./test_data`）
3. 确认新路径下创建了 `data/` 和 `images/` 子目录
4. 确认旧数据已迁移（数据库、token、图片）
5. 确认 `.env` 中 `DATA_ROOT` 已更新

- [ ] **Step 5: 验证 — Windows 路径不崩溃**

在 settings 页输入含反斜杠的绝对路径（如 `D:\test_pixiv_data`），确认不报 500。

- [ ] **Step 6: 验证 — 定时任务不崩溃**

启动后等待一个检查周期（或手动触发 `/settings/check-now`），确认 scheduler 正常执行，无异常日志。

- [ ] **Step 7: 恢复原 .env**

```bash
mv .env.bak .env
```
