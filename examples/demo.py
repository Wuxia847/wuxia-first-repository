"""可跑的最小演示：事件 → 卡片 → 状态 → 检索 → 人格锚校验 → 话题聚合。

不需要任何依赖、不联网：
    python examples/demo.py
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from companion import anchors, cards, gates, ledger, retrieval, state, topics  # noqa: E402

DAY = 86_400_000
T0 = 1_760_000_000_000        # 固定基准时间，输出可复现


def ev(i, role, text, day=0, minute=0):
    ts = T0 + day * DAY + minute * 60_000
    return ledger.Event(
        channel="im", source="demo", session_id="s1", seq=i,
        ts=f"day{day}+{minute}m", ts_ms=ts, kind=f"{role}/message",
        role=role, text=text)


SCRIPT = [
    (0, 0, "user", "今天考完期末考了，人都傻了"),
    (0, 1, "assistant", "考完就别想了。晚上早点睡。"),
    (1, 0, "user", "明天还要交方案"),
    (1, 30, "assistant", "先写个大纲，别一上来就排版。"),
    (1, 90, "user", "你觉得我能写完吗"),
    (1, 91, "assistant", "能。写不完就砍掉一半，别硬撑。"),
    (2, 0, "user", "今天又熬夜了"),
    (2, 1, "assistant", "说了多少遍。十二点前躺下。"),
    (3, 0, "user", "方案交了，终于轻松了"),
    (3, 1, "assistant", "那今天就别安排新的事了。"),
    (3, 2, "user", "[CronJob] 提醒他说晚安"),
    (3, 3, "assistant", "晚安。明天见。"),
]


def main() -> None:
    con = ledger.connect(":memory:")

    n = 0
    for idx, (day, minute, role, text) in enumerate(SCRIPT):
        if ledger.append(con, ev(idx, role, text, day, minute)):
            n += 1
    print(f"① 事件账：写入 {n} 条（去重后）")

    rows = ledger.conversation(con, "im")
    kinds = {}
    for r in rows:
        kinds[r["sender_kind"]] = kinds.get(r["sender_kind"], 0) + 1
    print(f"   来源分布：{kinds}   ← 注入的 [CronJob] 被挡掉了")

    groups = cards.split(rows)
    stats = cards.rebuild(con, groups)
    print(f"② 卡片：{len(groups)} 段对话 → 新建 {stats['created']} 张")
    for r in con.execute("SELECT speaker, quote FROM cards ORDER BY id LIMIT 4"):
        print(f"   [{'她' if r['speaker'] == 'self' else '他'}] {r['quote'][:26]}")

    st = state.compute(rows, T0 + 3 * DAY + 3_600_000)
    print(f"③ 状态：{state.to_prose(st)}")
    print(f"   内部坐标：亲密 {st['intimacy']} 信任 {st['trust']} "
          f"张力 {st['tension']} 热度 {st['warmth']}")

    all_cards = [dict(r) for r in con.execute("SELECT * FROM cards")]
    hit = retrieval.search(all_cards, "期末考考完了", k=2, now_ms=T0 + 3 * DAY)
    print(f"④ 检索：「期末考考完了」命中 {len(hit)} 条（空是合法的）")
    for h in hit:
        print(f"   {h['score']:.2f}  {h['quote'][:28]}")

    index = [(r["quote"], r["speaker"], r["day"]) for r in
             con.execute("SELECT * FROM cards")]
    # 取三张不同日期的"她自己说"当证据，跨度才够
    by_day = {}
    for r in con.execute("SELECT quote, speaker, day FROM cards ORDER BY ts0_ms"):
        if r["speaker"] == "self":
            by_day.setdefault(r["day"], r["quote"])
    days = sorted(by_day)
    picked = [by_day[days[0]], by_day[days[1]], by_day[days[-1]]]
    good = {"key": "短句", "value": "我说话短", "kind": "self", "evidence": picked}
    bad = {"key": "编的", "value": "我从不熬夜", "kind": "self",
           "evidence": ["这句话根本没人说过"]}
    print(f"⑤ 人格锚校验：3 条跨天真证据 → {anchors.verify(good, index)}")
    print(f"   编造的证据 → {anchors.verify(bad, index)}   ← None 表示丢弃")

    cl = topics.cluster(all_cards, min_overlap=0)
    print(f"⑥ 话题：聚出 {len(cl)} 组（每组 {[len(g) for g in cl]} 张卡）")

    now = T0 + 3 * DAY + 3_600_000
    hour, minute = gates.local_now(now)
    res = gates.evaluate({
        "hour": hour, "minute": minute, "sent_today": 0,
        "last_sent_ms": now - 10 * 3_600_000, "ignored_times": 0,
        "last_user_ms": now - 30 * 60_000, "last_user_text": "方案交了",
        "now_ms": now, "impulse": 0.62, "threshold": 0.58})
    print(f"⑦ 闸门：{'放行' if res['passed'] else '拦下'}"
          f"（{res['reason'] or '全部通过'}）")
    for g in res["gates"]:
        print(f"   {'✓' if g['pass'] else '✗'} {g['name']:12s} {g['detail']}")


if __name__ == "__main__":
    main()
