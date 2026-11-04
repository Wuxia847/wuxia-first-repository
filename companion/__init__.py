"""companion —— 一个陪伴型人格的"潜意识层"骨架。

四层：事件 → 派生（卡片/话题）→ 状态 → 人格锚
全部只用标准库，SQLite 存本地。
"""

from . import (  # noqa: F401
    anchors,
    cards,
    gates,
    gateway,
    ledger,
    retrieval,
    state,
    topics,
)

__all__ = ["ledger", "cards", "state", "retrieval", "gates",
           "anchors", "topics", "gateway"]
__version__ = "0.1.0"
