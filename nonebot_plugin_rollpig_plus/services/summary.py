from __future__ import annotations

from collections import Counter
from typing import Optional

from ..runtime import rollpig_date_str
from ..store.base import RollpigStore


async def build_daily_summary(
    store: RollpigStore,
    date_str: Optional[str] = None,
    group_id: Optional[str] = None,
) -> dict:
    target_date = date_str or rollpig_date_str()
    today_rolls = (
        await store.get_group_rolls(group_id, target_date)
        if group_id
        else await store.get_daily_rolls(target_date)
    )
    events = await store.list_daily_events(date_str=target_date, group_id=group_id)

    roll_stats = _get_roll_stats(today_rolls)
    if not events and roll_stats.get("roll_count", 0) == 0:
        return {"total": 0, **roll_stats}

    roasted_counter: Counter = Counter()
    attacker_counter: Counter = Counter()
    escape_counter: Counter = Counter()
    backfire_counter: Counter = Counter()
    name_map: dict[str, str] = {}

    for event in events:
        attacker_id = event.get("attacker", "")
        target_id = event.get("target", "")
        if event.get("attacker_name"):
            name_map[attacker_id] = event["attacker_name"]
        if event.get("target_name"):
            name_map[target_id] = event["target_name"]

        event_type = event.get("type", "")
        if event_type == "success":
            attacker_counter[attacker_id] += 1
            if attacker_id and target_id and attacker_id != target_id:
                roasted_counter[target_id] += 1
        elif event_type == "self_roast":
            attacker_counter[attacker_id] += 1
        elif event_type == "escape":
            escape_counter[target_id] += 1
            attacker_counter[attacker_id] += 1
        elif event_type in ("backfire", "bot_backfire"):
            backfire_counter[attacker_id] += 1
            attacker_counter[attacker_id] += 1

    def _top(counter: Counter):
        if not counter:
            return None, "", 0
        uid, count = counter.most_common(1)[0]
        return uid, name_map.get(uid, uid), count

    most_roasted_id, most_roasted_name, most_roasted_count = _top(roasted_counter)
    most_active_id, most_active_name, most_active_count = _top(attacker_counter)
    escape_king_id, escape_king_name, escape_king_count = _top(escape_counter)
    backfire_king_id, backfire_king_name, backfire_king_count = _top(backfire_counter)

    return {
        "total": len(events),
        "most_roasted_id": most_roasted_id,
        "most_roasted_name": most_roasted_name,
        "most_roasted_count": most_roasted_count,
        "most_active_id": most_active_id,
        "most_active_name": most_active_name,
        "most_active_count": most_active_count,
        "escape_king_id": escape_king_id,
        "escape_king_name": escape_king_name,
        "escape_king_count": escape_king_count,
        "backfire_king_id": backfire_king_id,
        "backfire_king_name": backfire_king_name,
        "backfire_king_count": backfire_king_count,
        **roll_stats,
    }


def _get_roll_stats(today_rolls: dict[str, str]) -> dict:
    if not today_rolls:
        return {"roll_count": 0}

    pig_counter: Counter = Counter(today_rolls.values())
    top_pig_id, top_pig_count = pig_counter.most_common(1)[0]
    human_ids = [uid for uid, pig_id in today_rolls.items() if pig_id == "human"]
    return {
        "roll_count": len(today_rolls),
        "top_pig_id": top_pig_id,
        "top_pig_count": top_pig_count,
        "human_count": len(human_ids),
    }
