"""混合检索：没有向量库，也能"想起"对的那句。

打分（权重可调，总和 1.0；没有语义能力时会自动归一化）
    0.45 语义（有 embedding 才算余弦）
    0.25 字面（中文用**双字重叠**，不用 FTS5 的 trigram——它搜不到两字词）
    0.15 时间（越近越高）
    0.10 重要度
    0.05 未闭环（答应过、还没做的事优先被想起）

关键行为：**空是合法的**。分数不够就一条都不给，绝不硬凑。
"""

from __future__ import annotations

import math
import re
import time

WEIGHTS = {"semantic": 0.45, "lexical": 0.25, "recency": 0.15,
           "importance": 0.10, "unresolved": 0.05}
MIN_SCORE = 0.35
RECENCY_TAU_H = 72.0


def bigrams(text: str) -> set[str]:
    t = re.sub(r"[^\w\u4e00-\u9fff]", "", text or "")
    return {t[i:i + 2] for i in range(len(t) - 1)}


def lexical(query: str, doc: str) -> float:
    q, d = bigrams(query), bigrams(doc)
    if not q or not d:
        return 0.0
    return len(q & d) / len(q)


def recency(ts_ms: int, now_ms: int) -> float:
    hours = max(0.0, (now_ms - ts_ms) / 3_600_000)
    return math.exp(-hours / RECENCY_TAU_H)


def search(cards, query: str, k: int = 3, now_ms: int | None = None,
           cosine=None) -> list[dict]:
    """cosine: 可选函数 (card_id, query) -> 0..1。

    没有 cosine 时把语义那 0.45 按比例分给其余项——
    否则纯字面命中永远够不到阈值，检索会永远返回空。
    """
    now = now_ms or int(time.time() * 1000)
    w = dict(WEIGHTS)
    if cosine is None:
        w.pop("semantic")
        total = sum(w.values())
        w = {name: v / total for name, v in w.items()}

    scored = []
    for c in cards:
        lex = lexical(query, c["quote"])
        sem = cosine(c["id"], query) if cosine else 0.0
        if not sem and lex < 0.12:
            continue                      # 先粗筛，省得全量算
        score = (w.get("semantic", 0.0) * sem
                 + w["lexical"] * lex
                 + w["recency"] * recency(int(c["ts0_ms"]), now)
                 + w["importance"] * float(c["importance"])
                 + w["unresolved"] * (1.0 if c["unresolved"] else 0.0))
        if score >= MIN_SCORE:
            scored.append((score, c))
    scored.sort(key=lambda x: -x[0])
    return [{"id": c["id"], "quote": c["quote"], "day": c["day"],
             "score": round(s, 4)} for s, c in scored[:k]]


def render(items: list[dict]) -> str:
    if not items:
        return ""
    return "\n".join(f"- {i['day']}：{i['quote']}" for i in items)
