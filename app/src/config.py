import os
import sys
import shutil
import threading
from pathlib import Path
from dotenv import load_dotenv

from .data_migration import execute_migration, persist_data_root, prepare_migration

_env_lock = threading.RLock()

# PyInstaller 打包后资源在 _MEIPASS 临时目录，数据在可执行文件目录
if getattr(sys, 'frozen', False):
    BUNDLE_DIR = Path(sys._MEIPASS)
    PROJECT_ROOT = Path(sys.executable).parent
    APP_DIR = BUNDLE_DIR  # 打包后所有资源在 _MEIPASS
else:
    BUNDLE_DIR = None
    PROJECT_ROOT = Path(__file__).parent.parent.parent  # 仓库根目录
    APP_DIR = Path(__file__).parent.parent  # app/ 目录


def resource_path(relative_path):
    """获取资源文件路径，兼容 PyInstaller 打包。"""
    if BUNDLE_DIR:
        return str(BUNDLE_DIR / relative_path)
    return str(APP_DIR / relative_path)


env_file = (PROJECT_ROOT if getattr(sys, 'frozen', False) else APP_DIR) / ".env"
if env_file.exists():
    load_dotenv(env_file, encoding='utf-8')


def _resolve_data_root():
    v = os.getenv("DATA_ROOT", "").strip()
    if v:
        p = Path(v)
        if p.is_absolute():
            return p
        return PROJECT_ROOT / p
    if getattr(sys, 'frozen', False):
        return PROJECT_ROOT
    return APP_DIR


def _migrate_to_data_root():
    """首次用新代码启动时：若 .env 有旧的 IMAGES_DIR 但无 DATA_ROOT，自动迁移。"""
    if not env_file.exists():
        return
    content = env_file.read_text(encoding='utf-8')
    if any(line.strip().startswith('DATA_ROOT=') for line in content.split('\n')):
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

    # 目标路径
    _default_root = PROJECT_ROOT if getattr(sys, 'frozen', False) else APP_DIR
    new_images = _default_root / "images"
    new_data = _default_root / "data"

    new_images.mkdir(parents=True, exist_ok=True)
    new_data.mkdir(parents=True, exist_ok=True)

    # 旧版本升级只复制、不删除源数据；发生冲突时保留源文件供人工核对。
    if old_images_path.resolve() != new_images.resolve() and old_images_path.exists():
        for child in old_images_path.iterdir():
            dest = new_images / child.name
            if not dest.exists():
                if child.is_dir():
                    shutil.copytree(str(child), str(dest))
                else:
                    shutil.copy2(str(child), str(dest))

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
    lines = [line for line in lines
             if not line.startswith('IMAGES_DIR=')
             and not line.startswith('DATA_DIR=')]
    migrated_root = str(_default_root.resolve())
    content = '\n'.join(lines).rstrip('\n') + f'\nDATA_ROOT={migrated_root}\n'
    temporary = env_file.with_name(env_file.name + '.tmp')
    temporary.write_text(content, encoding='utf-8')
    os.replace(temporary, env_file)

    # 重载环境变量
    os.environ["DATA_ROOT"] = migrated_root
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
    """复制并验证数据到空目录，成功后才原子切换 DATA_ROOT。"""
    new_path = new_path.strip()
    new_root = Path(new_path)
    if not new_root.is_absolute():
        new_root = PROJECT_ROOT / new_root
    new_root = new_root.resolve()
    if new_root == DATA_ROOT.resolve():
        return None

    plan = prepare_migration(DATA_ROOT, new_root)
    report = execute_migration(plan)
    persist_data_root(env_file, new_root)

    new_data = new_root / "data"
    new_images = new_root / "images"
    new_val = str(new_root)
    os.environ["DATA_ROOT"] = new_val

    # 运行时更新模块级变量
    import src.config
    src.config.DATA_ROOT = new_root
    src.config.DATA_DIR = new_data
    src.config.IMAGES_DIR = new_images
    src.config.DATABASE_URL = f"sqlite:///{new_data / 'pixiv_tracker.db'}"
    return report


# Pixiv auth
PIXIV_REFRESH_TOKEN = os.getenv("PIXIV_REFRESH_TOKEN", "")

# Server（IPv4/IPv6 固定双栈监听；这里只配置两者共用的端口）
PORT = int(os.getenv("PORT", "1919"))


def set_port(new_port):
    """修改端口号并持久化到 .env，需要重启生效。"""
    new_port = int(new_port)
    if not 1024 <= new_port <= 65535:
        raise ValueError("端口必须在 1024 到 65535 之间")
    _persist_env_values({"PORT": str(new_port)})
    os.environ["PORT"] = str(new_port)
    import src.config
    src.config.PORT = new_port


def set_pagination(page_size, pagination_mode, max_visible_items=0):
    """校验并原子持久化画廊分页参数。"""
    page_size = int(page_size)
    max_visible_items = int(max_visible_items)
    if page_size not in {50, 100, 200, 500}:
        raise ValueError("每页数量只允许 50、100、200 或 500")
    if pagination_mode not in {"scroll", "paginate"}:
        raise ValueError("未知的翻页模式")
    if max_visible_items < 0 or max_visible_items > 10000:
        raise ValueError("最大可见数量必须在 0 到 10000 之间")

    values = {
        "PAGE_SIZE": str(page_size),
        "PAGINATION_MODE": pagination_mode,
        "MAX_VISIBLE_ITEMS": str(max_visible_items),
    }
    _persist_env_values(values)
    os.environ.update(values)
    import src.config
    src.config.PAGE_SIZE = page_size
    src.config.PAGINATION_MODE = pagination_mode
    src.config.MAX_VISIBLE_ITEMS = max_visible_items


def _persist_env_values(values: dict[str, str]):
    """只替换指定键，并通过临时文件原子更新 .env。"""
    with _env_lock:
        content = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
        lines = content.splitlines()
        remaining = dict(values)
        output = []
        for line in lines:
            stripped = line.strip()
            key, separator, _ = stripped.partition("=")
            if separator and key in values:
                output.append(f"{key}={values[key]}")
                remaining.pop(key, None)
            else:
                output.append(line)
        for key, value in remaining.items():
            output.append(f"{key}={value}")
        rendered = "\n".join(output).rstrip("\n") + "\n"
        env_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = env_file.with_name(env_file.name + ".tmp")
        temporary.write_text(rendered, encoding="utf-8")
        os.replace(temporary, env_file)

# Scheduler
CHECK_INTERVAL_HOURS = int(os.getenv("CHECK_INTERVAL_HOURS", "6"))
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "0")) or None

# Download
DOWNLOAD_WORKERS = int(os.getenv("DOWNLOAD_WORKERS", "3"))
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "1.0"))

# 翻页设置
PAGE_SIZE = int(os.getenv("PAGE_SIZE", "200"))
PAGINATION_MODE = os.getenv("PAGINATION_MODE", "scroll")  # "scroll" | "paginate"
MAX_VISIBLE_ITEMS = int(os.getenv("MAX_VISIBLE_ITEMS", "0"))  # 0 = 不限
