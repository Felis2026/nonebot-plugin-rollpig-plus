from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any


def count_roasted_users(events: Iterable[Mapping[str, Any]]) -> Counter[str]:
    """统计被烤次数，特殊惩罚按同群、同日、同人、同种类各计一次。"""

    counts: Counter[str] = Counter()
    special_seen: set[tuple[str, str, str, str]] = set()
    event_seen: set[tuple[str, str]] = set()
    for event in events:
        date = str(event.get("date_str") or "")
        event_id = str(event.get("event_id") or "")
        if event_id:
            key = (date, event_id)
            if key in event_seen:
                continue
            event_seen.add(key)
        kind = str(event.get("type") or event.get("event_type") or "")
        attacker = str(event.get("attacker") or event.get("attacker_id") or "")
        target = str(event.get("target") or event.get("target_id") or "")
        special_kind = ""
        if kind == "success" and target and target != attacker:
            victims = [target]
        elif kind in {"backfire", "bot_backfire"}:
            victims = event.get("backfire_victim_ids") or [event.get("backfire_victim_id") or attacker]
            if kind == "bot_backfire":
                special_kind = "bot"
            elif event.get("special_reason") == "unrolled":
                special_kind = "unrolled"
        else:
            continue
        for victim in dict.fromkeys(str(item) for item in victims if item):
            if special_kind:
                special_key = (date, str(event.get("group_id") or ""), victim, special_kind)
                if special_key in special_seen:
                    continue
                special_seen.add(special_key)
            counts[victim] += 1
    return counts
