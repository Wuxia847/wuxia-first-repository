"""话题：把"同一件事的几张卡"装进一个容器，并给它一个生命周期。

定位（这是最容易做错的地方）
- 话题**不是**"一组卡片的标签"，那只是索引。
- 话题是**一件还没结束的事**：它有 open / dormant / closed / merged 四种状态。
- 它**不参与检索打分**，只在命中卡片之后"展开"上下文——
  否则同一件事的十张卡会互相抬分，把别的话题挤掉。

状态写成两段：`他……；我……`
推动性的动词（催/劝/提醒/让）永远是"我"的动作；他那栏只写处境。
不这么定死，模型会把"他陈述事实"写成"他提醒我"，视角就歪了。
"""

from __future__ import annotations

import json
import re

DORMANT_DAYS = 14
MIN_CARDS = 2
GAP_HOURS = 2.0
MIN_OVERLAP = 0.25

NAME_PROMPT = """下面是一段对话，已按说话人分好。你是"我"，对面是"他"。

【他说的】{his}
【我说的】{hers}

输出 JSON：
{{"title":"≤10字","he":"他那边（≤15字，以他开头，只写处境）","me":"我这边（≤15字，以我开头，写我做了什么）"}}"""


def init(con) -> None:
    con.execute("""CREATE TABLE IF NOT EXISTS topics(
        id INTEGER PRIMARY KEY, title TEXT, state TEXT,
        status TEXT DEFAULT 'open', started_ms INTEGER, last_ms INTEGER,
        closed_at TEXT, merged_into INTEGER, created_at TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS topic_cards(
        topic_id INTEGER, card_id INTEGER, added_at TEXT,
        PRIMARY KEY(topic_id, card_id))""")
    con.commit()


def _bigrams(t: str) -> set[str]:
    s = re.sub(r"[^\w\u4e00-\u9fff]", "", t or "")
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _related(a: dict, b: dict, min_overlap: float = MIN_OVERLAP) -> bool:
    if min_overlap <= 0:
        return True
    ga, gb = _bigrams(a["quote"]), _bigrams(b["quote"])
    if not ga or not gb:
        return False
    return len(ga & gb) / min(len(ga), len(gb)) >= min_overlap


def cluster(cards: list[dict], min_overlap: float = MIN_OVERLAP) -> list[list[dict]]:
    """规则聚合：时间相邻 + 内容相关。纯本地，不调模型。

    min_overlap=0 表示只看时间（话题内相邻两张卡间隔 ≤ GAP_HOURS）。
    """
    groups, cur = [], []
    for c in sorted(cards, key=lambda x: x["ts0_ms"]):
        if not cur:
            cur = [c]
            continue
        gap_h = (c["ts0_ms"] - cur[-1]["ts0_ms"]) / 3_600_000
        if gap_h <= GAP_HOURS and _related(cur[-1], c, min_overlap):
            cur.append(c)
        else:
            groups.append(cur)
            cur = [c]
    if cur:
        groups.append(cur)
    return [g for g in groups if len(g) >= MIN_CARDS]


def create(con, cards: list[dict], title: str, state: str) -> int:
    cur = con.execute(
        """INSERT INTO topics(title, state, status, started_ms, last_ms, created_at)
           VALUES(?,?,'open',?,?, datetime('now'))""",
        (title, state, cards[0]["ts0_ms"], cards[-1]["ts0_ms"]))
    tid = cur.lastrowid
    for c in cards:
        con.execute("INSERT OR IGNORE INTO topic_cards(topic_id, card_id, added_at) "
                    "VALUES(?,?, datetime('now'))", (tid, c["id"]))
    con.commit()
    return tid


def name_with(con, cards: list[dict], llm) -> tuple[str, str]:
    """让模型起名 + 写状态。素材必须按说话人分组，否则视角会串。"""
    his = "\n".join(f"- {c['quote'][:50]}" for c in cards if c["speaker"] == "human")
    hers = "\n".join(f"- {c['quote'][:50]}" for c in cards if c["speaker"] == "self")
    text = llm(NAME_PROMPT.format(his=his or "（无）", hers=hers or "（无）"))
    s, e = text.find("{"), text.rfind("}")
    if s < 0 or e <= s:
        return "未命名", ""
    try:
        d = json.loads(text[s:e + 1])
    except Exception:
        return "未命名", ""
    clean = lambda x: re.sub(r"^(他|我)(那边|这边|侧)?[:：]\s*", "", (x or "").strip())
    he, me = clean(d.get("he")), clean(d.get("me"))
    if he and not he.startswith("他"):
        he = "他" + he
    if me and not me.startswith("我"):
        me = "我" + me
    return (d.get("title") or "未命名")[:12], f"{he}；{me}"[:44]


def sweep(con, now_ms: int) -> dict:
    """太久没动 → 休眠；休眠后又有新卡 → 复活。"""
    cutoff = now_ms - DORMANT_DAYS * 86_400_000
    a = con.execute("UPDATE topics SET status='dormant' "
                    "WHERE status='open' AND last_ms < ?", (cutoff,)).rowcount
    b = con.execute("UPDATE topics SET status='open' "
                    "WHERE status='dormant' AND last_ms >= ?", (cutoff,)).rowcount
    con.commit()
    return {"to_dormant": a, "to_open": b}


def close(con, topic_id: int, reason: str = "manual") -> None:
    con.execute("UPDATE topics SET status='closed', closed_at=datetime('now') WHERE id=?",
                (topic_id,))
    con.commit()


def merge(con, src: int, dst: int) -> None:
    con.execute("INSERT OR IGNORE INTO topic_cards(topic_id, card_id, added_at) "
                "SELECT ?, card_id, datetime('now') FROM topic_cards WHERE topic_id=?",
                (dst, src))
    con.execute("DELETE FROM topic_cards WHERE topic_id=?", (src,))
    con.execute("UPDATE topics SET status='merged', merged_into=? WHERE id=?", (dst, src))
    con.commit()


def expand(con, card_ids: list[int], budget: int = 120) -> str:
    """命中之后才展开：这个话题最近两条 + 一句状态。闭环的不展开。"""
    for cid in card_ids:
        t = con.execute(
            """SELECT t.* FROM topics t JOIN topic_cards tc ON tc.topic_id=t.id
               WHERE tc.card_id=? AND t.status IN ('open','dormant')
               ORDER BY t.last_ms DESC LIMIT 1""", (cid,)).fetchone()
        if not t:
            continue
        rows = con.execute(
            """SELECT c.quote, c.speaker FROM cards c
               JOIN topic_cards tc ON tc.card_id=c.id
               WHERE tc.topic_id=? ORDER BY c.ts0_ms DESC LIMIT 2""", (t["id"],)).fetchall()
        body = "；".join(f"{'他' if r['speaker'] == 'human' else '我'}：{r['quote'][:28]}"
                        for r in reversed(rows))
        text = f"（这件事：{t['title']}，{t['state'] or ''}）\n{body}"
        return text if len(text) <= budget else text[:budget].rstrip() + "…"
    return ""
