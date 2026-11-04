"""人格锚：关于"我是谁"的稳定描述。只读、可谱系、必须可回指原文。

四条规则（少一条都会烂）
1. **提议 ≠ 生效**：程序只能提，人确认后才进提示词。
2. **每条必须有原文依据**，且能分清是自己说的还是对方说的——依据在原文里找不到，丢弃。
3. **阈值**：证据 ≥3 条不同事件，跨度 ≥3 天（性格/行为）、≥7 天（关系）。
4. **只增与退休，不改写**：要改就新增一条，旧锚记 replaced_by，保留演化史。

llm 参数是可选的回调 `call(prompt: str) -> str`，不传就只能做机械部分。
"""

from __future__ import annotations

import json
import re

KIND_SPAN = {"self": 3, "behavior": 3, "relation": 7}
MIN_SUPPORT = 3
MAX_PROMPT = 6
KIND_BUDGET = {"self": 3, "behavior": 2, "relation": 1}

PROMPT = """材料分三类，来源不能混：

【自己说】{self}
【对方说】{user}

提取 3–5 条人格锚，每条：
key（≤8字）、value（≤20字，第一人称）、kind（self性格/behavior行为/relation关系）、
evidence（**原文数组，至少3条，逐字摘自对应来源，尽量来自不同日期**）。

规则：性格只能引用【自己说】；不许把对方的话写成自己的性格；只输出 JSON 数组。"""


def _norm(t: str) -> str:
    return re.sub(r"[\s，。！？、,.!?：；“”\"'（）()…\-—]", "", t or "")


def verify(item: dict, index: list[tuple[str, str, str]]) -> dict | None:
    """机械校验：证据能否回指、够不够、跨度够不够。

    index: [(原文, speaker, day)]——原文会在这里归一化，传原样即可
    """
    kind = (item.get("kind") or "self").strip()
    kind = kind if kind in KIND_SPAN else "self"
    ev = item.get("evidence")
    ev = [ev] if isinstance(ev, str) else (ev or [])

    hits = []
    for q in ev:
        e = _norm(q)
        if len(e) < 3:
            continue      # 太短容易误匹配
        hit = next(((sp, day) for nq, sp, day in index if e in _norm(nq)), None)
        if not hit:
            hit = next(((sp, day) for nq, sp, day in index if e[:8] in _norm(nq)), None)
        if not hit:
            continue
        sp, day = hit
        if kind == "self" and sp != "self":
            continue                      # 性格类证据必须来自她自己
        hits.append((sp, day))
    if len(hits) < MIN_SUPPORT:
        return None
    from datetime import date
    days = sorted({d for _, d in hits})
    span = abs((date.fromisoformat(days[-1]) - date.fromisoformat(days[0])).days)
    if span < KIND_SPAN[kind]:
        return None
    return {"kind": kind, "support": len(hits), "span_days": span}


def propose(con, index, evidence: dict, llm) -> list[dict]:
    """让模型提议，**校验通过才入库**（status=proposed）。"""
    text = llm(PROMPT.format(self=evidence.get("self", "（无）"),
                             user=evidence.get("user", "（无）")))
    s, e = text.find("["), text.rfind("]")
    if s < 0 or e < 0:
        return []
    try:
        items = json.loads(text[s:e + 1])
    except Exception:
        return []

    out = []
    for it in items[:5]:
        key, value = (it.get("key") or "")[:20], (it.get("value") or "")[:60]
        v = verify(it, index)
        if not key or not value or not v:
            continue
        con.execute(
            """INSERT INTO anchors(key, value, kind, status, support, span_days, created_at)
               VALUES(?,?,?,'proposed',?,?, datetime('now'))
               ON CONFLICT(key) DO UPDATE SET value=excluded.value, kind=excluded.kind,
                   status='proposed', support=excluded.support, span_days=excluded.span_days
               WHERE anchors.status != 'active'""",
            (key, value, v["kind"], v["support"], v["span_days"]))
        out.append({"key": key, "value": value, **v})
    con.commit()
    return out


def init(con) -> None:
    con.execute("""CREATE TABLE IF NOT EXISTS anchors(
        key TEXT PRIMARY KEY, value TEXT NOT NULL, kind TEXT DEFAULT 'self',
        status TEXT DEFAULT 'proposed', support INTEGER DEFAULT 0,
        span_days INTEGER DEFAULT 0, health TEXT DEFAULT 'ok',
        replaced_by TEXT, created_at TEXT, checked_at TEXT)""")
    con.commit()


def confirm(con, key: str) -> bool:
    cur = con.execute("UPDATE anchors SET status='active' WHERE key=?", (key,))
    con.commit()
    return cur.rowcount > 0


def retire(con, key: str) -> None:
    con.execute("UPDATE anchors SET status='retired' WHERE key=?", (key,))
    con.commit()


def replace_with(con, old: str, new: str) -> bool:
    """谱系：新锚替代旧锚，旧的保留并记 replaced_by。"""
    if not con.execute("SELECT 1 FROM anchors WHERE key=?", (new,)).fetchone():
        return False
    con.execute("UPDATE anchors SET status='retired', replaced_by=? WHERE key=?", (new, old))
    con.commit()
    return True


def active(con) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT * FROM anchors WHERE status='active' ORDER BY kind, created_at")]


def prompt_block(con) -> str:
    """进提示词：按类限量，总共不超过 6 条。"""
    rows, out = active(con), []
    for kind, budget in KIND_BUDGET.items():
        out += [r["value"] for r in rows if r["kind"] == kind][:budget]
    out = out[:MAX_PROMPT]
    return ("你对自己的认知：\n" + "\n".join(f"- {v}" for v in out)) if out else ""


def check(con, cosine_search) -> list[dict]:
    """体检：最近窗口内还有多少支持证据（不调模型）。"""
    import time
    now = int(time.time() * 1000)
    window = {"self": 60, "behavior": 30, "relation": 30}
    out = []
    for a in active(con):
        days = window.get(a["kind"], 30)
        hits = [h for h in cosine_search(a["value"], 10)
                if h["score"] >= 0.45 and h["age_ms"] <= days * 86_400_000]
        health = "ok" if len(hits) >= 3 else ("fading" if hits else "stale")
        con.execute("UPDATE anchors SET health=?, checked_at=datetime('now') WHERE key=?",
                    (health, a["key"]))
        out.append({**a, "support_recent": len(hits), "health": health})
    con.commit()
    return out
