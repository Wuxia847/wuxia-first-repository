import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from companion import anchors, cards, gates, ledger, retrieval, topics  # noqa: E402

DAY = 86_400_000
T0 = 1_760_000_000_000


def ev(i, role, text, day=0):
    return ledger.Event(channel="im", source="t", session_id="s", seq=i,
                        ts="t", ts_ms=T0 + day * DAY, kind=f"{role}/message",
                        role=role, text=text)


class TestLedger(unittest.TestCase):
    def setUp(self):
        self.con = ledger.connect(":memory:")

    def test_attribution(self):
        self.assertEqual(ledger.classify("assistant", "在")[0], "self")
        self.assertEqual(ledger.classify("tool", "{}")[0], "tool")
        self.assertEqual(ledger.classify("user", "在吗")[0], "human")
        # 注入标记优先于角色：定时任务常以 assistant 入库
        self.assertEqual(ledger.classify("assistant", "[CronJob] 提醒他")[0], "system")

    def test_idempotent(self):
        self.assertTrue(ledger.append(self.con, ev(0, "user", "在吗")))
        self.assertFalse(ledger.append(self.con, ev(0, "user", "在吗")))

    def test_conversation_excludes_system(self):
        ledger.append(self.con, ev(0, "user", "在吗"))
        ledger.append(self.con, ev(1, "assistant", "[CronJob] 提醒"))
        ledger.append(self.con, ev(2, "assistant", "在的"))
        kinds = [r["sender_kind"] for r in ledger.conversation(self.con, "im")]
        self.assertEqual(kinds, ["human", "self"])


class TestCards(unittest.TestCase):
    def setUp(self):
        self.con = ledger.connect(":memory:")

    def test_both_sides(self):
        for i, (role, text) in enumerate([("user", "明天要交方案"),
                                          ("assistant", "先写大纲，别排版")]):
            ledger.append(self.con, ev(i, role, text))
        rows = ledger.conversation(self.con, "im")
        cards.rebuild(self.con, cards.split(rows))
        speakers = sorted(r["speaker"] for r in
                          self.con.execute("SELECT speaker FROM cards"))
        self.assertEqual(speakers, ["human", "self"])

    def test_blocklist_survives_rebuild(self):
        for i, (role, text) in enumerate([("user", "明天要交方案"),
                                          ("assistant", "先写大纲")]):
            ledger.append(self.con, ev(i, role, text))
        rows = ledger.conversation(self.con, "im")
        cards.rebuild(self.con, cards.split(rows))
        q = [r["quote"] for r in self.con.execute(
            "SELECT quote FROM cards WHERE speaker='human'")][0]
        cards.block_quote(self.con, q)
        cards.rebuild(self.con, cards.split(rows))
        left = self.con.execute(
            "SELECT COUNT(*) c FROM cards WHERE status='active' AND quote=?", (q,)).fetchone()["c"]
        self.assertEqual(left, 0)


class TestRetrieval(unittest.TestCase):
    def test_empty_is_legal(self):
        self.assertEqual(retrieval.search([], "随便"), [])

    def test_hits_without_vectors(self):
        doc = [{"id": 1, "quote": "今天考完期末考了", "ts0_ms": T0,
                "importance": 0.5, "unresolved": 0, "day": "2025-10-09"}]
        hits = retrieval.search(doc, "期末考考完了", now_ms=T0 + 3_600_000)
        self.assertEqual(len(hits), 1)


class TestAnchors(unittest.TestCase):
    def test_rejects_fabricated_evidence(self):
        index = [("我说话短", "self", "2025-10-09")]
        item = {"key": "k", "value": "v", "kind": "self",
                "evidence": ["没人说过的话"]}
        self.assertIsNone(anchors.verify(item, index))

    def test_accepts_cross_day_evidence(self):
        index = [("我说话短", "self", "2025-10-09"),
                 ("我不爱解释", "self", "2025-10-10"),
                 ("我嘴硬", "self", "2025-10-12")]
        item = {"key": "k", "value": "v", "kind": "self",
                "evidence": ["我说话短", "我不爱解释", "我嘴硬"]}
        out = anchors.verify(item, index)
        self.assertEqual(out["support"], 3)
        self.assertEqual(out["span_days"], 3)

    def test_rejects_single_day(self):
        index = [("我说话短", "self", "2025-10-09"),
                 ("我不爱解释", "self", "2025-10-09"),
                 ("我嘴硬", "self", "2025-10-09")]
        item = {"key": "k", "value": "v", "kind": "self",
                "evidence": ["我说话短", "我不爱解释", "我嘴硬"]}
        self.assertIsNone(anchors.verify(item, index))


class TestTopicsAndGates(unittest.TestCase):
    def test_cluster_by_time(self):
        cards_ = [{"id": i, "quote": f"话{i}", "ts0_ms": T0 + i * 60_000}
                  for i in range(3)]
        self.assertEqual(len(topics.cluster(cards_, min_overlap=0)), 1)

    def test_quiet_hours_block(self):
        res = gates.evaluate({"hour": 3, "minute": 0, "sent_today": 0,
                              "last_sent_ms": None, "ignored_times": 0,
                              "last_user_ms": None, "last_user_text": "",
                              "now_ms": T0, "impulse": 0.9, "threshold": 0.58})
        self.assertFalse(res["passed"])
        self.assertIn("quiet", res["reason"])

    def test_budget_block(self):
        res = gates.evaluate({"hour": 15, "minute": 0, "sent_today": 3,
                              "last_sent_ms": None, "ignored_times": 0,
                              "last_user_ms": T0 - 3_600_000, "last_user_text": "",
                              "now_ms": T0, "impulse": 0.9, "threshold": 0.58})
        self.assertFalse(res["passed"])
        self.assertIn("budget", res["reason"])


if __name__ == "__main__":
    unittest.main()
