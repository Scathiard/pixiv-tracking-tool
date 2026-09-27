# 统一数据目录 & Bug 修复

## 目标

将分散的 `DATA_DIR`（数据库/token）和 `IMAGES_DIR`（作品图片）合并为一个可配置的 `DATA_ROOT`，同时修复 3 个已知 bug。

## 动机

- **改 images 路径不迁移数据库**：全新安装后想读取旧订阅数据，需要手动从旧目录复制 `data/` 到新位置
- **set_images_dir 的 re.sub 在 Windows 路径下崩溃**：反斜杠被当作转义序列，导致 500 Internal Server Error
- **scheduler 定时任务静默崩溃**：`tracker.downloader` 属性不存在
- **新作品检测有漏检风险**：遇到第一个已存在记录就 break

## 配置模型

### 之前

```
DATA_DIR   = PROJECT_ROOT / os.getenv("DATA_DIR", "data")
IMAGES_DIR = _resolve_images_dir()   # 来自 IMAGES_DIR 环境变量
```

两个独立的环境变量，各自配置。

### 之后

```
DATA_ROOT   = _resolve_data_root()   # 来自 DATA_ROOT 环境变量，默认 PROJECT_ROOT
DATA_DIR    = DATA_ROOT / "data"     # 数据库、token、gallery-dl archive
IMAGES_DIR  = DATA_ROOT / "images"   # 画师作品图片
```

单个 `DATA_ROOT` 控制一切，内部保持分层结构。

**.env 示例：**
```
DATA_ROOT=D:/pixiv_data
```

默认值 `DATA_ROOT=.`（即项目根目录），与现有行为完全兼容。

### 兼容已有安装

首次用新代码启动时，若 `.env` 中有旧的 `IMAGES_DIR` 但无 `DATA_ROOT`，通过启发式推导出 `DATA_ROOT`：

- 若 `IMAGES_DIR` 在 `PROJECT_ROOT` 内 → `DATA_ROOT` 就是 `PROJECT_ROOT`
- 若 `IMAGES_DIR` 在外部（如 `D:/pixiv_images`）且内部有 `data/` 子目录 → `DATA_ROOT` 就是该外部路径的父级
- 否则 → `DATA_ROOT` 取 `IMAGES_DIR` 的父目录

推导后将 `DATA_ROOT` 写入 `.env`，移除旧的 `IMAGES_DIR`/`DATA_DIR` 配置项。

## 迁移流程

Settings 页修改 `DATA_ROOT` 时：

1. 创建新 `DATA_ROOT/data/` 和 `DATA_ROOT/images/`
2. 复制旧 `data/` 目录所有内容到新位置
3. 复制旧 `images/` 目录所有内容到新位置
4. 更新数据库中所有 `file_paths`，替换路径前缀
5. 写入新的 `DATA_ROOT` 到 `.env`
6. 运行时更新 `config.DATA_ROOT`、`config.DATA_DIR`、`config.IMAGES_DIR`

失败时原数据不受影响（先复制后切换的策略）。

## Bug 修复

### 1. set_images_dir → set_data_root，修复 re.error

**现状 (config.py):**
```python
def set_images_dir(new_path):
    content = re.sub(r'IMAGES_DIR=.*', f'IMAGES_DIR={new_path}', content)
```

**问题**：Windows 路径中的 `\p`、`\d` 等被 re.sub 的 replacement 字符串当作转义序列。

**修复**：用 `str.replace` 替代 `re.sub`，或是对 replacement 调用 `re.escape`。项目实际需求只是替换一行，用字符串匹配即可。

函数重命名为 `set_data_root`，逻辑改为更新 `DATA_ROOT=...` 行。

### 2. scheduler.py 崩溃

**现状 (scheduler.py:14-16):**
```python
session = Session()
tracker.downloader.download_pending(session)
session.close()
```

**问题**：`Tracker` 没有 `downloader` 属性。`check_updates()` 内部已对新作品执行下载，这两行完全多余。

**修复**：直接删除这三行。

### 3. _fetch_new_illusts 漏检

**现状 (tracker.py:130-140):**
```python
for illust_data in self.client.get_all_artist_illusts(artist.pixiv_user_id):
    exists = session.query(...).filter_by(pixiv_illust_id=...).first()
    if exists:
        break  # 遇到第一个已存在的就停
```

**问题**：若 Pixiv API 返回顺序变化，新作品出现在旧作品之前，会导致新作品被 break 跳过。

**修复**：先拉取全部作品 ID 列表，一次性查询数据库中已有的 ID 集合，然后只保存不存在的。去掉 break 的短路逻辑。

## 涉及文件

| 文件 | 改动 |
|------|------|
| `src/config.py` | 新增 `DATA_ROOT`，`set_images_dir` → `set_data_root`，修复 re.error，添加兼容迁移逻辑 |
| `src/scheduler.py` | 删除 `tracker.downloader` 三行 |
| `src/tracker.py` | `_fetch_new_illusts` 改为全量比对，去掉 break |
| `src/routes/settings.py` | 路径修改 endpoint 改为设置 `DATA_ROOT`，UI 文字从"图片存储路径"改为"数据存储目录" |
| `src/templates/settings.html` | 修改路径表单的 label 和说明文字 |
| `src/auth.py` | 无需改动（`configure_gallery_dl` 依赖 `IMAGES_DIR`，后者仍从 `DATA_ROOT` 推导） |

## 不做

- 日志系统、分页、session 管理重构——独立迭代
- `data/` 目录内的 `.db` 文件迁移时数据库连接不会中断（SQLite 是文件级锁，复制是读操作，安全）

## 验证

- 启动 `python -m src.web`，确认默认行为不变（`DATA_ROOT` 未设置时等同于项目根目录）
- settings 页修改路径，确认 data/ 和 images/ 全部迁移
- 添加画师 + 下载作品，确认文件落地到新位置
- Windows 路径（含反斜杠和中文）设置 `DATA_ROOT`，确认不报 500
- 定时任务运行一次，确认 scheduler 不崩溃
