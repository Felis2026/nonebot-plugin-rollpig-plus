from __future__ import annotations

from pathlib import Path


# ================================ 包内资源路径 ================================ #
# 这些路径必须以插件包根目录为基准。渲染器和服务模块被移动到子目录后，
PACKAGE_DIR = Path(__file__).parent
RESOURCE_DIR = PACKAGE_DIR / "resource"

