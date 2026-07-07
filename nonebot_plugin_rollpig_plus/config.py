from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from nonebot.log import logger
from pydantic import BaseModel


def _is_json_config_strict() -> bool:
    """仅允许环境变量控制严格模式；配置文件本身坏掉时不能再依赖其中的开关。"""
    return os.getenv("ROLLPIG_CONFIG_STRICT", "").strip().lower() in {"1", "true", "yes", "on"}


def _handle_json_config_error(path: Path, error: Exception) -> dict[str, Any]:
    if _is_json_config_strict():
        raise error
    logger.error(f"rollpig JSON 配置读取失败，已忽略该文件: {path}: {error}")
    return {}


def _load_json_config_file() -> dict[str, Any]:
    """读取可选 JSON 配置；缺失或为空时等价于不配置，不影响插件启动。"""
    env_path = os.getenv("ROLLPIG_CONFIG_FILE", "").strip()
    candidate_paths = [
        Path(env_path) if env_path else None,
        Path.cwd() / "rollpig_config.json",
        Path.cwd() / "config" / "rollpig.json",
    ]
    for raw_path in candidate_paths:
        if raw_path is None:
            continue
        path = raw_path.expanduser()
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError(f"rollpig JSON 配置必须是 object: {path}")
        except (json.JSONDecodeError, OSError, ValueError) as error:
            return _handle_json_config_error(path, error)
        nested = data.get("rollpig")
        if isinstance(nested, dict):
            data = nested
        return {str(key).lower(): value for key, value in data.items()}
    return {}


def _merge_json_config(env_data: dict[str, Any]) -> dict[str, Any]:
    """JSON 负责降低 .env 负担；NoneBot/.env 传入值始终覆盖 JSON。"""
    json_data = _load_json_config_file()
    if not json_data:
        return env_data
    normalized_env = {str(key).lower(): value for key, value in env_data.items() if value is not None}
    return {**json_data, **normalized_env}

class Config(BaseModel):
    def __init__(self, **data: Any):
        super().__init__(**_merge_json_config(data))

    # --- AI 烤猪配置 ---
    rollpig_ai_enabled: bool = False  # 是否开启 AI 生成
    rollpig_deepseek_key: Optional[str] = None  # DeepSeek API Key
    rollpig_deepseek_base: str = "https://api.deepseek.com" # Base URL
    rollpig_model: str = "deepseek-v4-flash" # 模型名称
    rollpig_ai_timeout: float = 20.0  # 单次 AI 生成超时时间（秒）
    rollpig_ai_concurrency: int = 4  # AI 文案生成并发上限
    rollpig_ai_max_tokens: int = 4096  # AI 单次响应 token 上限
    rollpig_ai_output_max_chars: int = 240  # AI 文案入库前的最大字符数
    rollpig_roast_cooldown_hours: float = 8.0  # 烤群友普通模式冷却时长（小时）
    rollpig_roast_charge_max: int = 2  # 普通烤群友最多可储存次数
    rollpig_storage_backend: str = "local"  # local / cloud
    rollpig_cloud_api_url: Optional[str] = None
    rollpig_cloud_token: Optional[str] = None
    rollpig_cloud_timeout: float = 3.0
    rollpig_cloud_strict_mode: bool = True  # true=云端异常直接失败；false=读接口可安全兜底，写接口仍提示稍后重试

    # --- 小猪资源云端同步 ---
    # 默认指向 FelisLab 静态资源包；同步失败时只回退到本地缓存/插件内置资源，不影响 Bot 启动。
    rollpig_resource_sync_enabled: bool = True
    rollpig_resource_manifest_url: str = "https://pig.felislab.cc/resources/rollpig/manifest.json"
    rollpig_resource_sync_interval_hours: int = 24
    rollpig_resource_sync_timeout: float = 10.0
    rollpig_resource_max_file_size: int = 10 * 1024 * 1024
    # 私有资源包是公有全量包之上的 overlay；公开版默认关闭，避免商店用户无感拉取维护者私有资源。
    rollpig_private_resource_manifest_url: Optional[str] = ""
    rollpig_private_resource_token: Optional[str] = None

    # --- 定时日报 ---
    # 关闭后会跳过每日总结定时任务；该任务负责日报推送，以及日报派生的次日保护名单刷新。
    rollpig_daily_summary_enabled: bool = True

    # --- 普通小猪卡片渲染 ---
    # Pillow 不具备浏览器级字体回退；Docker/Linux 缺字或想换风格时可显式指定字体。
    # 相对路径按 Bot 运行目录解析，例如 fonts/msyh.ttc。
    rollpig_card_font_path: Optional[str] = None

    # --- 图片版小猪图鉴 ---
    rollpig_catalog_enabled: bool = True
    rollpig_catalog_render_concurrency: int = 2
    rollpig_catalog_cache_seconds: int = 300
    rollpig_catalog_output_format: str = "png"
    rollpig_catalog_render_timeout: float = 8.0
    rollpig_catalog_scale_factor: float = 2.0

    # --- 代理设置 (可选，如果服务器在国内连不上API) ---
    rollpig_proxy: Optional[str] = None
