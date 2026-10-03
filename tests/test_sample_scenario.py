"""全链路联调样例测试：从 data/sample_events.jsonl 重放并核对关键口径。"""

import json
import unittest
from pathlib import Path

from scripts.build_sample import (SEG_DEMO, SEG_FINALE, SEG_MIC, SEG_OPEN,
                                  SESSION, build)
from src.event_store import EventStore
from src.rights_ledger import RightsLedger
from src.settlement import SettlementService
from src.views import AudienceView, EditorView, HolderView, PublishingService

DATA = Path(__file__).parents[1] / "data" / "sample_events.jsonl"


class SampleScenarioTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store, cls.ctx = build()
        cls.led = cls.ctx["editor"].ledger

    def test_sample_file_roundtrips(self):
        self.assertTrue(DATA.exists())
        loaded = EventStore.load_jsonl(DATA)
        self.assertEqual(len(loaded), len(self.store))

    def test_editor_matrix_for_four_segments(self):
        ed = EditorView(self.led)
        matrix = {sid: {u: ed.segment_card(sid)["uses"][u]["status"]
                        for u in ("LIVE_WEBCAST", "REPLAY", "COMMERCIAL_CLIP")}
                  for sid in (SEG_OPEN, SEG_DEMO, SEG_MIC, SEG_FINALE)}
        # 开场：回放可用；伴奏/平台切片无商业剪辑权 → 剪辑整段不可用
        self.assertEqual(matrix[SEG_OPEN]["REPLAY"], "available")
        self.assertEqual(matrix[SEG_OPEN]["COMMERCIAL_CLIP"], "blocked")
        # 示范：老影像到期 → 回放局部遮罩
        self.assertEqual(matrix[SEG_DEMO]["REPLAY"], "partial")
        # 连麦：口头同意已追认 + 监护人同意已补齐 → 全部可用
        self.assertEqual(matrix[SEG_MIC]["REPLAY"], "available")
        # 收尾：权利人要求最后 30 秒下架 → 回放局部遮罩
        finale = ed.segment_card(SEG_FINALE)["uses"]["REPLAY"]
        self.assertEqual(finale["status"], "partial")
        self.assertTrue(any(b["reason"] == "takedown_request"
                            for b in finale["blockers"]))

    def test_no_pending_consent_after_followup(self):
        self.assertEqual(EditorView(self.led).pending_consents(), [])

    def test_replay_plan_masks_three_specific_windows_only(self):
        plan = self.led.replay_plan(SESSION)
        masked = [(s["segment_id"], m["window"], m["reason"])
                  for s in plan["segments"] for m in s["masked"]]
        self.assertEqual(masked, [
            (SEG_DEMO, [2280000, 2340000], "rights_expired"),
            (SEG_FINALE, [3870000, 3900000], "takedown_request"),
        ])

    def test_revenue_tip_1001_initial_plus_supplement_minus_reversal(self):
        st = SettlementService(self.store, self.led)
        sheet = st.tip_sheet("tip-1001")
        net = sheet["net_by_party"]
        # 首分 10000：平台 3000；池 7000；传承人 20%*7000=1400；伴奏 350；机构余量 5250
        # 补付传承人 +1500（机构 -1500）；冲正伴奏 -350（机构 +350）
        self.assertEqual(net["platform"], 3000)
        self.assertEqual(net["participant:p-suxiu-master"], 2900)
        self.assertEqual(net["participant:p-accompanist"], 0)
        self.assertEqual(net["house"], 4100)
        self.assertEqual(sheet["net_total_fen"], 10_000)
        # 原条目仍保留旧合同版本，补付行可独立核对
        rows = st.holder_statement("participant:p-suxiu-master")["rows"]
        kinds_1001 = [(r["tip_order_id"], r["kind"]) for r in rows]
        self.assertIn(("tip-1001", "initial"), kinds_1001)
        self.assertIn(("tip-1001", "supplement"), kinds_1001)
        # tip-1002 虽已退款，原条目与冲正都保留，净额为 0
        self.assertEqual(sum(r["amount_fen"] for r in rows
                             if r["tip_order_id"] == "tip-1002"), 0)

    def test_revenue_tip_1002_refund_nets_to_zero(self):
        st = SettlementService(self.store, self.led)
        sheet = st.tip_sheet("tip-1002")
        self.assertEqual(sheet["status"], "refunded")
        self.assertEqual(sheet["net_total_fen"], 0)
        self.assertTrue(all(v == 0 for v in sheet["net_by_party"].values()))

    def test_published_clip_feed_only_has_clear_clip(self):
        feed = self.ctx["audience_cn"].clip_feed()
        self.assertEqual([c["clip_id"] for c in feed], ["clip-demo-hands"])

    def test_audience_replay_shows_reason_copy(self):
        feed = self.ctx["audience_cn"].replay_feed(SESSION)
        notes = [(seg["segment_id"], n["message"])
                 for seg in feed["segments"] for n in seg["unavailable_notes"]]
        self.assertTrue(any("授权已到期" in msg for _, msg in notes))
        self.assertTrue(any("权利人要求" in msg for _, msg in notes))

    def test_events_are_immutable_append_only(self):
        raw = DATA.read_text(encoding="utf-8").splitlines()
        versions: dict[tuple, int] = {}
        ids: set[str] = set()
        for line in raw:
            e = json.loads(line)
            self.assertNotIn(e["event_id"], ids)
            ids.add(e["event_id"])
            key = (e["aggregate_type"], e["aggregate_id"])
            versions[key] = versions.get(key, 0) + 1
            self.assertEqual(e["version"], versions[key])


if __name__ == "__main__":
    unittest.main()
