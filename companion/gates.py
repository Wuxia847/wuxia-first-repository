"""闸门：她"想说话"和"真的说"之间的六道检查，全部是规则，零 token。

为什么要有闸：主动性最大的风险不是"她不说"，而是"她说太多"。
每一道闸都对应一种打扰：半夜、刷屏、你正在说话、你说了忙、上次被无视。

铁律：**"想发但没发"必须留痕**（intents 表），否则没法判断她是变安静了还是变吵了。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))

DEFAULT = {
    "quiet_start": "00:30",       # 安静时段（跨零点）
    "quiet_end": "06:30",
    "daily_budget": 3,            # 一天最多主动几条
    "min_gap_hours": 4.0,         # 两条之间最少隔多久
    "backoff_factor": 2.0,        # 被忽略一次，间隔翻倍
    "min_gap_cap_hours": 48.0,
    "conversation_grace_min": 15,  # 你刚说完话，别插嘴
    "silent_after_ignored": 2,     # 连续被忽略几次 → 静默
    "silent_hours": 24.0,
    "busy_window_min": 180,
}
BUSY_WORDS = ("忙", "睡了", "睡了先", "开会", "上课", "先不聊", "回头聊")


def _hm(text: str) -> int:
    h, m = text.split(":")
    return int(h) * 60 + int(m)


def in_quiet(hour: int, minute: int, cfg: dict) -> bool:
    cur, start, end = hour * 60 + minute, _hm(cfg["quiet_start"]), _hm(cfg["quiet_end"])
    return (start <= cur or cur < end) if start > end else (start <= cur < end)


def evaluate(ctx: dict, cfg: dict | None = None) -> dict:
    """ctx 需要：hour, minute, sent_today, last_sent_ms, ignored_times,
    last_user_ms, last_user_text, now_ms, impulse, threshold。"""
    cfg = {**DEFAULT, **(cfg or {})}
    gates, failed = [], None

    def gate(name: str, ok: bool, detail: str) -> None:
        nonlocal failed
        gates.append({"name": name, "pass": ok, "detail": detail})
        if not ok and failed is None:
            failed = f"{name}: {detail}"

    now = ctx["now_ms"]
    gate("impulse", ctx["impulse"] >= ctx["threshold"],
         f"冲动 {ctx['impulse']:.2f} / 阈值 {ctx['threshold']:.2f}")
    gate("quiet", not in_quiet(ctx["hour"], ctx["minute"], cfg),
         f"{cfg['quiet_start']}–{cfg['quiet_end']} 静默")
    gate("budget", ctx["sent_today"] < cfg["daily_budget"],
         f"今天已说 {ctx['sent_today']}/{cfg['daily_budget']}")

    last = ctx.get("last_sent_ms")
    need = min(cfg["min_gap_cap_hours"],
               cfg["min_gap_hours"] * cfg["backoff_factor"] ** ctx.get("ignored_times", 0))
    gap_h = (now - last) / 3_600_000 if last else 1e9
    gate("rhythm", gap_h >= need,
         f"距上次 {gap_h:.1f}h / 需要 {need:.1f}h（连忽略 {ctx.get('ignored_times', 0)}）")

    since_user = (now - ctx["last_user_ms"]) / 60_000 if ctx.get("last_user_ms") else 1e9
    gate("context", since_user >= cfg["conversation_grace_min"],
         f"你 {since_user:.0f} 分钟前说过话")
    busy = any(w in (ctx.get("last_user_text") or "") for w in BUSY_WORDS) \
        and since_user < cfg["busy_window_min"]
    gate("busy", not busy, "你刚说过忙/要睡")

    silent = ctx.get("ignored_times", 0) >= cfg["silent_after_ignored"]
    since_any = (now - (last or 0)) / 3_600_000
    gate("self_review", not (silent and since_any < cfg["silent_hours"]),
         f"连续被忽略 {ctx.get('ignored_times', 0)} 次，静默 {cfg['silent_hours']:.0f}h")

    return {"passed": failed is None, "gates": gates, "reason": failed}


def allows_now(hour: int, minute: int, sent_today: int,
               cfg: dict | None = None) -> tuple[bool, str | None]:
    """只查作息/预算——给"到点就该做"的仪式用（它不该等"想说话"）。"""
    cfg = {**DEFAULT, **(cfg or {})}
    if in_quiet(hour, minute, cfg):
        return False, f"{cfg['quiet_start']}–{cfg['quiet_end']} 静默"
    if sent_today >= cfg["daily_budget"]:
        return False, f"今天已说 {sent_today}/{cfg['daily_budget']}"
    return True, None


def local_now(now_ms: int) -> tuple[int, int]:
    dt = datetime.fromtimestamp(now_ms / 1000, CST)
    return dt.hour, dt.minute
