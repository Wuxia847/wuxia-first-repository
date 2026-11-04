"""状态：从事件派生的四维向量（纯函数，可重放）。

思想
- **状态 = f(事件, as_of)**，不存"当前值"，随时可重算；想回看就换个 as_of。
- 两个时间尺度：**慢变量**按"共同天数"增长（关系是熬出来的），
  **快变量**按 72 小时衰减（热度是聊出来的）。
- 业务上的"一天"可以不是零点。用 `day_index()` 定义边界（比如凌晨 4 点），
  避免熬夜聊天被切成两天。
"""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
DAY_CUTOFF_HOUR = 4          # 凌晨 4 点算前一天

SLOW = {"intimacy": 20.0, "trust": 45.0}   # 天数尺度
FAST = {"tension": 6.0, "warmth": 12.0}    # 小时尺度
HALF_LIFE_H = 72.0


def day_index(ts_ms: int) -> int:
    dt = datetime.fromtimestamp(ts_ms / 1000, CST) - timedelta(hours=DAY_CUTOFF_HOUR)
    return dt.date().toordinal()


def _days_between(a_ms: int, b_ms: int) -> float:
    return abs(day_index(a_ms) - day_index(b_ms))


def _decay(hours: float, half_life: float) -> float:
    return 0.5 ** (hours / half_life)


def compute(events, as_of_ms: int) -> dict:
    """events 需按 ts_ms 升序，且已过滤成 self/human。"""
    if not events:
        return {"intimacy": 0.5, "trust": 0.5, "tension": 0.0, "warmth": 0.0}

    first_ms = int(events[0]["ts_ms"])
    last_ms = int(events[-1]["ts_ms"])
    span_days = max(1.0, _days_between(first_ms, last_ms))
    silent_h = max(0.0, (as_of_ms - last_ms) / 3_600_000)

    msgs = [e for e in events if e["sender_kind"] == "human"]
    n = len(msgs)

    # 慢变量：共同天数越多，越接近上限；长期不联系也几乎不掉
    intimacy = 0.45 + 0.4 * (1 - math.exp(-span_days / SLOW["intimacy"]))
    trust = 0.40 + 0.45 * (1 - math.exp(-span_days / SLOW["trust"]))

    # 快变量：最近 72h 的消息密度，沉默后指数衰减
    recent = [e for e in msgs if as_of_ms - int(e["ts_ms"]) <= 72 * 3_600_000]
    density = min(1.0, len(recent) / 40.0)
    warmth = density * _decay(silent_h, HALF_LIFE_H)

    # 张力：看"没接住"的信号（这里用问句未答 + 短促回复代理，可替换）
    tension = 0.0
    for a, b in zip(events, events[1:]):
        if a["sender_kind"] == "human" and len((a["text"] or "")) > 12:
            gap = (int(b["ts_ms"]) - int(a["ts_ms"])) / 60_000
            if gap > 240:
                tension += 0.05
    tension = min(0.6, tension) * _decay(silent_h, HALF_LIFE_H)

    return {
        "intimacy": round(min(0.95, intimacy), 3),
        "trust": round(min(0.95, trust), 3),
        "tension": round(tension, 3),
        "warmth": round(warmth, 3),
        "inputs": {"span_days": round(span_days, 1), "messages": n,
                   "hours_silent": round(silent_h, 1)},
    }


def to_prose(state: dict) -> str:
    """给模型看的是**词**，不是数字。数字是坐标系，词才是感觉。"""
    def pick(v, bands):
        for lo, word in bands:
            if v >= lo:
                return word
        return bands[-1][1]

    relation = pick(state["intimacy"], [(0.75, "你们很近"), (0.6, "你们挺亲近"),
                                        (0.5, "你们还行"), (0, "你们有点远")])
    trust = pick(state["trust"], [(0.7, "你信他"), (0.55, "还算信他"), (0, "你心里没底")])
    tension = pick(state["tension"], [(0.4, "心里有根刺"), (0.2, "有点别扭"), (0, "没什么疙瘩")])
    mood = pick(state["warmth"], [(0.8, "气氛正热"), (0.5, "不冷不热"), (0, "有点凉了")])
    return f"{relation}，{trust}，{tension}；{mood}。"
