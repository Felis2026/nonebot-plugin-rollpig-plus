import json
import random
import asyncio
from pathlib import Path
from typing import Optional, Dict, List
from urllib.parse import urlparse

import nonebot_plugin_localstore as store
from nonebot import get_plugin_config, logger
from openai import AsyncOpenAI

from .config import Config

# 数据文件
ROAST_LIB_FILE = store.get_plugin_data_file("roast_library.json")


def _clamp_number(value: object, default: float, minimum: float, maximum: float) -> float:
    """把外部配置收敛到安全区间，避免极端值拖垮事件循环或 API 账单。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _clamp_int(value: object, default: int, minimum: int, maximum: int) -> int:
    return int(_clamp_number(value, default, minimum, maximum))


# ================================ DeepSeek V4 兼容 ================================ #
# DeepSeek 旧模型名会在 2026-07-24 弃用；这里在运行时兼容旧配置，
# 同时只对官方 API 注入 thinking 扩展字段，避免第三方 OpenAI 网关不兼容。
def _is_deepseek_official_base(base_url: str) -> bool:
    """仅对 DeepSeek 官方地址注入 V4 私有参数，避免破坏第三方 OpenAI 兼容网关。"""

    try:
        parsed = urlparse((base_url or "").strip())
    except Exception:
        return False
    return parsed.scheme in {"http", "https"} and parsed.netloc.lower() == "api.deepseek.com"


def _resolve_deepseek_model(model: str, base_url: str, *, warn: bool = True) -> tuple[str, dict | None]:
    """兼容 DeepSeek 旧模型名，并为 V4 Flash 短文案默认关闭思考模式。"""

    configured_model = (model or "").strip() or "deepseek-v4-flash"
    normalized_model = configured_model.lower()
    is_official = _is_deepseek_official_base(base_url)

    if normalized_model == "deepseek-chat":
        if not is_official:
            return configured_model, None
        if warn:
            logger.warning(
                "检测到旧 DeepSeek 模型名 deepseek-chat，已自动兼容为 deepseek-v4-flash 非思考模式；"
                "建议更新 rollpig_model 配置。"
            )
        return "deepseek-v4-flash", {"thinking": {"type": "disabled"}}

    if normalized_model == "deepseek-reasoner":
        if not is_official:
            return configured_model, None
        if warn:
            logger.warning(
                "检测到旧 DeepSeek 模型名 deepseek-reasoner，已自动兼容为 deepseek-v4-flash 思考模式；"
                "AI 烤猪短文案建议改用 deepseek-v4-flash 非思考模式。"
            )
        return "deepseek-v4-flash", {"thinking": {"type": "enabled"}}

    if normalized_model == "deepseek-v4-flash":
        return configured_model, {"thinking": {"type": "disabled"}} if is_official else None

    return configured_model, None

# ================= 默认兜底文案模板 =================
DEFAULT_TEMPLATES = [
    "你本是一只无忧无虑的【{origin}】，却没能逃过命运的安排，含泪变成了【{food}】。",
    "看看你现在的样子！虽然不再是【{origin}】，但作为【{food}】的你，依然散发着诱人的光泽。",
]

BURNT_TEMPLATES = [
    "住手！它已经是一块【{origin}】了！在你无情的二次烧烤下，它彻底变成了黑漆漆的焦炭。",
    "你还不满足吗？这块可怜的【{origin}】已经被你烤得面目全非，化作了尘埃。",
]

PVP_TEMPLATES = [
    "【{k}】手法娴熟，手起刀落，将【{v}】（{origin}）做成了美味的【{food}】！",
    "【{v}】还没反应过来，就被【{k}】扔上了烤架。再见了，{origin}；你好，{food}。",
]

class RoastManager:
    def __init__(self, config: Config | None = None):
        # 允许测试或后续工厂函数显式注入配置；运行时不传则沿用 NoneBot 当前插件配置。
        plugin_config = config or get_plugin_config(Config)
        self.file = ROAST_LIB_FILE
        self.library: Dict[str, Dict[str, List[str]]] = self._load()
        # AI 文案可能由多个群同时触发生成；保存文案库时必须串行化，
        # 否则未来改成线程写或多路保存时容易出现覆盖/半写风险。
        self._lock = asyncio.Lock()
        
        self.client = None
        self.ai_timeout = _clamp_number(plugin_config.rollpig_ai_timeout, 20.0, 1.0, 60.0)
        self.ai_max_tokens = _clamp_int(plugin_config.rollpig_ai_max_tokens, 4096, 64, 4096)
        self.ai_output_max_chars = _clamp_int(plugin_config.rollpig_ai_output_max_chars, 240, 40, 600)
        self._ai_semaphore = asyncio.Semaphore(
            _clamp_int(plugin_config.rollpig_ai_concurrency, 4, 1, 6)
        )
        # AI 只有在“开关开启 + key 存在”时才会启用。
        self.ai_ready = bool(plugin_config.rollpig_ai_enabled and plugin_config.rollpig_deepseek_key)
        self.ai_model, self.ai_extra_body = _resolve_deepseek_model(
            plugin_config.rollpig_model,
            plugin_config.rollpig_deepseek_base,
            warn=self.ai_ready,
        )
        if self.ai_ready:
            self.client = AsyncOpenAI(
                api_key=plugin_config.rollpig_deepseek_key,
                base_url=plugin_config.rollpig_deepseek_base,
            )

    # ================================ 文案库持久化 ================================ #
    # roast_library.json 是 AI 生成文案的可增长缓存。这里参考 data_manager.py：
    # 写入动作放到线程，避免阻塞 NoneBot 事件循环；通过临时文件 replace 保证落盘原子性。
    def _load(self) -> dict:
        if not self.file.exists(): return {}
        try:
            return json.loads(self.file.read_text("utf-8"))
        except Exception as e:
            logger.warning(f"roast_library.json 读取失败，已使用空文案库兜底: {e}")
            return {}

    def _sync_save(self):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_name(f"{self.file.name}.{id(self)}.tmp")
        tmp.write_text(json.dumps(self.library, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.file)

    async def _save_new_text(self, origin_id: str, target_id: str, text: str):
        async with self._lock:
            if origin_id not in self.library: self.library[origin_id] = {}
            if target_id not in self.library[origin_id]: self.library[origin_id][target_id] = []
            if text not in self.library[origin_id][target_id]:
                self.library[origin_id][target_id].append(text)
                await asyncio.to_thread(self._sync_save)

    def _format_text(self, text: str, origin: str, food: str, killer: str = None, victim: str = None) -> str:
        res = text.replace("{origin}", origin).replace("{food}", food)
        k_name = killer if killer else "神秘人"
        v_name = victim if victim else "倒霉蛋"
        res = res.replace("{k}", k_name).replace("{v}", v_name)
        return res

    async def get_roast_text(self, origin_pig: dict, target_food: dict, 
                             operator_name: str = None, target_name: str = None) -> str:
        o_id = origin_pig["id"]
        t_id = target_food["id"]
        o_name = origin_pig["name"]
        t_name = target_food["name"]

        # --- 场景 1: 焦炭 ---
        # 该分支目前主要用于兼容/扩展；当前命令层默认不再触发“熟食再烤”。
        if t_id == "burnt":
            if self.ai_ready and self.client:
                try:
                    text = await self._call_ai(origin_pig, target_food, is_burnt=True)
                    return self._format_text(text, o_name, t_name)
                except Exception as e:
                    logger.warning(f"焦炭文案 AI 生成失败，回落本地模板: {e}")
            return random.choice(BURNT_TEMPLATES).format(origin=o_name)
        
        # --- 场景 2 & 3: PvP / PvE ---
        lookup_t_id = t_id + ("_pvp" if operator_name else "")
        local_texts = self.library.get(o_id, {}).get(lookup_t_id, [])
        
        # 只有 AI 已开启且可用时，才允许新增文案生成
        should_generate = self.ai_ready and (
            (not local_texts) or (len(local_texts) < 3 and random.random() < 0.4)
        )

        template_text = None
        if should_generate:
            try:
                template_text = await self._call_ai(origin_pig, target_food, is_pvp=bool(operator_name))
                if template_text:
                    await self._save_new_text(o_id, lookup_t_id, template_text)
            except Exception as e:
                logger.error(f"AI 生成失败: {e}")

        if not template_text and local_texts:
            template_text = random.choice(local_texts)
            
        if not template_text:
            template_text = random.choice(PVP_TEMPLATES) if operator_name else random.choice(DEFAULT_TEMPLATES)

        return self._format_text(template_text, o_name, t_name, operator_name, target_name)

    async def _call_ai(self, origin_pig: dict, target_food: dict, is_pvp: bool = False, is_burnt: bool = False) -> str:
        if not self.client:
            raise RuntimeError("AI client is not initialized")
        
        # 1. 提取特征
        origin_feature = origin_pig.get('description', '')
        if not origin_feature or len(origin_feature) > 15:
            origin_feature = origin_pig['analysis'][:20]

        # 基础 System Prompt
        system_prompt = "你是一个擅长黑色幽默、说话刻薄但好笑的脱口秀演员。你的任务是进行‘猪生终结’吐槽。"

        # === 场景 A: 变成焦炭 (二次烧烤兼容分支) ===
        if is_burnt:
            prompt = (
                f"【吐槽对象】：一块已经是美食的【{origin_pig['name']}】，被贪婪的人类再次放上烤架，彻底烤成了【焦炭/致癌物】。\n"
                f"请写一段40字以内的毒舌吐槽。\n\n"
                f"严格遵守【对比公式】：\n"
                f"“曾经你(美食状态)...如今你(焦炭状态)...”\n\n"
                f"参考范例：\n"
                f"- “曾经你是鲜嫩多汁的培根，如今却变成了一块用来画眉毛的木炭。人类的贪婪真是你的火葬场。”\n"
                f"要求：风格地狱笑话，尖酸刻薄，严禁客套。"
            )

        # === 场景 B: 烤群友 PvP ===
        elif is_pvp:
            prompt = (
                f"【吐槽对象】：凶手把受害者（本体【{origin_pig['name']}】，特征：{origin_feature}）残忍地做成了【{target_food['name']}】。\n"
                f"请写一段40字以内的解说，**必须使用占位符**：{{k}}代表凶手，{{v}}代表受害者。\n\n"
                f"严格遵守【对比公式】：\n"
                f"“{{k}} (动作)... 把 {{v}} (惨状/前世特征)... 变成了 (今生美食)...”\n\n"
                f"参考范例：\n"
                f"- “{{k}} 没给 {{v}} 任何辩解的机会。前一秒它还是只特立独行的野猪，下一秒就成了 {{k}} 盘子里滋滋作响的五花肉。”\n"
                f"- “{{k}} 的手艺真是‘惊天地泣鬼神’，硬生生把 {{v}} 这只大懒猪，炼成了一锅香喷喷的猪油。”\n"
                f"要求：既要体现受害者惨状，又要调侃凶手，必须包含 {{k}} 和 {{v}}。"
            )

        # === 场景 C: 标准烤猪 PvE ===
        else:
            prompt = (
                f"现在进行一场【猪生终结吐槽大会】。\n"
                f"对象前世：【{origin_pig['name']}】（特征：{origin_feature}）\n"
                f"对象今生：【{target_food['name']}】\n\n"
                
                f"请写一段40字以内的神吐槽。必须严格遵守以下【对比公式】：\n"
                f"“曾经你(前世特征/地位)...如今你(死后状态/口感)...”\n\n"
                
                f"参考范例（学习这种语气）：\n"
                f"- “曾经你是丛林里的一方霸主野猪，如今却成为培根在我的平底锅里滋滋作响。别说，比起你的獠牙，还是你的油脂更迷人。”\n"
                f"- “生前你是个除了吃就是睡的大懒猪，没想到变成红烧肉后，这层肥膘反而成了精华，真是懒猪有懒福。”\n\n"
                
                f"要求：\n"
                f"1. 必须同时提到“生前”和“死后”的反差。\n"
                f"2. 风格要毒舌、幽默、带点地狱笑话，不要纯夸好吃。\n"
                f"3. 严禁出现“这道菜”、“这道美食”这种客套话，直接对话（用“你”）。"
            )

        try:
            # OpenAI 兼容接口可能在网络抖动时长时间挂起；这里用本地超时和并发闸门
            # 保护 NoneBot 事件循环，失败后由调用方回落本地模板。
            async with self._ai_semaphore:
                request_kwargs = {
                    "model": self.ai_model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                    "stream": False,
                    "max_tokens": self.ai_max_tokens,
                }
                if self.ai_extra_body is not None:
                    # DeepSeek V4 的 thinking 是官方 OpenAI 兼容接口扩展字段；
                    # 第三方网关不一定支持，所以只在解析阶段确认安全时才注入。
                    request_kwargs["extra_body"] = self.ai_extra_body

                response = await asyncio.wait_for(
                    self.client.chat.completions.create(**request_kwargs),
                    timeout=self.ai_timeout,
                )
            usage = getattr(response, "usage", None)
            if usage:
                logger.info(
                    "AI 烤猪 token 用量: "
                    f"prompt={getattr(usage, 'prompt_tokens', None)} "
                    f"completion={getattr(usage, 'completion_tokens', None)} "
                    f"total={getattr(usage, 'total_tokens', None)} "
                    f"max_tokens={self.ai_max_tokens}"
                )
            content = response.choices[0].message.content
            if not isinstance(content, str) or not content.strip():
                raise ValueError("AI empty response")
            text = content.strip().strip('"').strip("'").replace("\n", "")
            return text[: self.ai_output_max_chars]
        except Exception as e:
            logger.error(f"DeepSeek API 请求错误: {e}")
            raise e

roast_manager = RoastManager()
