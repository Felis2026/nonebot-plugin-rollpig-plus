from __future__ import annotations

from nonebot import get_plugin_config

from ..config import Config
from .base import RollpigStore


def build_store() -> RollpigStore:
    config = get_plugin_config(Config)
    backend = (config.rollpig_storage_backend or "local").strip().lower()
    if backend == "cloud":
        from .cloud import CloudStore

        return CloudStore()

    from ..services.data_manager import get_data_manager
    from .local_json import LocalJsonStore

    return LocalJsonStore(get_data_manager)


store = build_store()
