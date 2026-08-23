"""在任何测试模块导入应用配置前，隔离默认数据目录。"""
import os
import tempfile


os.environ["DATA_ROOT"] = tempfile.mkdtemp(prefix="pixiv-tracker-pytest-")
