"""引文卡：从一段对话里挑出**一句原话**，不改写、不总结。

为什么是引文而不是摘要：摘要会引入错误，而且越总结越像"别人对她的描述"。
一句原话可以永远回指到事件账，是唯一能被验证的记忆单位。

关键规则
1. 一段对话产 1–2 张卡：他说的一句 + 她说的一句（两侧都要，否则记忆只有一半）。
2. 空引文不要；纯标记（[IMAGE] / [MSG_ID:…]）算空。
3. 人工改过/钉住的卡，重建时**不覆盖、不删除**（origin != auto）。
4. 被人工废除的引文进黑名单，重建也不会复活。
"""

from __future__ import annotations

import re
import sqlite3

NOISE = re.compile(r"\[(?:msg_id|image|reply)[^\]]*\]", re.I)
GENERIC = {"嗯", "嗯嗯", "好", "好的", "行", "是", "对", "哦", "哈哈", "…", "收到"}
CARD_GAP_MIN = 45      # 超过这么久算另一段对话
QUOTE_MAX = 120


def clean(text: str) -> str:
    return NOISE.sub("", text or "").strip()


def _blank(text: str) -> bool:
    t = clean(text)
    return not t or len(t) <= 1 or t in GENERIC


def _score(text: str) -> float:
    """挑哪一句当引文：信息量优先，但别太长。"""
    t = clean(text)
    score = len(t)
    if "?" in t or "？" in t:
        score += 6
    if any(w in t for w in ("明天", "说好", "记得", "答应", "以后")):
        score += 8      # 未完成的事，更值得记
    return score


def split(events) -> list[list]:
    groups, cur, last = [], [], None
    for ev in events:
        ts = int(ev["ts_ms"])
        if cur and (ts - last > CARD_GAP_MIN * 60_000):
            groups.append(cur)
            cur = []
        cur.append(ev)
        last = ts
    if cur:
        groups.append(cur)
    return [g for g in groups if any(not _blank(e["text"]) for e in g)]


def _day(ts_ms: int) -> str:
    import datetime as dt
    return dt.date.fromtimestamp(ts_ms / 1000).isoformat()


def build(group) -> list[dict]:
    """一段对话 → 0–2 张卡。"""
    out = []
    for side in ("human", "self"):
        pool = [e for e in group
                if e["sender_kind"] == side and not _blank(e["text"])]
        if not pool:
            continue
        best = max(pool, key=lambda e: _score(e["text"]))
        quote = clean(best["text"])[:QUOTE_MAX]
        out.append({
            "day": _day(int(group[0]["ts_ms"])),
            "ts0_ms": int(group[0]["ts_ms"]),
            "quote": quote,
            "speaker": side,
            "unresolved": 1 if any(w in quote for w in ("明天", "说好", "记得", "答应")) else 0,
        })
    return out


def rebuild(con: sqlite3.Connection, groups: list[list]) -> dict:
    """重建派生卡。人工卡受保护，消失的卡标作废而不是删除。"""
    con.execute("""CREATE TABLE IF NOT EXISTS cards(
        id INTEGER PRIMARY KEY, day TEXT, ts0_ms INTEGER, quote TEXT,
        speaker TEXT, unresolved INTEGER DEFAULT 0, importance REAL DEFAULT 0.5,
        origin TEXT DEFAULT 'auto', status TEXT DEFAULT 'active', created_at TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS card_blocks(
        quote_key TEXT PRIMARY KEY, reason TEXT)""")

    blocks = {r["quote_key"] for r in con.execute("SELECT quote_key FROM card_blocks")}
    existing = {(r["ts0_ms"], r["quote"]): dict(r) for r in
                con.execute("SELECT * FROM cards WHERE status='active'")}
    protected = {k: v for k, v in existing.items() if v["origin"] != "auto"}
    keep, stats = set(), {"created": 0, "skipped": 0, "superseded": 0}

    for group in groups:
        for c in build(group):
            key = (c["ts0_ms"], c["quote"])
            if re.sub(r"\s+", "", c["quote"])[:60] in blocks:
                continue
            if key in protected:
                stats["skipped"] += 1
                keep.add(protected[key]["id"])
                continue
            if key in existing:
                keep.add(existing[key]["id"])
                continue
            cur = con.execute(
                """INSERT INTO cards(day, ts0_ms, quote, speaker, unresolved,
                                     importance, origin, status, created_at)
                   VALUES(?,?,?,?,?,?,'auto','active',?)""",
                (c["day"], c["ts0_ms"], c["quote"], c["speaker"],
                 c["unresolved"], 0.5, c["day"]))
            keep.add(cur.lastrowid)
            stats["created"] += 1

    for key, row in existing.items():
        if row["id"] in keep or row["origin"] != "auto":
            continue
        con.execute("UPDATE cards SET status='superseded' WHERE id=?", (row["id"],))
        stats["superseded"] += 1
    con.commit()
    return stats


def block_quote(con: sqlite3.Connection, quote: str, reason: str = "manual") -> None:
    """永久废除一条引文：进黑名单 + 现有卡作废。"""
    key = re.sub(r"\s+", "", quote)[:60]
    con.execute("INSERT OR REPLACE INTO card_blocks(quote_key, reason) VALUES(?,?)",
                (key, reason))
    con.execute("UPDATE cards SET status='superseded' WHERE quote=?", (quote,))
    con.commit()
