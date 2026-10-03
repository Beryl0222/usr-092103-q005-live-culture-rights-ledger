import unittest
from datetime import datetime

from src import contracts as C
from src.app import LedgerService
from src.event_store import parse_ts

AT = "2026-09-21T12:00:00+08:00"
SEG = "seg-1"
SEG2 = "seg-2"


def base_service() -> LedgerService:
    s = LedgerService()
    s.schedule_session("live-1", "测试直播", "2026-09-20T19:30:00+08:00",
                       occurred_at="2026-09-19T10:00:00+08:00")
    s.start_session("live-1", "2026-09-20T19:30:00+08:00")
    s.register_segment(SEG, "live-1", 1, "段落一", 0, 600_000,
                       occurred_at="2026-09-20T19:30:00+08:00")
    return s


class PermissionIntersectionTest(unittest.TestCase):
    def test_combined_sources_use_intersection_of_uses(self):
        s = base_service()
        s.register_participant("pa", "甲", "inheritor")
        s.register_participant("pb", "乙", "accompanist")
        s.cast_participant(SEG, [{"participant_id": "pa"}, {"participant_id": "pb"}])
        # 甲三用途齐全；乙只有直播+回放
        s.capture_consent("perm-a", "participant", "pa", channel=C.CHANNEL_WRITTEN,
                          uses=[C.USE_LIVE, C.USE_REPLAY, C.USE_CLIP])
        s.capture_consent("perm-b", "participant", "pb", channel=C.CHANNEL_WRITTEN,
                          uses=[C.USE_LIVE, C.USE_REPLAY])
        led = s.ledger
        self.assertEqual(led.segment_availability(SEG, C.USE_LIVE, at=parse_ts(AT)).status,
                         "available")
        self.assertEqual(led.segment_availability(SEG, C.USE_REPLAY, at=parse_ts(AT)).status,
                         "available")
        clip = led.segment_availability(SEG, C.USE_CLIP, at=parse_ts(AT))
        self.assertEqual(clip.status, "blocked")
        self.assertEqual(clip.blockers[0].reason, C.REASON_USE_NOT_LICENSED)

    def test_territory_intersection(self):
        s = base_service()
        s.register_participant("pa", "甲", "inheritor")
        s.cast_participant(SEG, [{"participant_id": "pa"}])
        s.capture_consent("perm-a", "participant", "pa", channel=C.CHANNEL_WRITTEN,
                          uses=[C.USE_REPLAY], territories=["CN", "HK"])
        cn = s.ledger.segment_availability(SEG, C.USE_REPLAY, territories=["CN"],
                                           at=parse_ts(AT))
        self.assertEqual(cn.status, "available")
        us = s.ledger.segment_availability(SEG, C.USE_REPLAY, territories=["US"],
                                           at=parse_ts(AT))
        self.assertEqual(us.status, "blocked")
        self.assertEqual(us.blockers[0].reason, C.REASON_TERRITORY)

    def test_partial_time_range_takes_intersection_not_whole_segment(self):
        s = base_service()
        s.register_participant("pa", "甲", "guest")
        s.cast_participant(SEG, [{"participant_id": "pa",
                                  "time_ranges": [[120_000, 300_000]]}])
        # 许可只覆盖其中 [180s,240s]
        s.capture_consent("perm-a", "participant", "pa", channel=C.CHANNEL_WRITTEN,
                          uses=[C.USE_REPLAY],
                          time_ranges=[(180_000, 240_000)])
        av = s.ledger.segment_availability(SEG, C.USE_REPLAY, at=parse_ts(AT))
        self.assertEqual(av.status, "partial")
        # 人物只在 120s-300s 出现；许可只覆盖 180s-240s
        self.assertEqual(av.playable, [(0, 120_000), (180_000, 240_000), (300_000, 600_000)])

    def test_oral_consent_is_pending_and_blocks_replay_but_live_accepts(self):
        s = base_service()
        s.register_participant("pf", "连麦观众", "mic_audience")
        s.cast_participant(SEG, [{"participant_id": "pf"}])
        s.capture_consent("perm-f", "participant", "pf", channel=C.CHANNEL_ORAL,
                          uses=[C.USE_LIVE, C.USE_REPLAY, C.USE_CLIP],
                          occurred_at="2026-09-20T20:00:00+08:00")
        pending = s.ledger.pending_permissions()
        self.assertEqual([p.id for p in pending], ["perm-f"])
        replay = s.ledger.segment_availability(SEG, C.USE_REPLAY, at=parse_ts(AT))
        self.assertEqual(replay.status, "blocked")
        self.assertEqual(replay.blockers[0].reason, C.REASON_PENDING_CONSENT)
        live = s.ledger.segment_availability(SEG, C.USE_LIVE, at=parse_ts(AT))
        self.assertEqual(live.status, "available")
        # 书面确认后回放放行
        s.confirm_consent("perm-f", uses=[C.USE_LIVE, C.USE_REPLAY, C.USE_CLIP],
                          occurred_at="2026-09-21T15:00:00+08:00")
        self.assertEqual(
            s.ledger.segment_availability(SEG, C.USE_REPLAY, at=parse_ts(AT)).status,
            "available")

    def test_minor_blocks_only_mic_window_until_guardian_consent(self):
        s = base_service()
        s.register_participant("pm", "小学徒", "mic_audience", is_minor=True)
        s.cast_participant(SEG, [{"participant_id": "pm",
                                  "time_ranges": [[300_000, 420_000]]}])
        av = s.ledger.segment_availability(SEG, C.USE_REPLAY, at=parse_ts(AT))
        self.assertEqual(av.status, "partial")
        self.assertEqual(av.playable, [(0, 300_000), (420_000, 600_000)])
        self.assertEqual(av.blockers[0].reason, C.REASON_MINOR)
        # 监护人同意覆盖该窗口后放行
        s.capture_consent("perm-guard", "participant", "pm", channel=C.CHANNEL_GUARDIAN,
                          uses=[C.USE_REPLAY], time_ranges=[(300_000, 420_000)],
                          occurred_at=AT)
        av2 = s.ledger.segment_availability(SEG, C.USE_REPLAY, at=parse_ts(AT))
        self.assertEqual(av2.status, "available")

    def test_uncertain_music_blocks_until_verified_and_licensed(self):
        s = base_service()
        s.register_participant("pa", "甲", "inheritor")
        s.cast_participant(SEG, [{"participant_id": "pa"}])
        s.capture_consent("perm-a", "participant", "pa", channel=C.CHANNEL_WRITTEN,
                          uses=[C.USE_LIVE, C.USE_REPLAY])
        s.register_material("m1", "未知BGM", "background_music",
                            music_detection="uncertain",
                            occurred_at="2026-09-20T20:00:00+08:00")
        s.link_material("m1", [{"segment_id": SEG, "start_offset_ms": 60_000,
                                "end_offset_ms": 120_000}],
                        occurred_at="2026-09-20T20:01:00+08:00")
        av = s.ledger.segment_availability(SEG, C.USE_REPLAY, at=parse_ts(AT))
        self.assertEqual(av.playable, [(0, 60_000), (120_000, 600_000)])
        self.assertEqual(av.blockers[0].reason, C.REASON_MUSIC_UNKNOWN)
        # 识别确认但尚无许可 → 变成未许可素材
        s.verify_material("m1", music_detection="confirmed", right_holder="曲库X",
                          occurred_at="2026-09-21T13:00:00+08:00")
        av2 = s.ledger.segment_availability(SEG, C.USE_REPLAY, at=parse_ts(AT))
        self.assertEqual(av2.blockers[0].reason, C.REASON_UNLICENSED_MATERIAL)
        # 补授权后放行
        s.capture_consent("perm-m1", "material", "m1", channel=C.CHANNEL_PLATFORM,
                          uses=[C.USE_REPLAY], time_ranges=[(60_000, 120_000)],
                          occurred_at="2026-09-21T14:00:00+08:00")
        self.assertEqual(
            s.ledger.segment_availability(SEG, C.USE_REPLAY,
                                          at=parse_ts("2026-09-21T15:00:00+08:00")).status,
            "available")

    def test_expired_permission_blocks_at_publish_time(self):
        s = base_service()
        s.register_participant("pa", "甲", "inheritor")
        s.cast_participant(SEG, [{"participant_id": "pa"}])
        s.capture_consent("perm-a", "participant", "pa", channel=C.CHANNEL_WRITTEN,
                          uses=[C.USE_REPLAY],
                          valid_from="2026-01-01T00:00:00+08:00",
                          expires_at="2026-09-30T00:00:00+08:00")
        before = s.ledger.segment_availability(
            SEG, C.USE_REPLAY, at=parse_ts("2026-09-25T00:00:00+08:00"))
        self.assertEqual(before.status, "available")
        after = s.ledger.segment_availability(
            SEG, C.USE_REPLAY, at=parse_ts("2026-10-01T00:00:00+08:00"))
        self.assertEqual(after.status, "blocked")
        self.assertEqual(after.blockers[0].reason, C.REASON_RIGHTS_EXPIRED)

    def test_manual_restriction_lifecycle(self):
        s = base_service()
        s.apply_restriction("r1", SEG, 0, 60_000, C.REASON_TAKEDOWN,
                            uses=[C.USE_REPLAY], occurred_at=AT)
        av = s.ledger.segment_availability(SEG, C.USE_REPLAY, at=parse_ts(AT))
        self.assertEqual(av.playable, [(60_000, 600_000)])
        # 直播用途不受“仅回放”限制影响
        self.assertEqual(
            s.ledger.segment_availability(SEG, C.USE_LIVE, at=parse_ts(AT)).status,
            "available")
        s.lift_restriction("r1", occurred_at=AT)
        self.assertEqual(
            s.ledger.segment_availability(SEG, C.USE_REPLAY, at=parse_ts(AT)).status,
            "available")


if __name__ == "__main__":
    unittest.main()
