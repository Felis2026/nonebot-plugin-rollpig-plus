import json
import asyncio
import datetime
import shutil
import time
import uuid
from pathlib import Path
from typing import List, Optional

from nonebot.log import logger
import nonebot_plugin_localstore as store

from .runtime import rollpig_date_str, rollpig_today, resolve_roast_cooldown_seconds
from .store.models import (
    CatalogSnapshot,
    CooldownConsumeResult,
    DailyRollResult,
    DrawState,
    PigProgress,
    RoastEvent,
    RoastReservation,
    RoastReservationClaimResult,
    RoastReservationParticipant,
    RoastReservationPrepareResult,
    UnrolledRoastAttemptResult,
)

ROAST_COOLDOWN_SECONDS = resolve_roast_cooldown_seconds()
DEFAULT_ROAST_CHARGE_MAX = 2
ROAST_RESERVATION_MAX_PARTICIPANTS = 12
ROAST_RESERVATION_CLAIM_TIMEOUT_SECONDS = 5 * 60
ROAST_RESERVATION_CROSS_DAY_GRACE_SECONDS = 10 * 60


# ================================ 预约跨日重试 ================================ #


def _matches_reservation_claim_date(raw: dict, target_date: str, now: datetime.datetime) -> bool:
    """匹配当日预约，并为刚在零点后释放的昨日发送前任务保留短暂重试窗口。"""

    reservation_date = str(raw.get("date_str") or "")
    if reservation_date == target_date:
        return True
    if raw.get("status") not in {"ready", "processing", "prepared"}:
        return False
    released_at_raw = raw.get("released_at")
    if not isinstance(released_at_raw, str):
        return False
    try:
        reservation_day = datetime.date.fromisoformat(reservation_date)
        target_day = datetime.date.fromisoformat(target_date)
        released_at = datetime.datetime.fromisoformat(released_at_raw)
    except ValueError:
        return False
    if reservation_day != target_day - datetime.timedelta(days=1):
        return False
    if released_at.tzinfo is None:
        released_at = released_at.replace(tzinfo=datetime.timezone.utc)
    elapsed = (now - released_at).total_seconds()
    return 0 <= elapsed <= ROAST_RESERVATION_CROSS_DAY_GRACE_SECONDS


def _normalize_charge_settings(cooldown_seconds: Optional[int], max_charges: Optional[int]) -> tuple[int, int]:
    """本地 JSON 与 cloud 使用同一组边界，避免两种后端表现不一致。"""
    cooldown = max(60, int(cooldown_seconds or ROAST_COOLDOWN_SECONDS))
    charge_max = max(1, min(6, int(max_charges or DEFAULT_ROAST_CHARGE_MAX)))
    return cooldown, charge_max


def _legacy_roast_state(last_use: float, now: float, cooldown: int, max_charges: int) -> tuple[int, float]:
    """把旧单时间戳 CD 宽松迁移为充能桶：最近一次使用后视为还剩 1 格。"""
    if last_use <= 0:
        return max_charges, now
    elapsed = max(0.0, now - last_use)
    recovered = int(elapsed // cooldown)
    charges = min(max_charges, 1 + recovered)
    updated_ts = now if charges >= max_charges else last_use + recovered * cooldown
    return int(charges), float(updated_ts)


def _recover_roast_charges(charges: int, updated_ts: float, now: float, cooldown: int, max_charges: int) -> tuple[int, float]:
    """按 token bucket 恢复普通烤群友次数；满格后把锚点归到当前时间。"""
    charges = max(0, min(max_charges, int(charges)))
    updated_ts = float(updated_ts or now)
    if charges >= max_charges:
        return max_charges, now
    elapsed = max(0.0, now - updated_ts)
    recovered = int(elapsed // cooldown)
    if recovered <= 0:
        return charges, updated_ts
    charges = min(max_charges, charges + recovered)
    updated_ts = now if charges >= max_charges else updated_ts + recovered * cooldown
    return int(charges), float(updated_ts)


def _next_charge_seconds(charges: int, updated_ts: float, now: float, cooldown: int, max_charges: int) -> int:
    if charges >= max_charges:
        return 0
    elapsed = max(0.0, now - float(updated_ts or now))
    return max(1, int(cooldown - (elapsed % cooldown)))

# ================= 数据管理 =================

DATA_FILE = store.get_plugin_data_file("pig_data.json")
DATA_BACKUP_COUNT = 2


class PigDataManager:
    """
    负责插件所有持久化数据的读写。

    数据结构：
    - history    : {date: {user_id: pig_id}}  ← 新格式，仅存 pig_id（14天后自动清理）
                   旧版存完整 pig dict，_migrate() 会自动转换
    - group_rolls: {date: {group_id: {user_id: pig_id}}} ← 群内“今日已抽/已显形”记录
    - collection : {user_id: [pig_id, ...]}   ← 永久保留，图鉴数据
    - pig_progress: {user_id: {pig_id: {copies, first_obtained_at}}} ← P1A 抽到次数/专家等级
    - draw_state : {user_id: {duplicate_streak}} ← P1A 连续重复次数，用于伪保底
    - usage      : {user_id: {last_roast_ts, roast_charges, roast_charge_updated_ts}} ← 普通烤群友充能
    - force_usage: {user_id: "YYYY-MM-DD"}    ← 后门口令每日计数
    - daily_events: {date: [event, ...]}      ← 群内烧烤事件（用于日报）
    - unrolled_roast_attempts: {date: {user_id: count}} ← 未抽猪先烤的每日违规次数
    - roast_reservations: {reservation_id: reservation} ← 延迟到目标抽猪后结算的群预约

    写操作通过 asyncio.Lock 串行化，文件使用原子替换（.tmp → rename）防止 JSON 损坏。
    """

    def __init__(self):
        self.file = DATA_FILE
        self._lock = asyncio.Lock()
        self._load_failed = False
        self._skip_backup_rotation_once = False
        self.data = self._load()

    # ---- 加载与迁移 ----

    def _default_data(self) -> dict:
        return {
            "history": {},
            "group_rolls": {},
            "collection": {},
            "pig_progress": {},
            "draw_state": {},
            "usage": {},
            "force_usage": {},
            "daily_events": {},
            "protected": {},
            "unrolled_roast_attempts": {},
            "roast_reservations": {},
        }

    def _load(self) -> dict:
        if not self.file.exists():
            default = self._default_data()
            self.file.parent.mkdir(parents=True, exist_ok=True)
            self.file.write_text(json.dumps(default, ensure_ascii=False, indent=2), encoding="utf-8")
            return default
        try:
            raw = json.loads(self.file.read_text("utf-8"))
            return self._migrate(raw)
        except Exception as e:
            self._load_failed = True
            logger.error(f"pig_data.json 读取失败，进入写保护模式以避免覆盖旧数据: {e}")
            self._preserve_broken_file()

            recovered = self._load_backup()
            if recovered is not None:
                logger.warning("pig_data.json 已从备份恢复，并写回主文件。")
                self._load_failed = False
                self.data = recovered
                self._skip_backup_rotation_once = True
                self._sync_save()
                return recovered

            logger.error("pig_data.json 没有可用备份；本地存储写操作将被拒绝，请手动修复数据文件。")
            return self._default_data()

    def _migrate(self, data: dict, *, persist: bool = True) -> dict:
        """将旧版 history（存完整 pig dict）迁移为新版（只存 pig_id 字符串）。
        迁移完成后立即同步落盘，防止进程在第一次写入前已退出导致磁盘仍为旧格式。
        """
        if not isinstance(data, dict):
            data = {}

        migrated = False
        for key in (
            "history",
            "group_rolls",
            "collection",
            "pig_progress",
            "draw_state",
            "usage",
            "force_usage",
            "daily_events",
            "unrolled_roast_attempts",
            "roast_reservations",
        ):
            if not isinstance(data.get(key), dict):
                data[key] = {}
                migrated = True
        if not isinstance(data.get("protected"), dict):
            data["protected"] = {}
            migrated = True

        history = data.get("history", {})
        for date_str, records in history.items():
            if not isinstance(records, dict):
                continue
            for uid, val in list(records.items()):
                if isinstance(val, dict) and "id" in val:
                    records[uid] = val["id"]
                    migrated = True

        # ================================ P1A成长状态回填 ================================ #
        # 旧版本地数据只有 collection，只能确认“曾经拥有过”，无法还原真实重复次数。
        # 因此升级时保守初始化为 copies=1，之后每天首次抽到重复猪才继续递增。
        collection = data.get("collection", {})
        pig_progress = data.setdefault("pig_progress", {})
        draw_state = data.setdefault("draw_state", {})
        for user_id, pig_ids in collection.items():
            if not isinstance(pig_ids, list):
                continue
            user_progress = pig_progress.setdefault(str(user_id), {})
            if not isinstance(user_progress, dict):
                user_progress = {}
                pig_progress[str(user_id)] = user_progress
                migrated = True
            for pig_id in pig_ids:
                pig_id = str(pig_id)
                item = user_progress.get(pig_id)
                if not isinstance(item, dict):
                    user_progress[pig_id] = {"copies": 1, "first_obtained_at": None}
                    migrated = True
                elif _safe_int(item.get("copies"), 0) <= 0:
                    item["copies"] = 1
                    migrated = True
            state = draw_state.get(str(user_id))
            if not isinstance(state, dict):
                draw_state[str(user_id)] = {"duplicate_streak": 0}
                migrated = True
            elif _safe_int(state.get("duplicate_streak"), 0) < 0:
                state["duplicate_streak"] = 0
                migrated = True

        protected = data.get("protected", {})
        if "date" in protected and isinstance(protected.get("users"), list):
            protect_date = str(protected.get("date") or "")
            users = [str(user_id) for user_id in protected.get("users", []) if user_id]
            data["protected"] = {protect_date: {"__all__": users}} if protect_date else {}
            migrated = True
        else:
            normalized_protected: dict[str, dict[str, list[str]]] = {}
            for protect_date, group_map in protected.items():
                if not _is_valid_date(str(protect_date)):
                    continue
                if not isinstance(group_map, dict):
                    continue
                normalized_group_map: dict[str, list[str]] = {}
                for group_id, user_ids in group_map.items():
                    if not isinstance(user_ids, list):
                        continue
                    normalized_group_map[str(group_id)] = [str(user_id) for user_id in user_ids if user_id]
                normalized_protected[str(protect_date)] = normalized_group_map
            if normalized_protected != protected:
                data["protected"] = normalized_protected
                migrated = True

        # ================================ 预约投递状态迁移 ================================ #
        # 旧版本可能在 QQ 消息成功后、completed 落盘前遗留 processing + outcome。
        # 这类记录是否已经发送无法可靠判断，必须按“可能已发送”处理，禁止超时重领。
        for reservation in data.get("roast_reservations", {}).values():
            if (
                isinstance(reservation, dict)
                and reservation.get("status") == "processing"
                and isinstance(reservation.get("outcome_snapshot"), dict)
            ):
                reservation["status"] = "sending"
                migrated = True
        if migrated:
            logger.info("pig_data.json 数据结构已自动迁移/补全，开始落盘...")
            if persist:
                self.data = data
                self._sync_save()  # 迁移后立即落盘，防止重启丢失
        return data

    # ================================ 预约烤猪序列化 ================================ #

    def _reservation_from_raw(self, raw: dict) -> RoastReservation:
        """把持久化字典恢复成只读领域对象；坏参与者记录会被忽略。"""

        participants = tuple(
            RoastReservationParticipant(
                user_id=str(item.get("user_id") or ""),
                display_name=str(item.get("display_name") or ""),
                pig_id=str(item.get("pig_id") or ""),
            )
            for item in raw.get("participants", [])
            if isinstance(item, dict) and item.get("user_id")
        )
        snapshot = raw.get("outcome_snapshot")
        return RoastReservation(
            reservation_id=str(raw.get("reservation_id") or ""),
            date_str=str(raw.get("date_str") or ""),
            group_id=str(raw.get("group_id") or ""),
            target_id=str(raw.get("target_id") or ""),
            target_name=str(raw.get("target_name") or ""),
            owner_id=str(raw.get("owner_id") or ""),
            owner_name=str(raw.get("owner_name") or ""),
            owner_pig_id=str(raw.get("owner_pig_id") or ""),
            participants=participants,
            delivery_bot_id=str(raw.get("delivery_bot_id") or ""),
            force_mode=raw.get("force_mode"),
            status=str(raw.get("status") or "pending"),
            target_pig_id=str(raw.get("target_pig_id") or ""),
            outcome_snapshot=dict(snapshot) if isinstance(snapshot, dict) else None,
            claim_token=str(raw.get("claim_token") or ""),
        )

    def _bind_reservation_event_locked(self, raw: dict, event: RoastEvent) -> RoastEvent:
        """用预约快照覆盖事件身份字段，避免客户端把日报写到错误群或角色名下。"""

        reservation = self._reservation_from_raw(raw)
        return RoastEvent(
            event_type=event.event_type,
            attacker_id=reservation.owner_id,
            target_id=reservation.target_id,
            attacker_name=reservation.owner_name,
            target_name=reservation.target_name,
            food=event.food,
            group_id=reservation.group_id,
            reservation_id=reservation.reservation_id,
            participant_ids=tuple(item.user_id for item in reservation.participants),
            participant_names=tuple(item.display_name for item in reservation.participants),
            participant_count=reservation.participant_count,
            backfire_victim_id=event.backfire_victim_id,
            backfire_victim_name=event.backfire_victim_name,
        )

    def _find_reservation_locked(self, reservation_id: str) -> Optional[dict]:
        raw = self.data.setdefault("roast_reservations", {}).get(str(reservation_id))
        return raw if isinstance(raw, dict) else None

    def _find_pending_reservation_locked(self, date_str: str, group_id: str, target_id: str) -> Optional[dict]:
        for raw in self.data.setdefault("roast_reservations", {}).values():
            if not isinstance(raw, dict):
                continue
            if (
                raw.get("date_str") == date_str
                and raw.get("group_id") == group_id
                and raw.get("target_id") == target_id
                and raw.get("status") == "pending"
            ):
                return raw
        return None

    def _consume_roast_usage_locked(
        self,
        user_id: str,
        *,
        now: float,
        cooldown_seconds: Optional[int],
        max_charges: Optional[int],
    ) -> CooldownConsumeResult:
        """在预约事务已有锁的情况下消费普通充能，避免二次加锁。"""

        cooldown, charge_max = _normalize_charge_settings(cooldown_seconds, max_charges)
        usage = self.data.setdefault("usage", {})
        raw_state = usage.get(user_id, 0)
        if isinstance(raw_state, dict):
            charges = _safe_int(raw_state.get("roast_charges"), 0)
            updated_ts = float(raw_state.get("roast_charge_updated_ts") or now)
        else:
            charges, updated_ts = _legacy_roast_state(float(raw_state or 0), now, cooldown, charge_max)
        charges, updated_ts = _recover_roast_charges(charges, updated_ts, now, cooldown, charge_max)
        if charges <= 0:
            remaining = _next_charge_seconds(charges, updated_ts, now, cooldown, charge_max)
            usage[user_id] = {
                "last_roast_ts": float(raw_state or 0) if not isinstance(raw_state, dict) else raw_state.get("last_roast_ts"),
                "roast_charges": charges,
                "roast_charge_updated_ts": updated_ts,
            }
            return CooldownConsumeResult(False, remaining, 0, charge_max, remaining)

        was_full = charges >= charge_max
        charges -= 1
        if was_full:
            updated_ts = now
        usage[user_id] = {
            "last_roast_ts": now,
            "roast_charges": charges,
            "roast_charge_updated_ts": updated_ts,
        }
        return CooldownConsumeResult(
            True,
            0,
            charges,
            charge_max,
            _next_charge_seconds(charges, updated_ts, now, cooldown, charge_max),
        )

    def _consume_force_usage_locked(self, user_id: str, date_str: str) -> bool:
        usage = self.data.setdefault("force_usage", {})
        if usage.get(user_id) == date_str:
            return False
        usage[user_id] = date_str
        return True

    # ---- 原子写 ----

    def _sync_save(self):
        """同步原子写（仅用于启动期迁移，运行期写操作应使用 _atomic_save）。"""
        self._ensure_writable()
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        self._rotate_backups()
        tmp.replace(self.file)

    async def _atomic_save(self):
        """异步原子写：把 JSON 序列化和磁盘 IO 放到线程，避免阻塞 NoneBot 事件循环。"""
        # 调用方仍在 self._lock 临界区内等待写入完成；这里只是把同步文件 IO 挪出事件循环。
        await asyncio.to_thread(self._sync_save)

    def _ensure_writable(self):
        if self._load_failed:
            raise RuntimeError("pig_data.json 读取失败，已拒绝写入以避免覆盖旧数据。请先修复数据文件或恢复备份。")

    def _backup_paths(self) -> list[Path]:
        return [self.file.with_name(f"{self.file.name}.bak{'' if index == 0 else f'.{index}'}") for index in range(DATA_BACKUP_COUNT + 1)]

    def _rotate_backups(self) -> None:
        """每次成功写入前保留滚动备份；从损坏文件恢复时跳过一次，避免把坏主文件覆盖好备份。"""
        if self._skip_backup_rotation_once:
            self._skip_backup_rotation_once = False
            return
        if not self.file.exists():
            return

        backup_paths = self._backup_paths()
        for index in range(len(backup_paths) - 1, -1, -1):
            source = self.file if index == 0 else backup_paths[index - 1]
            target = backup_paths[index]
            if not source.exists():
                continue
            if target.exists():
                target.unlink()
            shutil.copy2(source, target)

    def _preserve_broken_file(self) -> None:
        """把无法读取的主文件另存为 broken 备份，方便人工排查和恢复。"""
        if not self.file.exists():
            return
        broken_path = self.file.with_name(f"{self.file.name}.broken.{int(time.time())}.bak")
        try:
            shutil.copy2(self.file, broken_path)
            logger.error(f"pig_data.json 损坏文件已保留: {broken_path}")
        except Exception as error:
            logger.error(f"pig_data.json 损坏文件备份失败: {error}")

    def _load_backup(self) -> Optional[dict]:
        for backup_path in self._backup_paths():
            if not backup_path.exists():
                continue
            try:
                raw = json.loads(backup_path.read_text("utf-8"))
                logger.warning(f"尝试从 pig_data.json 备份恢复: {backup_path}")
                return self._migrate(raw, persist=False)
            except Exception as error:
                logger.warning(f"pig_data.json 备份不可用: {backup_path}: {error}")
        return None

    # ---- 今日/历史 抽猪记录 ----

    def get_today_pig(self, user_id: str, date_str: Optional[str] = None) -> Optional[str]:
        """返回今日已抽的 pig_id，未抽返回 None。"""
        target_date = date_str or rollpig_date_str()
        return self.data["history"].get(target_date, {}).get(user_id)

    def get_daily_rolls(self, date_str: Optional[str] = None) -> dict:
        target_date = date_str or rollpig_date_str()
        return dict(self.data.get("history", {}).get(target_date, {}))

    def _record_group_roll(self, date_str: str, group_id: str, user_id: str, pig_id: str):
        """在群维度登记今日已出现的猪形态，用于群内日报与随机烤群友。"""
        if not group_id:
            return
        group_rolls = self.data.setdefault("group_rolls", {})
        day_rolls = group_rolls.setdefault(date_str, {})
        group_roll_map = day_rolls.setdefault(group_id, {})
        group_roll_map[user_id] = pig_id

    # ================================ P1A抽猪成长状态 ================================ #
    # 本地模式没有数据库事务，因此所有写入都必须在调用方持有 self._lock 时完成。
    # 与 cloud 的 P1A 语义保持一致：只有当天 DailyRoll 首次创建成功时，才允许 copies / duplicate_streak 变化，重复发送命令只读取既有结果。

    def get_draw_state(self, user_id: str) -> DrawState:
        """返回用户图鉴成长状态；旧 collection 数据会按 copies=1 兜底聚合。"""
        user_id = str(user_id)
        collection = self.data.setdefault("collection", {})
        raw_collection = collection.get(user_id, [])
        collection_ids = [str(pig_id) for pig_id in raw_collection] if isinstance(raw_collection, list) else []

        raw_progress = self.data.setdefault("pig_progress", {}).get(user_id, {})
        progress: dict[str, PigProgress] = {}
        if isinstance(raw_progress, dict):
            for pig_id, item in raw_progress.items():
                if not isinstance(item, dict):
                    continue
                progress[str(pig_id)] = PigProgress(
                    copies=max(0, _safe_int(item.get("copies"), 0)),
                    first_obtained_at=item.get("first_obtained_at"),
                )

        for pig_id in collection_ids:
            progress.setdefault(pig_id, PigProgress(copies=1, first_obtained_at=None))

        raw_state = self.data.setdefault("draw_state", {}).get(user_id, {})
        duplicate_streak = _safe_int(raw_state.get("duplicate_streak"), 0) if isinstance(raw_state, dict) else 0
        return DrawState(
            pig_ids=sorted(progress),
            progress=dict(sorted(progress.items())),
            duplicate_streak=max(0, duplicate_streak),
        )

    def _apply_created_roll_progress_locked(self, user_id: str, pig_id: str) -> DailyRollResult:
        collection = self.data.setdefault("collection", {})
        user_collection = collection.setdefault(user_id, [])
        if not isinstance(user_collection, list):
            user_collection = []
            collection[user_id] = user_collection

        pig_progress = self.data.setdefault("pig_progress", {})
        user_progress = pig_progress.setdefault(user_id, {})
        if not isinstance(user_progress, dict):
            user_progress = {}
            pig_progress[user_id] = user_progress

        draw_state = self.data.setdefault("draw_state", {})
        state = draw_state.setdefault(user_id, {"duplicate_streak": 0})
        if not isinstance(state, dict):
            state = {"duplicate_streak": 0}
            draw_state[user_id] = state

        previous_duplicate_streak = max(0, _safe_int(state.get("duplicate_streak"), 0))
        previous_item = user_progress.get(pig_id)
        has_progress = isinstance(previous_item, dict)
        already_collected = pig_id in user_collection
        previous_copies = (
            max(1, _safe_int(previous_item.get("copies"), 1))
            if has_progress
            else (1 if already_collected else 0)
        )
        is_new_pig = previous_copies <= 0 and not already_collected

        if pig_id not in user_collection:
            user_collection.append(pig_id)

        if is_new_pig:
            copies = 1
            duplicate_streak = 0
            first_obtained_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        else:
            copies = previous_copies + 1
            duplicate_streak = previous_duplicate_streak + 1
            first_obtained_at = (
                previous_item.get("first_obtained_at")
                if has_progress and previous_item.get("first_obtained_at")
                else None
            )

        user_progress[pig_id] = {
            "copies": copies,
            "first_obtained_at": first_obtained_at,
        }
        state["duplicate_streak"] = duplicate_streak
        return DailyRollResult(
            pig_id=pig_id,
            created=True,
            is_new_pig=is_new_pig,
            previous_copies=previous_copies,
            copies=copies,
            previous_duplicate_streak=previous_duplicate_streak,
            duplicate_streak=duplicate_streak,
        )

    def _build_existing_roll_result_locked(self, user_id: str, pig_id: str) -> DailyRollResult:
        draw_state = self.get_draw_state(user_id)
        copies = draw_state.copies_of(pig_id)
        return DailyRollResult(
            pig_id=pig_id,
            created=False,
            previous_copies=copies,
            copies=copies,
            previous_duplicate_streak=draw_state.duplicate_streak,
            duplicate_streak=draw_state.duplicate_streak,
        )

    async def set_today_pig(self, user_id: str, pig_id: str, group_id: str = ""):
        """记录今日抽到的 pig_id，并同步将其写入图鉴（永久保留）。"""
        async with self._lock:
            today = rollpig_date_str()
            if today not in self.data["history"]:
                self.data["history"][today] = {}
            previous_pig_id = self.data["history"][today].get(user_id)
            self.data["history"][today][user_id] = pig_id
            self._record_group_roll(today, group_id, user_id, pig_id)
            if previous_pig_id != pig_id:
                self._apply_created_roll_progress_locked(user_id, pig_id)

            await self._atomic_save()

    async def get_or_create_today_pig(
        self,
        user_id: str,
        proposed_pig_id: str,
        date_str: Optional[str] = None,
        group_id: str = "",
    ) -> DailyRollResult:
        target_date = date_str or rollpig_date_str()
        async with self._lock:
            history = self.data.setdefault("history", {})
            day_history = history.setdefault(target_date, {})
            existing_pig_id = day_history.get(user_id)
            dirty = False

            if existing_pig_id:
                if group_id:
                    before = (
                        self.data.get("group_rolls", {})
                        .get(target_date, {})
                        .get(group_id, {})
                        .get(user_id)
                    )
                    self._record_group_roll(target_date, group_id, user_id, existing_pig_id)
                    dirty = before != existing_pig_id
                if dirty:
                    await self._atomic_save()
                return self._build_existing_roll_result_locked(user_id, existing_pig_id)

            day_history[user_id] = proposed_pig_id
            self._record_group_roll(target_date, group_id, user_id, proposed_pig_id)
            result = self._apply_created_roll_progress_locked(user_id, proposed_pig_id)

            # 预约只在 DailyRoll 首次创建的同一临界区内转为 ready；重复查看不会二次激活。
            for reservation in self.data.setdefault("roast_reservations", {}).values():
                if not isinstance(reservation, dict):
                    continue
                if (
                    reservation.get("date_str") == target_date
                    and reservation.get("target_id") == user_id
                    and reservation.get("status") == "pending"
                ):
                    reservation["status"] = "ready"
                    reservation["target_pig_id"] = proposed_pig_id
                    reservation["ready_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()

            await self._atomic_save()
            return result

    # ================================ 预约烤猪业务 ================================ #

    async def record_unrolled_roast_attempt(
        self, user_id: str, date_str: Optional[str] = None
    ) -> UnrolledRoastAttemptResult:
        """持久化未抽猪先烤次数；日期是天然分区，第二天自动从 1 重新开始。"""

        target_date = date_str or rollpig_date_str()
        async with self._lock:
            day_attempts = self.data.setdefault("unrolled_roast_attempts", {}).setdefault(target_date, {})
            count = max(0, _safe_int(day_attempts.get(user_id), 0)) + 1
            day_attempts[user_id] = count
            await self._atomic_save()
        return UnrolledRoastAttemptResult(target_date, user_id, count)

    async def prepare_roast_reservation(
        self,
        *,
        attacker_id: str,
        attacker_name: str,
        attacker_pig_id: str,
        target_id: str,
        target_name: str,
        group_id: str,
        delivery_bot_id: str,
        force_mode: Optional[str] = None,
        date_str: Optional[str] = None,
        cooldown_seconds: Optional[int] = None,
        max_charges: Optional[int] = None,
    ) -> RoastReservationPrepareResult:
        """原子完成目标检查、免费加入或扣资源创建预约。"""

        target_date = date_str or rollpig_date_str()
        attacker_id = str(attacker_id)
        target_id = str(target_id)
        async with self._lock:
            target_pig_id = self.data.setdefault("history", {}).get(target_date, {}).get(target_id)
            existing = self._find_pending_reservation_locked(target_date, str(group_id), target_id)
            if existing is not None:
                participants = existing.setdefault("participants", [])
                if any(str(item.get("user_id")) == attacker_id for item in participants if isinstance(item, dict)):
                    return RoastReservationPrepareResult("already_joined", self._reservation_from_raw(existing))
                if len(participants) >= ROAST_RESERVATION_MAX_PARTICIPANTS:
                    return RoastReservationPrepareResult("reservation_full", self._reservation_from_raw(existing))
                participants.append({
                    "user_id": attacker_id,
                    "display_name": str(attacker_name),
                    "pig_id": str(attacker_pig_id),
                })
                await self._atomic_save()
                return RoastReservationPrepareResult("reservation_joined", self._reservation_from_raw(existing))

            protected_users = (
                self.data.setdefault("protected", {})
                .get(target_date, {})
                .get(str(group_id), [])
            )
            is_protected = target_id in {str(user_id) for user_id in protected_users}
            protection_broken = is_protected and force_mode in {"normal", "super"}
            if is_protected and not protection_broken:
                return RoastReservationPrepareResult("protected")
            if target_pig_id:
                return RoastReservationPrepareResult(
                    "target_ready",
                    target_pig_id=str(target_pig_id),
                    protection_broken=protection_broken,
                )

            cooldown_result: Optional[CooldownConsumeResult] = None
            if force_mode == "normal":
                if not self._consume_force_usage_locked(attacker_id, target_date):
                    return RoastReservationPrepareResult("force_denied")
            elif force_mode != "super":
                cooldown_result = self._consume_roast_usage_locked(
                    attacker_id,
                    now=time.time(),
                    cooldown_seconds=cooldown_seconds,
                    max_charges=max_charges,
                )
                if not cooldown_result.allowed:
                    return RoastReservationPrepareResult("cooldown_denied", cooldown=cooldown_result)

            reservation_id = uuid.uuid4().hex
            now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
            raw = {
                "reservation_id": reservation_id,
                "date_str": target_date,
                "group_id": str(group_id),
                "target_id": target_id,
                "target_name": str(target_name),
                "owner_id": attacker_id,
                "owner_name": str(attacker_name),
                "owner_pig_id": str(attacker_pig_id),
                "participants": [{
                    "user_id": attacker_id,
                    "display_name": str(attacker_name),
                    "pig_id": str(attacker_pig_id),
                }],
                "delivery_bot_id": str(delivery_bot_id),
                "force_mode": force_mode,
                "status": "pending",
                "target_pig_id": "",
                "created_at": now_iso,
                "outcome_snapshot": None,
            }
            self.data.setdefault("roast_reservations", {})[reservation_id] = raw
            await self._atomic_save()
            return RoastReservationPrepareResult(
                "reservation_created",
                self._reservation_from_raw(raw),
                cooldown_result,
                protection_broken=protection_broken,
            )

    async def claim_roast_reservations(
        self,
        delivery_bot_id: str,
        date_str: Optional[str] = None,
        excluded_reservation_ids: Optional[set[str]] = None,
    ) -> RoastReservationClaimResult:
        """领取当前 Bot 当天可投递的预约；只回收仍可安全重试的发送前租约。"""

        target_date = date_str or rollpig_date_str()
        excluded_ids = set(excluded_reservation_ids or ())
        claimed: list[RoastReservation] = []
        async with self._lock:
            now = datetime.datetime.now(datetime.timezone.utc)
            reservations = self.data.setdefault("roast_reservations", {})
            for reservation_id, raw in reservations.items():
                if not isinstance(raw, dict):
                    continue
                claimed_at = raw.get("claimed_at")
                stale_presend = False
                # processing 负责生成结果，prepared 表示结果已固化但还在渲染消息；两者
                # 都没有调用外部发送接口，可以依赖租约恢复。sending 绝不能自动重领。
                if raw.get("status") in {"processing", "prepared"}:
                    if not isinstance(claimed_at, str):
                        stale_presend = True
                    else:
                        try:
                            claimed_time = datetime.datetime.fromisoformat(claimed_at)
                            if claimed_time.tzinfo is None:
                                claimed_time = claimed_time.replace(tzinfo=datetime.timezone.utc)
                            stale_presend = (
                                now - claimed_time
                            ).total_seconds() >= ROAST_RESERVATION_CLAIM_TIMEOUT_SECONDS
                        except ValueError:
                            stale_presend = True
                if (
                    _matches_reservation_claim_date(raw, target_date, now)
                    and raw.get("delivery_bot_id") == str(delivery_bot_id)
                    and reservation_id not in excluded_ids
                    and (raw.get("status") == "ready" or stale_presend)
                ):
                    # 固定快照仍需先渲染成最终消息，因此重领只能进入 prepared；真正
                    # 调用 OneBot 发送前由独立事务推进到 sending。
                    raw["status"] = (
                        "prepared" if isinstance(raw.get("outcome_snapshot"), dict) else "processing"
                    )
                    raw["claim_token"] = uuid.uuid4().hex
                    raw["claimed_at"] = now.isoformat()
                    claimed.append(self._reservation_from_raw(raw))
            if claimed:
                await self._atomic_save()
            has_owned = any(
                isinstance(raw, dict)
                and _matches_reservation_claim_date(raw, target_date, now)
                and raw.get("delivery_bot_id") == str(delivery_bot_id)
                and raw.get("status") in {"pending", "ready", "processing", "prepared"}
                for raw in reservations.values()
            )
        return RoastReservationClaimResult(tuple(claimed), has_owned=has_owned)

    def has_owned_roast_reservations(self, delivery_bot_id: str, date_str: Optional[str] = None) -> bool:
        target_date = date_str or rollpig_date_str()
        now = datetime.datetime.now(datetime.timezone.utc)
        return any(
            isinstance(raw, dict)
            and _matches_reservation_claim_date(raw, target_date, now)
            and raw.get("delivery_bot_id") == str(delivery_bot_id)
            and raw.get("status") in {"pending", "ready", "processing", "prepared"}
            for raw in self.data.setdefault("roast_reservations", {}).values()
        )

    async def save_roast_reservation_outcome(
        self,
        reservation_id: str,
        claim_token: str,
        outcome_snapshot: dict,
    ) -> Optional[RoastReservation]:
        """原子固化结果并进入可恢复的 prepared；此时尚未调用外部发送接口。"""

        async with self._lock:
            raw = self._find_reservation_locked(reservation_id)
            if (
                raw is None
                or raw.get("status") not in {"processing", "prepared", "sending", "completed"}
                or raw.get("claim_token") != claim_token
            ):
                return None
            changed = False
            if not isinstance(raw.get("outcome_snapshot"), dict):
                if raw.get("status") != "processing":
                    return None
                raw["outcome_snapshot"] = dict(outcome_snapshot)
                changed = True
            elif raw.get("outcome_snapshot") != outcome_snapshot:
                return None
            if raw.get("status") == "processing":
                raw["status"] = "prepared"
                changed = True
            if changed:
                await self._atomic_save()
            return self._reservation_from_raw(raw)

    async def mark_roast_reservation_sending(
        self,
        reservation_id: str,
        claim_token: str,
    ) -> Optional[RoastReservation]:
        """最终消息准备完成后进入不可自动重领的 sending。"""

        async with self._lock:
            raw = self._find_reservation_locked(reservation_id)
            if (
                raw is None
                or raw.get("claim_token") != claim_token
                or raw.get("status") not in {"prepared", "sending", "completed"}
                or not isinstance(raw.get("outcome_snapshot"), dict)
            ):
                return None
            if raw.get("status") == "prepared":
                raw["status"] = "sending"
                await self._atomic_save()
            return self._reservation_from_raw(raw)

    async def complete_roast_reservation(
        self,
        reservation_id: str,
        claim_token: str,
        event: Optional[RoastEvent] = None,
    ) -> bool:
        """原子完成预约，并把对应日报事件与状态写入同一次 JSON 保存。"""

        async with self._lock:
            raw = self._find_reservation_locked(reservation_id)
            if (
                raw is None
                or raw.get("claim_token") != claim_token
                or raw.get("status") not in {"sending", "completed"}
            ):
                return False
            changed = False
            if raw.get("status") != "completed":
                raw["status"] = "completed"
                raw["completed_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
                changed = True
            if event is not None:
                bound_event = self._bind_reservation_event_locked(raw, event)
                changed = self._append_roast_event_locked(
                    bound_event,
                    date_str=str(raw.get("date_str") or rollpig_date_str()),
                ) or changed
            if changed:
                await self._atomic_save()
            return True

    async def release_roast_reservation(self, reservation_id: str, claim_token: str) -> bool:
        """发送开始前失败时放回 ready；已确定 outcome 会原样保留。"""

        async with self._lock:
            raw = self._find_reservation_locked(reservation_id)
            if (
                raw is None
                or raw.get("status") not in {"processing", "prepared"}
                or raw.get("claim_token") != claim_token
            ):
                return False
            raw["status"] = "ready"
            # 保留释放时间，使零点前领取、零点后失败的发送前任务仍能在短暂窗口内重领。
            raw["released_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            raw.pop("claimed_at", None)
            raw.pop("claim_token", None)
            await self._atomic_save()
            return True

    async def mark_group_roll_seen(
        self,
        user_id: str,
        pig_id: str,
        group_id: str,
        date_str: Optional[str] = None,
    ):
        """将已有的今日形态登记到当前群，避免群内统计漏记。"""
        if not group_id:
            return
        async with self._lock:
            target_date = date_str or rollpig_date_str()
            self._record_group_roll(target_date, group_id, user_id, pig_id)
            await self._atomic_save()

    def get_pig_by_date(self, user_id: str, date_str: str) -> Optional[str]:
        """返回指定日期的 pig_id，无记录返回 None。"""
        return self.data["history"].get(date_str, {}).get(user_id)

    def get_user_collection(self, user_id: str) -> List[str]:
        # 返回副本，避免命令层无意修改内部 list 后绕过锁与原子保存。
        return list(self.data.get("collection", {}).get(user_id, []))

    async def clean_old_history(self, days_to_keep: int = 14):
        """清理超过 days_to_keep 天的历史记录（不影响图鉴数据）。"""
        async with self._lock:
            today = rollpig_today()
            now = datetime.datetime.now(datetime.timezone.utc)
            history_dates_to_del = [
                d for d in self.data["history"]
                if _is_valid_date(d)  # 必须先过滤非法日期键，再做计算（防止 ValueError）
                and (today - datetime.date.fromisoformat(d)).days > days_to_keep
            ]
            for d in history_dates_to_del:
                del self.data["history"][d]

            group_rolls = self.data.get("group_rolls", {})
            group_dates_to_del = [
                d for d in group_rolls
                if _is_valid_date(d)
                and (today - datetime.date.fromisoformat(d)).days > days_to_keep
            ]
            for d in group_dates_to_del:
                del group_rolls[d]

            # completed 已由事件记录承接，其他预约跨日后也不再有效；每日清理直接
            # 移除这些终态/过期记录，避免结果快照让 JSON 与滚动备份无限增长。
            reservations = self.data.get("roast_reservations", {})
            reservation_ids_to_del = [
                reservation_id
                for reservation_id, raw in reservations.items()
                if isinstance(raw, dict)
                and (
                    raw.get("status") == "completed"
                    or (
                        _is_valid_date(str(raw.get("date_str") or ""))
                        and datetime.date.fromisoformat(str(raw["date_str"])) < today
                        and not _matches_reservation_claim_date(raw, today.isoformat(), now)
                    )
                )
            ]
            for reservation_id in reservation_ids_to_del:
                del reservations[reservation_id]

            # 未抽猪违规次数只在当日判定中使用，跨日后直接清理，避免日期分区无限增长。
            unrolled_attempts = self.data.get("unrolled_roast_attempts", {})
            attempt_dates_to_del = [
                date_str
                for date_str in unrolled_attempts
                if _is_valid_date(date_str) and datetime.date.fromisoformat(date_str) < today
            ]
            for date_str in attempt_dates_to_del:
                del unrolled_attempts[date_str]

            if history_dates_to_del or group_dates_to_del or reservation_ids_to_del or attempt_dates_to_del:
                await self._atomic_save()

    # ---- 烤群友 普通模式充能 ----

    def check_roast_usage(self, user_id: str) -> tuple[bool, str]:
        """
        Deprecated: 仅保留给旧调用兜底；新流程必须使用 consume_roast_usage()。
        检查与扣减分离会重新引入 TOCTOU 竞态，因此不要在新代码中调用本函数。
        返回: (是否可用, 若不可用时的提示信息)
        """
        usage = self.data.setdefault("usage", {})
        raw_state = usage.get(user_id, 0)
        now = float(time.time())
        cooldown, max_charges = _normalize_charge_settings(ROAST_COOLDOWN_SECONDS, DEFAULT_ROAST_CHARGE_MAX)

        if isinstance(raw_state, dict):
            charges = _safe_int(raw_state.get("roast_charges"), 0)
            updated_ts = float(raw_state.get("roast_charge_updated_ts") or now)
        else:
            charges, updated_ts = _legacy_roast_state(float(raw_state or 0), now, cooldown, max_charges)

        charges, updated_ts = _recover_roast_charges(charges, updated_ts, now, cooldown, max_charges)
        if charges <= 0:
            remaining = _next_charge_seconds(charges, updated_ts, now, cooldown, max_charges)
            m, s = divmod(remaining, 60)
            h, m = divmod(m, 60)
            time_str = f"{h}小时{m}分" if h > 0 else f"{m}分{s}秒"
            return False, f"烧烤充能恢复中！还需要 {time_str} 恢复 1 次。"

        return True, ""

    async def consume_roast_usage(
        self,
        user_id: str,
        now_ts: Optional[float] = None,
        cooldown_seconds: Optional[int] = None,
        max_charges: Optional[int] = None,
    ) -> CooldownConsumeResult:
        now = float(now_ts or time.time())
        cooldown, charge_max = _normalize_charge_settings(cooldown_seconds, max_charges)
        async with self._lock:
            usage = self.data.setdefault("usage", {})
            raw_state = usage.get(user_id, 0)
            if isinstance(raw_state, dict):
                charges = _safe_int(raw_state.get("roast_charges"), 0)
                updated_ts = float(raw_state.get("roast_charge_updated_ts") or now)
            else:
                charges, updated_ts = _legacy_roast_state(float(raw_state or 0), now, cooldown, charge_max)

            charges, updated_ts = _recover_roast_charges(charges, updated_ts, now, cooldown, charge_max)
            if charges <= 0:
                remaining = _next_charge_seconds(charges, updated_ts, now, cooldown, charge_max)
                usage[user_id] = {
                    "last_roast_ts": float(raw_state or 0) if not isinstance(raw_state, dict) else raw_state.get("last_roast_ts"),
                    "roast_charges": charges,
                    "roast_charge_updated_ts": updated_ts,
                }
                await self._atomic_save()
                return CooldownConsumeResult(
                    allowed=False,
                    remaining_seconds=remaining,
                    charges_left=0,
                    max_charges=charge_max,
                    next_recover_seconds=remaining,
                )

            was_full = charges >= charge_max
            charges -= 1
            if was_full:
                updated_ts = now
            usage[user_id] = {
                "last_roast_ts": now,
                "roast_charges": charges,
                "roast_charge_updated_ts": updated_ts,
            }
            await self._atomic_save()
            return CooldownConsumeResult(
                allowed=True,
                remaining_seconds=0,
                charges_left=charges,
                max_charges=charge_max,
                next_recover_seconds=_next_charge_seconds(charges, updated_ts, now, cooldown, charge_max),
            )

    async def update_roast_usage(self, user_id: str):
        """Deprecated: 仅保留给旧调用兜底；新流程必须使用 consume_roast_usage() 原子扣减。"""
        async with self._lock:
            usage = self.data.setdefault("usage", {})
            now = time.time()
            usage[user_id] = {
                "last_roast_ts": now,
                "roast_charges": max(0, DEFAULT_ROAST_CHARGE_MAX - 1),
                "roast_charge_updated_ts": now,
            }
            await self._atomic_save()

    # ---- 烤群友 后门口令 每日计数 ----

    def check_force_roast_usage(self, user_id: str) -> bool:
        """普通用户后门：每日仅 1 次，返回今日是否仍可用。"""
        today = rollpig_date_str()
        if "force_usage" not in self.data or not isinstance(self.data["force_usage"], dict):
            self.data["force_usage"] = {}
        return self.data["force_usage"].get(user_id) != today

    async def consume_force_roast_usage(self, user_id: str, date_str: Optional[str] = None) -> bool:
        target_date = date_str or rollpig_date_str()
        async with self._lock:
            usage = self.data.setdefault("force_usage", {})
            if usage.get(user_id) == target_date:
                return False
            usage[user_id] = target_date
            await self._atomic_save()
            return True

    async def update_force_roast_usage(self, user_id: str):
        async with self._lock:
            today = rollpig_date_str()
            self.data.setdefault("force_usage", {})[user_id] = today
            await self._atomic_save()

    # ================================ 烤群友事件记录 ================================ #
    # 预约完成和日报事件必须共用同一次原子保存；普通即时事件仍复用同一序列化入口。

    def _append_roast_event_locked(self, event: RoastEvent, *, date_str: str) -> bool:
        """在持有数据锁时追加事件；预约事件跨日期按 reservation_id 幂等。"""

        events = self.data.setdefault("daily_events", {})
        if event.reservation_id:
            for existing_events in events.values():
                if any(
                    isinstance(item, dict) and item.get("reservation_id") == event.reservation_id
                    for item in existing_events
                ):
                    return False
        events.setdefault(date_str, []).append({
            "type": event.event_type,
            "attacker": event.attacker_id,
            "target": event.target_id,
            "attacker_name": event.attacker_name,
            "target_name": event.target_name,
            "food": event.food,
            "group_id": event.group_id,
            "reservation_id": event.reservation_id,
            "participant_ids": list(event.participant_ids),
            "participant_names": list(event.participant_names),
            "participant_count": max(0, int(event.participant_count or 0)),
            "backfire_victim_id": str(event.backfire_victim_id or ""),
            "backfire_victim_name": str(event.backfire_victim_name or ""),
        })
        return True

    async def log_roast_event(self, event_type: str, attacker_id: str, target_id: str,
                               attacker_name: str = "", target_name: str = "",
                               food: str = "", group_id: str = "",
                               reservation_id: str = "", participant_ids: Optional[list[str]] = None,
                               participant_names: Optional[list[str]] = None, participant_count: int = 0,
                               backfire_victim_id: str = "", backfire_victim_name: str = ""):
        """
        记录一次烤群友事件。
        event_type: "success" / "escape" / "backfire" / "bot_backfire" / "self_roast"
        """
        async with self._lock:
            changed = self._append_roast_event_locked(
                RoastEvent(
                    event_type=event_type,
                    attacker_id=attacker_id,
                    target_id=target_id,
                    attacker_name=attacker_name,
                    target_name=target_name,
                    food=food,
                    group_id=group_id,
                    reservation_id=reservation_id,
                    participant_ids=tuple(participant_ids or ()),
                    participant_names=tuple(participant_names or ()),
                    participant_count=participant_count,
                    backfire_victim_id=backfire_victim_id,
                    backfire_victim_name=backfire_victim_name,
                ),
                date_str=rollpig_date_str(),
            )
            if changed:
                await self._atomic_save()

    def get_daily_events(self, date_str: Optional[str] = None, group_id: Optional[str] = None) -> list:
        """获取指定日期（默认今天）的所有烤群友事件。"""
        if not date_str:
            date_str = rollpig_date_str()
        events = self.data.get("daily_events", {}).get(date_str, [])
        if not group_id:
            return [dict(e) if isinstance(e, dict) else e for e in events]
        return [dict(e) if isinstance(e, dict) else e for e in events if e.get("group_id") == group_id]

    def get_recent_rolls(self, user_id: str, days: int = 14) -> dict[str, str]:
        """返回最近若干天的抽猪记录；图鉴只读使用，不会修改 copies。"""
        today = rollpig_today()
        safe_days = max(1, min(60, int(days or 14)))
        start_date = today - datetime.timedelta(days=safe_days - 1)
        result: dict[str, str] = {}
        for date_str, rows in self.data.get("history", {}).items():
            if not _is_valid_date(date_str) or not isinstance(rows, dict):
                continue
            date_obj = datetime.date.fromisoformat(date_str)
            if start_date <= date_obj <= today:
                pig_id = rows.get(str(user_id))
                if pig_id:
                    result[date_str] = str(pig_id)
        return dict(sorted(result.items(), reverse=True))

    def count_success_roasted(self, user_id: str, days: int = 7) -> int:
        """统计用户近 N 天成功被烤次数；逃脱/反噬不算“被烤成功”。"""
        today = rollpig_today()
        safe_days = max(1, min(60, int(days or 7)))
        start_date = today - datetime.timedelta(days=safe_days - 1)
        total = 0
        for date_str, events in self.data.get("daily_events", {}).items():
            if not _is_valid_date(date_str) or not isinstance(events, list):
                continue
            date_obj = datetime.date.fromisoformat(date_str)
            if not (start_date <= date_obj <= today):
                continue
            total += sum(
                1
                for event in events
                if isinstance(event, dict)
                and event.get("type") == "success"
                and str(event.get("target") or "") == str(user_id)
            )
        return total

    def get_catalog_snapshot(self, user_id: str, days: int = 14) -> CatalogSnapshot:
        """聚合图片版图鉴需要的本地只读数据，避免命令层多处手算。"""
        return CatalogSnapshot(
            draw_state=self.get_draw_state(user_id),
            recent_rolls=self.get_recent_rolls(user_id, days=days),
            roasted_7d=self.count_success_roasted(user_id, days=7),
        )

    def get_group_rolls(self, group_id: str, date_str: Optional[str] = None) -> dict:
        """获取指定群在某天登记过的今日形态。"""
        if not date_str:
            date_str = rollpig_date_str()
        return dict(self.data.get("group_rolls", {}).get(date_str, {}).get(group_id, {}))

    def get_active_group_ids(self, date_str: Optional[str] = None) -> set[str]:
        """获取指定日期内有抽猪或烧烤活动的群号集合。"""
        if not date_str:
            date_str = rollpig_date_str()

        event_groups = {
            str(e.get("group_id"))
            for e in self.get_daily_events(date_str)
            if e.get("group_id")
        }
        roll_groups = {
            str(group_id)
            for group_id in self.data.get("group_rolls", {}).get(date_str, {}).keys()
            if group_id
        }
        return event_groups | roll_groups

    def get_daily_summary(self, date_str: Optional[str] = None, group_id: Optional[str] = None) -> dict:
        """
        汇总指定日期的烤群友数据，返回:
        {
            "total": int,
            "most_roasted_id": str | None,      # 被烤最多的 UID
            "most_roasted_name": str,
            "most_roasted_count": int,
            "most_active_id": str | None,        # 烤人最多的 UID
            "most_active_name": str,
            "most_active_count": int,
            "escape_king_id": str | None,        # 逃脱最多的 UID
            "escape_king_name": str,
            "escape_king_count": int,
            "backfire_king_id": str | None,      # 反噬最多的 UID
            "backfire_king_name": str,
            "backfire_king_count": int,
        }
        """
        roll_stats = self._get_roll_stats(date_str, group_id=group_id)
        events = self.get_daily_events(date_str, group_id=group_id)
        if not events and roll_stats.get("roll_count", 0) == 0:
            return {"total": 0, **roll_stats}

        from collections import Counter
        roasted_counter: Counter = Counter()       # 被烤次数
        attacker_counter: Counter = Counter()      # 发起烤次数
        escape_counter: Counter = Counter()        # 逃脱次数
        backfire_counter: Counter = Counter()      # 反噬次数
        name_map: dict = {}

        for e in events:
            a_id = e.get("attacker", "")
            t_id = e.get("target", "")
            if e.get("attacker_name"):
                name_map[a_id] = e["attacker_name"]
            if e.get("target_name"):
                name_map[t_id] = e["target_name"]

            etype = e.get("type", "")
            if etype == "success":
                attacker_counter[a_id] += 1
                if a_id and t_id and a_id != t_id:
                    roasted_counter[t_id] += 1
            elif etype == "self_roast":
                attacker_counter[a_id] += 1
            elif etype == "escape":
                escape_counter[t_id] += 1
                attacker_counter[a_id] += 1
            elif etype in ("backfire", "bot_backfire"):
                backfire_counter[a_id] += 1
                attacker_counter[a_id] += 1

        def _top(counter: Counter):
            if not counter:
                return None, "", 0
            uid, count = counter.most_common(1)[0]
            return uid, name_map.get(uid, uid), count

        mr_id, mr_name, mr_count = _top(roasted_counter)
        ma_id, ma_name, ma_count = _top(attacker_counter)
        ek_id, ek_name, ek_count = _top(escape_counter)
        bk_id, bk_name, bk_count = _top(backfire_counter)

        return {
            "total": len(events),
            "most_roasted_id": mr_id, "most_roasted_name": mr_name, "most_roasted_count": mr_count,
            "most_active_id": ma_id, "most_active_name": ma_name, "most_active_count": ma_count,
            "escape_king_id": ek_id, "escape_king_name": ek_name, "escape_king_count": ek_count,
            "backfire_king_id": bk_id, "backfire_king_name": bk_name, "backfire_king_count": bk_count,
            **roll_stats,
        }

    def _get_roll_stats(self, date_str: Optional[str] = None, group_id: Optional[str] = None) -> dict:
        """从 history 中统计今日抽猪信息。"""
        from collections import Counter
        if not date_str:
            date_str = rollpig_date_str()
        if group_id:
            today_rolls = self.get_group_rolls(group_id, date_str)
        else:
            today_rolls = self.data.get("history", {}).get(date_str, {})
        if not today_rolls:
            return {"roll_count": 0}

        pig_counter: Counter = Counter(today_rolls.values())
        top_pig_id, top_pig_count = pig_counter.most_common(1)[0]

        # 统计人类形态的用户
        human_ids = [uid for uid, pid in today_rolls.items() if pid == "human"]

        return {
            "roll_count": len(today_rolls),
            "top_pig_id": top_pig_id,
            "top_pig_count": top_pig_count,
            "human_count": len(human_ids),
        }

    # ---- 被烤最多 → 次日保护 ----

    def is_protected(self, group_id: str, user_id: str, date_str: Optional[str] = None) -> bool:
        """检查用户在当前群今日是否受保护。"""
        target_date = date_str or rollpig_date_str()
        protected_map = self.data.get("protected", {}).get(target_date, {})
        if not isinstance(protected_map, dict):
            return False
        group_users = protected_map.get(group_id, [])
        legacy_users = protected_map.get("__all__", [])
        return user_id in group_users or user_id in legacy_users

    async def replace_group_protected_users(
        self,
        group_id: str,
        user_ids: list[str],
        protect_date: Optional[str] = None,
    ):
        """按群设置某日受保护的用户列表。"""
        target_date = protect_date or rollpig_date_str(1)
        async with self._lock:
            protected = self.data.setdefault("protected", {})
            day_map = protected.setdefault(target_date, {})
            day_map[group_id] = sorted({str(user_id) for user_id in user_ids if user_id})
            await self._atomic_save()

    async def set_protected_users(self, user_ids: list):
        """兼容旧接口：写入 legacy 全局保护名单。"""
        target_date = rollpig_date_str(1)
        async with self._lock:
            protected = self.data.setdefault("protected", {})
            day_map = protected.setdefault(target_date, {})
            day_map["__all__"] = sorted({str(user_id) for user_id in user_ids if user_id})
            await self._atomic_save()

    async def clean_old_events(self, days_to_keep: int = 7):
        """清理超过 days_to_keep 天的事件记录。"""
        async with self._lock:
            today = rollpig_today()
            events = self.data.get("daily_events", {})
            dates_to_del = [
                d for d in events
                if _is_valid_date(d)
                and (today - datetime.date.fromisoformat(d)).days > days_to_keep
            ]
            for d in dates_to_del:
                del events[d]

            protected = self.data.get("protected", {})
            protection_dates_to_del = [
                d for d in list(protected.keys())
                if _is_valid_date(d)
                and (today - datetime.date.fromisoformat(d)).days > 1
            ]
            for d in protection_dates_to_del:
                del protected[d]

            if dates_to_del or protection_dates_to_del:
                await self._atomic_save()


def _safe_int(value, default: int = 0) -> int:
    """将历史 JSON 中可能出现的字符串/空值转成整数，失败时使用安全默认值。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _is_valid_date(date_str: str) -> bool:
    try:
        datetime.date.fromisoformat(date_str)
        return True
    except ValueError:
        return False


_data_manager: PigDataManager | None = None


def get_data_manager() -> PigDataManager:
    global _data_manager
    if _data_manager is None:
        _data_manager = PigDataManager()
    return _data_manager
