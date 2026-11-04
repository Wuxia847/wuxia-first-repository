"""事件账：只追加，不改写。所有记忆都从这里派生。

设计要点
1. **只追加**：insert-only，靠 UNIQUE(hash) 去重，重复导入不会产生重复事件。
2. **谁在说话**：每条事件带 sender_kind（self/human/other/system/tool）。
   这一步是后面一切的前提——分不清谁说的，记忆和人格都会歪。
3. **时间**：ts_ms（毫秒）+ ts（ISO）。业务上的"一天"由调用方定义（见 state.py）。
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id          INTEGER PRIMARY KEY,
  channel     TEXT NOT NULL,              -- 渠道：qq / im / local
  source      TEXT NOT NULL,              -- 数据来源
  session_id  TEXT NOT NULL,
  seq         INTEGER NOT NULL,
  ts          TEXT NOT NULL,
  ts_ms       INTEGER NOT NULL,
  kind        TEXT NOT NULL,
  role        TEXT NOT NULL,              -- user / assistant / tool / system
  sender_kind TEXT,                       -- self / human / other / system / tool
  sender_id   TEXT,
  text        TEXT NOT NULL,
  hash        TEXT NOT NULL UNIQUE,
  ingested_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts_ms);
CREATE INDEX IF NOT EXISTS idx_events_role ON events(role);
"""

# 插件/框架注入的消息：它们是"指令"，不是人说的话
INJECT_MARKS = (
    "[cronjob]", "[schedule]", "output your last task result",
    "<system_reminder>",
)


def connect(path: str = ":memory:") -> sqlite3.Connection:
    con = sqlite3.connect(path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    return con


def stable_hash(*parts: object) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p).encode("utf-8", "ignore"))
        h.update(b"\x00")
    return h.hexdigest()


def classify(role: str, text: str, bot_id: str = "bot",
             owner_id: str = "owner") -> tuple[str, str | None]:
    """谁在说话——结构判定，不猜语气。

    注入标记优先于角色：定时任务/插件指令经常以 assistant 角色入库，
    只看 role 会把"被指使说的话"当成"她自己说的话"。
    """
    role = (role or "").strip()
    t = (text or "").lower()
    if any(m in t for m in INJECT_MARKS):
        return "system", None
    if role == "assistant":
        return "self", bot_id
    if role == "tool":
        return "tool", None
    if role == "system":
        return "system", None
    if role != "user":
        return "system", None
    return "human", owner_id


@dataclass
class Event:
    channel: str
    source: str
    session_id: str
    seq: int
    ts: str
    ts_ms: int
    kind: str
    role: str
    text: str
    sender_kind: str | None = None
    sender_id: str | None = None


def append(con: sqlite3.Connection, ev: Event) -> bool:
    """追加一条事件。已存在（同 hash）返回 False。"""
    if ev.sender_kind is None:
        ev.sender_kind, ev.sender_id = classify(ev.role, ev.text)
    cur = con.execute(
        """INSERT OR IGNORE INTO events
           (channel, source, session_id, seq, ts, ts_ms, kind, role,
            sender_kind, sender_id, text, hash, ingested_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ev.channel, ev.source, ev.session_id, ev.seq, ev.ts, ev.ts_ms, ev.kind,
         ev.role, ev.sender_kind, ev.sender_id, ev.text,
         stable_hash(ev.channel, ev.session_id, ev.seq, ev.kind, ev.text),
         ev.ts),
    )
    con.commit()
    return cur.rowcount > 0


def conversation(con: sqlite3.Connection, channel: str,
                 kinds: tuple[str, ...] = ("self", "human")) -> list[sqlite3.Row]:
    """只取"真人"和"她自己"的话——别人的话、系统提示不进记忆。"""
    marks = ",".join("?" * len(kinds))
    return con.execute(
        f"""SELECT * FROM events WHERE channel=? AND sender_kind IN ({marks})
            ORDER BY ts_ms, id""", (channel, *kinds)).fetchall()
