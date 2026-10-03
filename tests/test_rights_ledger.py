import json
import tempfile
import unittest
from pathlib import Path

from src import catalogs as C
from src.events import EventStore, JsonlEventStore, EventStoreError
from src.ledger import (
    RightsLedger,
    RightsLedgerError,
    fold,
    parse_dt,
    segment_verdict,
)
from src.policies import normalize, windows_len
from src.views import editor_workspace, viewer_replay, right_holder_statement

SESSION = "s1"
SEG1, SEG2 = "seg1", "seg2"
INH, MUS, V1, VMIN = "inh", "mus", "v1", "vmin"
M_DEMO, M_LINK, M_MINOR, M_BGM, M_OK = "m-demo", "m-link", "m-minor", "m-bgm", "m-ok"
MIN = 60_000


def build_basic(ledger: RightsLedger) -> None:
    """一场两段直播：示范段 + 连麦段；演前合同与基础许可。"""
    ledger.execute_contract("c1", SESSION, "2026-09-01T10:00:00+08:00",
                            revenue_share={INH: 0.5, MUS: 0.1}, platform_rate=0.4)
    ledger.register_participant(INH, "传承人", C.ROLE_INHERITOR, "2026-09-01T10:00:00+08:00")
    ledger.register_participant(MUS, "乐手", C.ROLE_MUSICIAN, "2026-09-01T10:00:00+08:00")
    ledger.register_participant(V1, "观众甲", C.ROLE_VIEWER, "2026-09-01T10:00:00+08:00")
    ledger.register_participant(VMIN, "小观众", C.ROLE_VIEWER, "2026-09-01T10:00:00+08:00",
                                is_minor=True)
    ledger.schedule_session(SESSION, "测试直播", "2026-10-01T19:00:00+08:00")
    ledger.register_segment(SEG1, SESSION, "示范", [{"start_ms": 0, "end_ms": 20 * MIN}],
                            "2026-10-01T19:00:00+08:00")
    ledger.register_segment(SEG2, SESSION, "连麦", [{"start_ms": 20 * MIN, "end_ms": 40 * MIN}],
                            "2026-10-01T19:00:00+08:00")
    ledger.start_session(SESSION, "2026-10-01T19:00:00+08:00")
    ledger.link_material(M_DEMO, material_type=C.MATERIAL_HERITAGE_DEMO, segment_id=SEG1,
                         windows=[{"start_ms": 0, "end_ms": 20 * MIN}],
                         at="2026-10-01T19:01:00+08:00", participant_id=INH)
    ledger.grant_permission("p-demo", material_id=M_DEMO, right_holder_id=INH,
                            uses=list(C.USES), territories=["CN"],
                            valid_from="2026-09-01T00:00:00+08:00", valid_until=None,
                            at="2026-09-01T11:00:00+08:00")


class EventStoreTest(unittest.TestCase):
    def test_versions_are_sequential_and_ids_unique(self) -> None:
        store = EventStore()
        e1 = store.append(event_type=C.EV_SESSION_SCHEDULED, aggregate_type=C.AGG_LIVE_SESSION,
                          aggregate_id="a", occurred_at="2026-10-01T19:00:00+08:00",
                          summary="x", payload={"title": "t"})
        e2 = store.append(event_type=C.EV_SESSION_STARTED, aggregate_type=C.AGG_LIVE_SESSION,
                          aggregate_id="a", occurred_at="2026-10-01T19:01:00+08:00", summary="y")
        self.assertEqual((e1["version"], e2["version"]), (1, 2))
        self.assertEqual((e1.seq, e2.seq), (1, 2))
        with self.assertRaises(EventStoreError):
            store.append(event_type=C.EV_SESSION_ENDED, aggregate_type=C.AGG_LIVE_SESSION,
                         aggregate_id="a", occurred_at="2026-10-01T20:00:00+08:00",
                         summary="z", version=9)
        with self.assertRaises(EventStoreError):
            store.append(event_type=C.EV_SESSION_ENDED, aggregate_type=C.AGG_LIVE_SESSION,
                         aggregate_id="a", occurred_at="2026-10-01T20:00:00+08:00",
                         summary="z", event_id=e1["event_id"])

    def test_invalid_payload_rejected(self) -> None:
        store = EventStore()
        with self.assertRaises(EventStoreError):
            store.append(event_type=C.EV_SEGMENT_REGISTERED, aggregate_type=C.AGG_SEGMENT,
                         aggregate_id="s", occurred_at="2026-10-01T19:00:00+08:00",
                         summary="坏区间", payload={
                             "session_id": SESSION,
                             "time_windows": [{"start_ms": 100, "end_ms": 50}]})

    def test_jsonl_persistence_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "log.jsonl"
            s1 = JsonlEventStore(path)
            RightsLedger(s1).schedule_session(SESSION, "持久化直播",
                                              "2026-10-01T19:00:00+08:00")
            self.assertEqual(len(s1), 1)
            s2 = JsonlEventStore(path)
            self.assertEqual(len(s2), 1)
            self.assertEqual(fold(s2).sessions[SESSION]["status"], "SCHEDULED")


class ConsentAndIntersectionTest(unittest.TestCase):
    def test_verbal_consent_pending_only_blocks_replay_not_live(self) -> None:
        store = EventStore()
        L = RightsLedger(store)
        build_basic(L)
        L.link_material(M_LINK, material_type=C.MATERIAL_VOICE_LINK, segment_id=SEG2,
                        windows=[{"start_ms": 20 * MIN, "end_ms": 30 * MIN}],
                        at="2026-10-01T19:21:00+08:00", participant_id=V1)
        L.capture_verbal_consent("p-link", material_id=M_LINK, right_holder_id=V1,
                                 uses=list(C.USES), at="2026-10-01T19:21:30+08:00")
        st = fold(store)
        seg = st.segments[SEG2]
        at = parse_dt("2026-10-01T20:00:00+08:00")
        live = segment_verdict(st, seg, C.USE_LIVE, at=at)
        replay = segment_verdict(st, seg, C.USE_REPLAY, at=at)
        # 直播当时已记录口头同意：连麦区间可播；其余区间无素材→未授权
        self.assertTrue(any(20 * MIN <= w["start_ms"] and w["end_ms"] <= 30 * MIN
                            for w in live["allowed"]))
        # 回放：口头同意未补签，连麦区间被 PENDING_CONSENT 阻断
        self.assertNotIn(C.REASON_PENDING_CONSENT, live["blockers"])
        self.assertIn(C.REASON_PENDING_CONSENT, replay["blockers"])
        # 补签后回放放行
        L.confirm_consent("p-link", "2026-10-02T10:00:00+08:00")
        st2 = fold(store)
        replay2 = segment_verdict(st2, st2.segments[SEG2], C.USE_REPLAY,
                                  at=parse_dt("2026-10-02T12:00:00+08:00"))
        self.assertNotIn(C.REASON_PENDING_CONSENT, replay2["blockers"])
        self.assertEqual(windows_len(replay2["allowed"]), 10 * MIN)

    def test_permission_intersection_across_materials(self) -> None:
        """同一段落两个素材：一个全许可、一个只授权回放 → 商业剪辑只能取交集。"""
        store = EventStore()
        L = RightsLedger(store)
        build_basic(L)
        L.link_material(M_OK, material_type=C.MATERIAL_PLATFORM_CLIP, segment_id=SEG1,
                        windows=[{"start_ms": 0, "end_ms": 20 * MIN}],
                        at="2026-10-01T19:02:00+08:00")
        L.grant_permission("p-ok", material_id=M_OK, right_holder_id=C.ROLE_PLATFORM,
                           uses=[C.USE_REPLAY], territories=["CN"],
                           valid_from="2026-09-01T00:00:00+08:00", valid_until=None,
                           at="2026-09-01T11:00:00+08:00")
        st = fold(store)
        at = parse_dt("2026-10-02T12:00:00+08:00")
        replay = segment_verdict(st, st.segments[SEG1], C.USE_REPLAY, at=at)
        cut = segment_verdict(st, st.segments[SEG1], C.USE_COMMERCIAL_CUT, at=at)
        self.assertEqual(windows_len(replay["allowed"]), 20 * MIN)
        self.assertEqual(windows_len(cut["allowed"]), 0)
        self.assertIn(C.REASON_NOT_LICENSED, cut["blockers"])

    def test_territory_intersection(self) -> None:
        store = EventStore()
        L = RightsLedger(store)
        build_basic(L)
        st = fold(store)
        at = parse_dt("2026-10-02T12:00:00+08:00")
        cn = segment_verdict(st, st.segments[SEG1], C.USE_REPLAY, territory="CN", at=at)
        us = segment_verdict(st, st.segments[SEG1], C.USE_REPLAY, territory="US", at=at)
        self.assertEqual(windows_len(cn["allowed"]), 20 * MIN)
        self.assertEqual(windows_len(us["allowed"]), 0)
        self.assertIn(C.REASON_GEO_OUT_OF_SCOPE, us["blockers"])


class RestrictionTest(unittest.TestCase):
    def _link_minor(self, L: RightsLedger) -> None:
        L.link_material(M_MINOR, material_type=C.MATERIAL_VOICE_LINK, segment_id=SEG2,
                        windows=[{"start_ms": 30 * MIN, "end_ms": 40 * MIN}],
                        at="2026-10-01T19:31:00+08:00", participant_id=VMIN, is_minor=True)
        L.capture_verbal_consent("p-minor", material_id=M_MINOR, right_holder_id=VMIN,
                                 uses=list(C.USES), at="2026-10-01T19:31:30+08:00",
                                 note="需监护人补签")

    def test_minor_restricts_only_its_window_not_whole_session(self) -> None:
        store = EventStore()
        L = RightsLedger(store)
        build_basic(L)
        self._link_minor(L)
        st = fold(store)
        at = parse_dt("2026-10-02T12:00:00+08:00")
        v = segment_verdict(st, st.segments[SEG2], C.USE_REPLAY, at=at)
        self.assertEqual(v["blockers"][C.REASON_MINOR_ON_MIC],
                         [{"start_ms": 30 * MIN, "end_ms": 40 * MIN}])
        # 段落里没有未成年人的部分不被牵连（SEG2 20-30 分无素材登记 → NOT_LICENSED，
        # 但绝不是 MINOR_ON_MIC）；SEG1 完全不受影响
        v1 = segment_verdict(st, st.segments[SEG1], C.USE_REPLAY, at=at)
        self.assertNotIn(C.REASON_MINOR_ON_MIC, v1["blockers"])
        self.assertEqual(windows_len(v1["allowed"]), 20 * MIN)

    def test_minor_released_after_guardian_confirm(self) -> None:
        store = EventStore()
        L = RightsLedger(store)
        build_basic(L)
        self._link_minor(L)
        L.confirm_consent("p-minor", "2026-10-04T09:00:00+08:00")
        st = fold(store)
        v = segment_verdict(st, st.segments[SEG2], C.USE_REPLAY,
                            at=parse_dt("2026-10-04T12:00:00+08:00"))
        self.assertNotIn(C.REASON_MINOR_ON_MIC, v["blockers"])
        self.assertEqual(windows_len(v["allowed"]), 10 * MIN)

    def test_bgm_uncertain_held_then_verified_and_granted(self) -> None:
        store = EventStore()
        L = RightsLedger(store)
        build_basic(L)
        L.link_material(M_BGM, material_type=C.MATERIAL_ACCOMPANIMENT, segment_id=SEG1,
                        windows=[{"start_ms": 10 * MIN, "end_ms": 20 * MIN}],
                        at="2026-10-01T19:11:00+08:00",
                        certainty=C.CERTAINTY_UNCERTAIN)
        st = fold(store)
        at = parse_dt("2026-10-02T12:00:00+08:00")
        v = segment_verdict(st, st.segments[SEG1], C.USE_REPLAY, at=at)
        self.assertIn(C.REASON_BGM_UNVERIFIED, v["blockers"])
        # 0-10 分示范仍可用：只限制 BGM 对应时段
        self.assertEqual(v["allowed"], [{"start_ms": 0, "end_ms": 10 * MIN}])
        # 核实 + 补授权后解除
        L.verify_material(M_BGM, "2026-10-04T10:00:00+08:00")
        L.grant_permission("p-bgm", material_id=M_BGM, right_holder_id=MUS,
                           uses=list(C.USES), territories=["CN"],
                           valid_from="2026-10-04T10:30:00+08:00", valid_until=None,
                           at="2026-10-04T10:30:00+08:00")
        st2 = fold(store)
        v2 = segment_verdict(st2, st2.segments[SEG1], C.USE_REPLAY,
                             at=parse_dt("2026-10-04T12:00:00+08:00"))
        self.assertNotIn(C.REASON_BGM_UNVERIFIED, v2["blockers"])
        self.assertEqual(windows_len(v2["allowed"]), 20 * MIN)

    def test_expired_rights_restrict_window_and_renewal_restores(self) -> None:
        store = EventStore()
        L = RightsLedger(store)
        build_basic(L)
        # 原无期限许可被撤回，改签 10-11 到期的新许可（权利重谈的真实路径）
        L.withdraw_consent("p-demo", "2026-10-05T09:00:00+08:00", reason="合同重谈")
        L.grant_permission("p-demo2", material_id=M_DEMO, right_holder_id=INH,
                           uses=list(C.USES), territories=["CN"],
                           valid_from="2026-10-05T00:00:00+08:00",
                           valid_until="2026-10-11T00:00:00+08:00",
                           at="2026-10-05T10:00:00+08:00")
        st_exp = fold(store)
        expired = segment_verdict(st_exp, st_exp.segments[SEG1], C.USE_REPLAY,
                                  at=parse_dt("2026-10-12T00:00:00+08:00"))
        self.assertIn(C.REASON_RIGHTS_EXPIRED, expired["blockers"])
        self.assertEqual(windows_len(expired["allowed"]), 0)
        # 续约后恢复
        L.grant_permission("p-demo-renew", material_id=M_DEMO, right_holder_id=INH,
                           uses=list(C.USES), territories=["CN"],
                           valid_from="2026-10-15T00:00:00+08:00", valid_until=None,
                           at="2026-10-15T09:00:00+08:00")
        st_ok = fold(store)
        renewed = segment_verdict(st_ok, st_ok.segments[SEG1], C.USE_REPLAY,
                                  at=parse_dt("2026-10-16T00:00:00+08:00"))
        self.assertNotIn(C.REASON_RIGHTS_EXPIRED, renewed["blockers"])
        self.assertEqual(windows_len(renewed["allowed"]), 20 * MIN)


class SettlementTest(unittest.TestCase):
    def test_settlement_uses_contract_effective_at_order_time(self) -> None:
        store = EventStore()
        L = RightsLedger(store)
        build_basic(L)
        # 10-01 打赏 100 元，按 v1 合同（50%/10%/平台40%）
        L.place_tip("tip1", session_id=SESSION, segment_id=SEG1, participant_id=V1,
                    amount=10_000, at="2026-10-01T19:30:00+08:00")
        # 10-20 换合同：传承人 70%，平台 30%（乐手不在新合同内）
        L.execute_contract("c2", SESSION, "2026-10-20T10:00:00+08:00",
                           revenue_share={INH: 0.7}, platform_rate=0.3,
                           valid_from="2026-10-20T10:00:00+08:00")
        L.place_tip("tip2", session_id=SESSION, segment_id=SEG1, participant_id=V1,
                    amount=10_000, at="2026-10-21T19:30:00+08:00")
        st = fold(store)

        def pays(tip_id: str) -> dict:
            return {e["payee_id"]: e["amount"] for e in st.revenue
                    if e["tip_id"] == tip_id and e["entry_kind"] == C.ENTRY_INITIAL}

        self.assertEqual(pays("tip1"), {INH: 5000, MUS: 1000, C.ROLE_PLATFORM: 4000})
        self.assertEqual(pays("tip2"), {INH: 7000, C.ROLE_PLATFORM: 3000})
        # 旧订单的结算依据永远是 c1，不被新合同改写
        old = [e for e in st.revenue if e["tip_id"] == "tip1"]
        self.assertTrue(all(e["contract_id"] == "c1" for e in old))

    def test_refund_creates_reversal_without_deleting_history(self) -> None:
        store = EventStore()
        L = RightsLedger(store)
        build_basic(L)
        L.place_tip("tip1", session_id=SESSION, segment_id=SEG1, participant_id=V1,
                    amount=10_000, at="2026-10-01T19:30:00+08:00")
        L.refund_tip("tip1", "2026-10-02T18:00:00+08:00", reason="退款")
        st = fold(store)
        initial = [e for e in st.revenue if e["tip_id"] == "tip1"
                   and e["entry_kind"] == C.ENTRY_INITIAL]
        reversal = [e for e in st.revenue if e["tip_id"] == "tip1"
                    and e["entry_kind"] == C.ENTRY_REVERSAL]
        self.assertEqual(len(initial), 3)
        self.assertEqual(len(reversal), 3)
        self.assertEqual(sum(e["amount"] for e in initial + reversal), 0)
        self.assertEqual(st.tips["tip1"]["status"], "REFUNDED")
        with self.assertRaises(RightsLedgerError):
            L.refund_tip("tip1", "2026-10-03T18:00:00+08:00")

    def test_rights_adjustment_topup_is_appended(self) -> None:
        store = EventStore()
        L = RightsLedger(store)
        build_basic(L)
        L.place_tip("tip1", session_id=SESSION, segment_id=SEG1, participant_id=V1,
                    amount=10_000, at="2026-10-01T19:30:00+08:00")
        L.adjust_rights("adj1", session_id=SESSION, at="2026-10-04T11:00:00+08:00",
                        kind=C.ADJ_TOP_UP, tip_id="tip1",
                        allocations=[{"payee_id": MUS, "amount": 300}], reason="确权补付")
        st = fold(store)
        mus_total = sum(e["amount"] for e in st.revenue if e["payee_id"] == MUS)
        self.assertEqual(mus_total, 1000 + 300)  # 原 10% 分录原样保留

    def test_settlement_without_contract_refused(self) -> None:
        store = EventStore()
        L = RightsLedger(store)
        # 只有场次和段落，没有合同
        L.schedule_session(SESSION, "无合同直播", "2026-10-01T19:00:00+08:00")
        L.register_segment(SEG1, SESSION, "段", [{"start_ms": 0, "end_ms": 10 * MIN}],
                           "2026-10-01T19:00:00+08:00")
        with self.assertRaises(RightsLedgerError):
            L.place_tip("tipx", session_id=SESSION, segment_id=SEG1, participant_id=V1,
                        amount=1000, at="2026-10-01T19:30:00+08:00")


class ReleaseGateTest(unittest.TestCase):
    def _evening_state(self, L: RightsLedger) -> None:
        build_basic(L)
        # 成年观众连麦（待确认）+ 未成年人连麦
        L.link_material(M_LINK, material_type=C.MATERIAL_VOICE_LINK, segment_id=SEG2,
                        windows=[{"start_ms": 20 * MIN, "end_ms": 30 * MIN}],
                        at="2026-10-01T19:21:00+08:00", participant_id=V1)
        L.capture_verbal_consent("p-link", material_id=M_LINK, right_holder_id=V1,
                                 uses=list(C.USES), at="2026-10-01T19:21:30+08:00")
        L.link_material(M_MINOR, material_type=C.MATERIAL_VOICE_LINK, segment_id=SEG2,
                        windows=[{"start_ms": 30 * MIN, "end_ms": 40 * MIN}],
                        at="2026-10-01T19:31:00+08:00", participant_id=VMIN, is_minor=True)
        L.capture_verbal_consent("p-minor", material_id=M_MINOR, right_holder_id=VMIN,
                                 uses=list(C.USES), at="2026-10-01T19:31:30+08:00")
        L.end_session(SESSION, "2026-10-01T20:00:00+08:00")

    def test_replay_publish_partial_but_cut_requires_full_clearance(self) -> None:
        store = EventStore()
        L = RightsLedger(store)
        self._evening_state(L)
        sources = [
            {"segment_id": SEG1, "time_windows": [{"start_ms": 0, "end_ms": 20 * MIN}]},
            {"segment_id": SEG2, "time_windows": [{"start_ms": 20 * MIN, "end_ms": 40 * MIN}]},
        ]
        # 回放允许“部分受限 + 播放端按时段屏蔽”
        L.publish_replay("r1", session_id=SESSION, source_segments=sources,
                         at="2026-10-02T12:00:00+08:00", version_no=1)
        self.assertEqual(fold(store).replays["r1"]["status"], "PUBLISHED")

        # 商业剪辑含未成年人段 → 必须整段获准，驳回并留 CUT_REJECTED 事件
        L.propose_cut("cut1", session_id=SESSION, title="连麦切片",
                      at="2026-10-03T09:00:00+08:00", source_segments=[sources[1]])
        with self.assertRaises(RightsLedgerError):
            L.publish_cut("cut1", "2026-10-03T09:05:00+08:00")
        self.assertEqual(fold(store).cuts["cut1"]["status"], "REJECTED")

        # 补签后重新送审通过
        L.confirm_consent("p-link", "2026-10-04T08:00:00+08:00")
        L.confirm_consent("p-minor", "2026-10-04T09:00:00+08:00")
        L.propose_cut("cut2", session_id=SESSION, title="连麦切片定稿",
                      at="2026-10-04T10:00:00+08:00", source_segments=[sources[1]])
        L.publish_cut("cut2", "2026-10-04T10:05:00+08:00")
        self.assertEqual(fold(store).cuts["cut2"]["status"], "PUBLISHED")

    def test_takedown_recorded_as_event(self) -> None:
        store = EventStore()
        L = RightsLedger(store)
        build_basic(L)
        L.end_session(SESSION, "2026-10-01T20:00:00+08:00")
        L.publish_replay("r1", session_id=SESSION, source_segments=[
            {"segment_id": SEG1, "time_windows": [{"start_ms": 0, "end_ms": 20 * MIN}]}],
            at="2026-10-02T12:00:00+08:00")
        L.take_down_replay("r1", "2026-10-05T12:00:00+08:00", reason="权利人投诉")
        self.assertEqual(fold(store).replays["r1"]["status"], "TAKEN_DOWN")
        page = viewer_replay(store, "r1", at=parse_dt("2026-10-06T12:00:00+08:00"))
        self.assertFalse(page["available"])


class ViewTest(unittest.TestCase):
    def test_three_views_consistent_and_minor_reason_neutralized(self) -> None:
        store = EventStore()
        L = RightsLedger(store)
        build_basic(L)
        L.link_material(M_MINOR, material_type=C.MATERIAL_VOICE_LINK, segment_id=SEG2,
                        windows=[{"start_ms": 30 * MIN, "end_ms": 40 * MIN}],
                        at="2026-10-01T19:31:00+08:00", participant_id=VMIN, is_minor=True)
        L.capture_verbal_consent("p-minor", material_id=M_MINOR, right_holder_id=VMIN,
                                 uses=list(C.USES), at="2026-10-01T19:31:30+08:00")
        L.place_tip("tip1", session_id=SESSION, segment_id=SEG1, participant_id=V1,
                    amount=10_000, at="2026-10-01T19:40:00+08:00")
        L.end_session(SESSION, "2026-10-01T20:00:00+08:00")
        L.publish_replay("r1", session_id=SESSION, source_segments=[
            {"segment_id": SEG1, "time_windows": [{"start_ms": 0, "end_ms": 20 * MIN}]},
            {"segment_id": SEG2, "time_windows": [{"start_ms": 20 * MIN, "end_ms": 40 * MIN}]},
        ], at="2026-10-02T12:00:00+08:00")

        at = parse_dt("2026-10-02T13:00:00+08:00")
        ed = editor_workspace(store, SESSION, at=at)
        seg2 = next(s for s in ed["segments"] if s["segment_id"] == SEG2)
        self.assertTrue(any(r["reason_code"] == C.REASON_MINOR_ON_MIC
                            for r in seg2["uses"][C.USE_REPLAY]["restrictions"]))
        # 编辑端看到完整原因
        self.assertIn("未成年", seg2["uses"][C.USE_REPLAY]["restrictions"][0]["reason"])

        page = viewer_replay(store, "r1", at=at)
        seg2_view = next(t for t in page["timeline"] if t["segment_id"] == SEG2)
        messages = {r["message"] for r in seg2_view["reasons"]}
        # 观众端不出现“未成年”字样，只有中性提示
        self.assertTrue(any("权益保护" in m for m in messages))
        self.assertFalse(any("未成年" in m for m in messages))
        # 可播区间不包含受限 10 分钟
        self.assertEqual(seg2_view["playable_windows"], [])

        # 权利人对账单：传承人能看到被使用与 50 元分成
        stmt = right_holder_statement(store, INH, at=at)
        self.assertEqual(stmt["revenue_total_fen"], 5000)
        self.assertTrue(any(u["release_id"] == "r1" for u in stmt["used_in"]))


if __name__ == "__main__":
    unittest.main()
