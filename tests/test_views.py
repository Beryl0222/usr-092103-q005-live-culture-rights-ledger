import unittest

from src import contracts as C
from src.app import LedgerService
from src.event_store import parse_ts
from src.views import AudienceView, EditorView, HolderView, PublishingService

PUBLISH_AT = "2026-10-03T14:00:00+08:00"


def setup() -> LedgerService:
    s = LedgerService()
    s.schedule_session("live-1", "测试非遗直播", "2026-09-20T19:30:00+08:00",
                       occurred_at="2026-09-19T10:00:00+08:00")
    s.start_session("live-1", "2026-09-20T19:30:00+08:00")
    s.register_segment("seg-1", "live-1", 1, "示范段", 0, 600_000,
                       occurred_at="2026-09-20T19:30:00+08:00")
    s.register_participant("p-master", "周师傅", "inheritor")
    s.register_participant("p-music", "林琴师", "accompanist")
    s.cast_participant("seg-1", [
        {"participant_id": "p-master"},
        {"participant_id": "p-music", "time_ranges": [[0, 300_000]]},
    ])
    # 传承人三用途全球；伴奏师只有直播+回放
    s.capture_consent("perm-master", "participant", "p-master",
                      channel=C.CHANNEL_WRITTEN,
                      uses=[C.USE_LIVE, C.USE_REPLAY, C.USE_CLIP],
                      territories=[C.WORLDWIDE])
    s.capture_consent("perm-music", "participant", "p-music",
                      channel=C.CHANNEL_WRITTEN,
                      uses=[C.USE_LIVE, C.USE_REPLAY],
                      territories=[C.WORLDWIDE])
    s.end_session("live-1", "2026-09-20T20:30:00+08:00")
    return s


class PublishingGateTest(unittest.TestCase):
    def test_editor_matrix_shows_use_level_status(self):
        s = setup()
        card = EditorView(s.ledger).segment_card("seg-1")
        self.assertEqual(card["uses"][C.USE_LIVE]["status"], "available")
        self.assertEqual(card["uses"][C.USE_REPLAY]["status"], "available")
        # 伴奏师只在前 300 秒出场且无剪辑权 → 剪辑列局部可用而非整段可用
        clip = card["uses"][C.USE_CLIP]
        self.assertEqual(clip["status"], "partial")
        self.assertEqual(clip["playable"], [[300_000, 600_000]])
        self.assertEqual(clip["blockers"][0]["reason"], C.REASON_USE_NOT_LICENSED)

    def test_clip_within_clear_window_publishes(self):
        s = setup()
        pub = PublishingService(s.store, s.ledger)
        # 300s-360s 只有传承人出镜：可商业剪辑
        ev = pub.propose_clip("clip-ok", "传承人特写",
                              [{"segment_id": "seg-1", "start_offset_ms": 300_000,
                                "end_offset_ms": 360_000}],
                              at=PUBLISH_AT)
        self.assertEqual(ev["payload"]["ranges"][0]["blockers"], [])
        published = pub.publish_clip("clip-ok", at=PUBLISH_AT)
        self.assertEqual(published["event_type"], "CLIP_PUBLISHED")

    def test_clip_crossing_unlicensed_window_is_blocked(self):
        s = setup()
        pub = PublishingService(s.store, s.ledger)
        ev = pub.propose_clip("clip-bad", "带伴奏集锦",
                              [{"segment_id": "seg-1", "start_offset_ms": 240_000,
                                "end_offset_ms": 360_000}],
                              at=PUBLISH_AT)
        blockers = ev["payload"]["ranges"][0]["blockers"]
        self.assertTrue(any(b["reason"] == C.REASON_USE_NOT_LICENSED for b in blockers))
        with self.assertRaises(C.ContractError):
            pub.publish_clip("clip-bad", at=PUBLISH_AT)
        rejected = pub.reject_clip("clip-bad", "交集不含商业剪辑权", at=PUBLISH_AT)
        self.assertEqual(rejected["event_type"], "CLIP_REJECTED")

    def test_replay_masks_windows_instead_of_taking_down_session(self):
        s = setup()
        # 伴奏出现窗口限制回放（模拟节目后核对）
        s.apply_restriction("r1", "seg-1", 0, 300_000, C.REASON_PENDING_CONSENT,
                            uses=[C.USE_REPLAY], occurred_at=PUBLISH_AT)
        pub = PublishingService(s.store, s.ledger)
        ev = pub.publish_replay("live-1", label="回放（遮罩版）", at=PUBLISH_AT)
        seg = ev["payload"]["segments"][0]
        self.assertEqual(seg["status"], "partial")
        self.assertEqual(seg["playable"], [[300_000, 600_000]])
        self.assertEqual(seg["masked"][0]["reason"], C.REASON_PENDING_CONSENT)

    def test_replay_cannot_publish_when_nothing_playable(self):
        s = setup()
        s.apply_restriction("r-all", "seg-1", 0, 600_000, C.REASON_TAKEDOWN,
                            uses=[C.USE_REPLAY], occurred_at=PUBLISH_AT)
        pub = PublishingService(s.store, s.ledger)
        with self.assertRaises(C.ContractError):
            pub.publish_replay("live-1", label="x", at=PUBLISH_AT)


class AudienceViewTest(unittest.TestCase):
    def test_feed_only_contains_published_and_territory_allowed(self):
        s = setup()
        pub = PublishingService(s.store, s.ledger)
        pub.propose_clip("clip-ok", "传承人特写",
                         [{"segment_id": "seg-1", "start_offset_ms": 300_000,
                           "end_offset_ms": 360_000}],
                        territories=["CN"], at=PUBLISH_AT)
        pub.publish_clip("clip-ok", at=PUBLISH_AT)
        cn = AudienceView(s.ledger, territory="CN")
        us = AudienceView(s.ledger, territory="US")
        self.assertEqual([c["clip_id"] for c in cn.clip_feed()], ["clip-ok"])
        self.assertEqual(us.clip_feed(), [])

    def test_feed_shows_reason_copy_for_masked_window(self):
        s = setup()
        s.apply_restriction("r1", "seg-1", 0, 300_000, C.REASON_MUSIC_UNKNOWN,
                            uses=[C.USE_REPLAY], occurred_at=PUBLISH_AT)
        PublishingService(s.store, s.ledger).publish_replay(
            "live-1", label="回放", at=PUBLISH_AT)
        feed = AudienceView(s.ledger).replay_feed("live-1")
        self.assertEqual(feed["status"], "available")
        note = feed["segments"][0]["unavailable_notes"][0]
        self.assertIn("识别确认", note["message"])

    def test_takedown_shows_unavailable_feed(self):
        s = setup()
        pub = PublishingService(s.store, s.ledger)
        pub.publish_replay("live-1", label="回放", at=PUBLISH_AT)
        pub.takedown_replay("live-1", reason=C.REASON_TAKEDOWN, at=PUBLISH_AT)
        feed = AudienceView(s.ledger).replay_feed("live-1")
        self.assertEqual(feed["status"], "unavailable")


class HolderViewTest(unittest.TestCase):
    def test_usage_report_tracks_segments_permissions_restrictions(self):
        s = setup()
        s.apply_restriction("r1", "seg-1", 0, 300_000, C.REASON_PENDING_CONSENT,
                            uses=[C.USE_REPLAY], occurred_at=PUBLISH_AT)
        report = HolderView(s.ledger).usage_report(participant_id="p-music")
        self.assertEqual(report["segments"], ["seg-1"])
        self.assertEqual(report["permissions"][0]["status"], C.CONSENT_GRANTED)
        self.assertEqual(report["restrictions"][0]["reason"], C.REASON_PENDING_CONSENT)


if __name__ == "__main__":
    unittest.main()
