"""非遗直播全链路联调样例（生成 data/sample_events.jsonl）。

故事线：
- 09-20 一场苏绣直播：传承人示范、伴奏曲目、成年观众连麦、未成年人连麦、平台切片；
- 背景音乐识别一度“不确定”，第二天确认；未成年连麦的监护人书面同意第三天补齐；
- 两段打赏：一笔正常按当时合同分配；一笔先分配后退款（整笔冲正）；
- 后来传承人合同改版，对一笔历史打赏做补付；旧合同误分给出镜观众的一笔做冲正；
- 发布回放：问题窗口逐段遮罩而非整场下架；商业剪辑必须全程落在权限交集内。
"""

from __future__ import annotations

from pathlib import Path

from src import contracts as C
from src.app import LedgerService
from src.event_store import EventStore
from src.settlement import SettlementService
from src.views import AudienceView, EditorView, HolderView, PublishingService

SESSION = "live-2026-0920-suxiu"
SEG_OPEN = "seg-01-opening"
SEG_DEMO = "seg-02-demo"
SEG_MIC = "seg-03-audience-mic"
SEG_FINALE = "seg-04-finale"


def build() -> tuple[EventStore, dict]:
    svc = LedgerService()
    s = svc

    # ---- 9-01：先签当时有效合同（平台 30%、机构 70%；传承人固定比例 20%）----
    s.agree_contract("platform", rate_bps=3000, effective_from="2026-09-01T00:00:00+08:00",
                     note="平台抽成 30%")
    s.agree_contract("house", rate_bps=7000, effective_from="2026-09-01T00:00:00+08:00",
                     note="内容机构基础分成 70%（含兜底余量）")
    s.agree_contract("participant:p-suxiu-master", rate_bps=2000,
                     effective_from="2026-09-01T00:00:00+08:00",
                     note="传承人劳务分成 20%（从池内列支）")
    s.agree_contract("participant:p-accompanist", rate_bps=500,
                     effective_from="2026-09-01T00:00:00+08:00",
                     note="伴奏师分成 5%")

    # ---- 9-20：直播当天 ----
    s.schedule_session(SESSION, "中秋夜·苏绣非遗直播",
                       "2026-09-20T19:30:00+08:00", occurred_at="2026-09-15T10:00:00+08:00")
    s.start_session(SESSION, "2026-09-20T19:30:00+08:00")

    # 四个节目段落（毫秒偏移）
    s.register_segment(SEG_OPEN, SESSION, 1, "开场与传承人介绍", 0, 10 * 60_000,
                       occurred_at="2026-09-20T19:30:00+08:00")
    s.register_segment(SEG_DEMO, SESSION, 2, "双面绣技法示范", 10 * 60_000, 40 * 60_000,
                       occurred_at="2026-09-20T19:40:00+08:00")
    s.register_segment(SEG_MIC, SESSION, 3, "观众连麦互动", 40 * 60_000, 55 * 60_000,
                       occurred_at="2026-09-20T20:10:00+08:00")
    s.register_segment(SEG_FINALE, SESSION, 4, "收尾与作品展示", 55 * 60_000, 65 * 60_000,
                       occurred_at="2026-09-20T20:25:00+08:00")

    # 参与者
    s.register_participant("p-suxiu-master", "周师傅", "inheritor")
    s.register_participant("p-accompanist", "林琴师", "accompanist")
    s.register_participant("p-fan-adult", "网友“绣线猫”", "mic_audience")
    s.register_participant("p-fan-minor", "连麦观众“小学徒”", "mic_audience", is_minor=True)

    # 出场时间：传承人全程；伴奏师出现在开场/示范/收尾
    s.cast_participant(SEG_OPEN, [
        {"participant_id": "p-suxiu-master", "role": "inheritor"},
        {"participant_id": "p-accompanist", "role": "accompanist"},
    ])
    s.cast_participant(SEG_DEMO, [
        {"participant_id": "p-suxiu-master", "role": "inheritor"},
        {"participant_id": "p-accompanist", "role": "accompanist",
         "time_ranges": [[10 * 60_000, 25 * 60_000]]},
    ])
    s.cast_participant(SEG_MIC, [
        {"participant_id": "p-suxiu-master", "role": "inheritor",
         "time_ranges": [[40 * 60_000, 46 * 60_000]]},
        {"participant_id": "p-fan-adult", "role": "mic_audience",
         "time_ranges": [[43 * 60_000, 48 * 60_000]]},
        {"participant_id": "p-fan-minor", "role": "mic_audience",
         "time_ranges": [[49 * 60_000, 52 * 60_000]]},
    ])
    s.cast_participant(SEG_FINALE, [
        {"participant_id": "p-suxiu-master", "role": "inheritor"},
        {"participant_id": "p-accompanist", "role": "accompanist"},
    ])

    # 素材来源：传承人示范画面（书面，三用途齐全，一年期）
    s.capture_consent(
        "perm-master", "participant", "p-suxiu-master", channel=C.CHANNEL_WRITTEN,
        uses=[C.USE_LIVE, C.USE_REPLAY, C.USE_CLIP], territories=[C.WORLDWIDE],
        valid_from="2026-09-01T00:00:00+08:00", expires_at="2027-09-01T00:00:00+08:00",
        note="传承人肖像与示范作品使用许可")
    # 伴奏师：书面，但只授直播+回放，不含商业剪辑
    s.capture_consent(
        "perm-accompanist", "participant", "p-accompanist", channel=C.CHANNEL_WRITTEN,
        uses=[C.USE_LIVE, C.USE_REPLAY], territories=[C.WORLDWIDE],
        valid_from="2026-09-01T00:00:00+08:00", expires_at="2027-09-01T00:00:00+08:00")
    # 成年连麦观众：直播现场口头同意 → 只能待确认
    s.capture_consent(
        "perm-fan-adult-oral", "participant", "p-fan-adult", channel=C.CHANNEL_ORAL,
        uses=[C.USE_LIVE, C.USE_REPLAY, C.USE_CLIP],
        time_ranges=[(43 * 60_000, 48 * 60_000)],
        occurred_at="2026-09-20T20:14:00+08:00")

    # 背景音乐：登记时识别“不确定”，关联示范段前 5 分钟
    s.register_material("mat-bgm", "直播伴奏曲目 B-12（识别中）", "background_music",
                        music_detection="uncertain",
                        occurred_at="2026-09-20T19:35:00+08:00")
    s.link_material("mat-bgm", [
        {"segment_id": SEG_DEMO, "start_offset_ms": 12 * 60_000,
         "end_offset_ms": 17 * 60_000}],
        occurred_at="2026-09-20T19:41:00+08:00")

    # 平台切片素材（平台曲库许可，仅回放，不含商业剪辑）
    s.register_material("mat-platform-slice", "平台片头切片", "platform_overlay",
                        right_holder="platform",
                        occurred_at="2026-09-20T19:30:00+08:00")
    s.link_material("mat-platform-slice", [
        {"segment_id": SEG_OPEN, "start_offset_ms": 0, "end_offset_ms": 30_000}])
    s.capture_consent(
        "perm-platform-slice", "material", "mat-platform-slice",
        channel=C.CHANNEL_PLATFORM, uses=[C.USE_LIVE, C.USE_REPLAY],
        territories=[C.WORLDWIDE],
        valid_from="2026-01-01T00:00:00+08:00", expires_at="2027-01-01T00:00:00+08:00")

    # 观众贡献（含打赏分配依据）
    s.record_contributions("contrib-batch-1", SESSION, [
        {"participant_id": "p-suxiu-master", "kind": "cast", "segment_id": SEG_DEMO},
        {"participant_id": "p-accompanist", "kind": "cast", "segment_id": SEG_DEMO},
        {"participant_id": "p-fan-adult", "kind": "mic_audience", "segment_id": SEG_MIC},
        {"participant_id": "p-fan-minor", "kind": "mic_audience", "segment_id": SEG_MIC},
    ], occurred_at="2026-09-20T20:30:00+08:00")

    # ---- 打赏：示范段 100 元（正常分配）；连麦段 50 元（后来退款）----
    s.place_tip("tip-1001", SESSION, "fan-8848", 10_000,
                "2026-09-20T19:55:00+08:00", segment_id=SEG_DEMO)
    s.place_tip("tip-1002", SESSION, "fan-2203", 5_000,
                "2026-09-20T20:20:00+08:00", segment_id=SEG_MIC)

    settlement = SettlementService(svc.store, svc.ledger)
    settlement.allocate_tip("tip-1001", occurred_at="2026-09-20T20:31:00+08:00")
    settlement.allocate_tip("tip-1002", occurred_at="2026-09-20T20:31:00+08:00")

    s.end_session(SESSION, "2026-09-20T20:35:00+08:00")

    # ---- 9-21：背景音乐识别确认，取得曲库授权 ----
    s.verify_material("mat-bgm", music_detection="confirmed",
                      right_holder="曲库公司 X",
                      occurred_at="2026-09-21T11:00:00+08:00")
    s.capture_consent(
        "perm-bgm", "material", "mat-bgm", channel=C.CHANNEL_PLATFORM,
        uses=[C.USE_LIVE, C.USE_REPLAY], territories=[C.WORLDWIDE],
        valid_from="2026-09-20T00:00:00+08:00", expires_at="2027-09-20T00:00:00+08:00",
        time_ranges=[(12 * 60_000, 17 * 60_000)],
        note="曲库补授权，覆盖直播日", occurred_at="2026-09-21T11:05:00+08:00")

    # 成年连麦观众书面追认（口头→授予）
    s.confirm_consent(
        "perm-fan-adult-oral",
        uses=[C.USE_LIVE, C.USE_REPLAY, C.USE_CLIP],
        territories=[C.WORLDWIDE],
        valid_from="2026-09-20T00:00:00+08:00", expires_at="2027-09-20T00:00:00+08:00",
        time_ranges=[(43 * 60_000, 48 * 60_000)],
        occurred_at="2026-09-21T15:00:00+08:00")

    # 节目后核对：未成年人连麦窗口先限制回放/剪辑（不删整场），等监护人书面同意
    s.apply_restriction(
        "r-minor-mic", SEG_MIC, 49 * 60_000, 52 * 60_000, C.REASON_MINOR,
        uses=[C.USE_REPLAY, C.USE_CLIP],
        note="节目后核对发现未成年人连麦，监护人同意未齐",
        occurred_at="2026-09-21T16:00:00+08:00")

    # ---- 9-22：未成年人监护人书面同意补齐（回放/剪辑，不含其连麦之外时间）----
    s.capture_consent(
        "perm-fan-minor-guardian", "participant", "p-fan-minor",
        channel=C.CHANNEL_GUARDIAN,
        uses=[C.USE_LIVE, C.USE_REPLAY, C.USE_CLIP], territories=[C.WORLDWIDE],
        valid_from="2026-09-22T00:00:00+08:00", expires_at="2027-09-22T00:00:00+08:00",
        time_ranges=[(49 * 60_000, 52 * 60_000)],
        note="监护人书面同意，仅覆盖连麦窗口",
        occurred_at="2026-09-22T09:00:00+08:00")
    s.lift_restriction("r-minor-mic", note="监护人书面同意已补齐，解除限制",
                       occurred_at="2026-09-22T09:05:00+08:00")

    # ---- 9-23：tip-1002 观众申请退款 → 整笔冲正 ----
    settlement.refund_tip("tip-1002", reason="观众未成年消费申诉，平台核实退款",
                          occurred_at="2026-09-23T10:00:00+08:00")

    # ---- 10-01：传承人合同改版（35%），不追溯历史；对 tip-1001 补付差额确权 ----
    s.agree_contract("participant:p-suxiu-master", rate_bps=3500,
                     effective_from="2026-10-01T00:00:00+08:00",
                     supersedes="contract-participant-p-suxiu-master",
                     note="传承人合作升级 35%")
    settlement.supplement_payment(
        "tip-1001",
        [{"party": "participant:p-suxiu-master", "amount_fen": 1_500}],
        reason="传承人技法段落后续确权补付（机构补差）",
        occurred_at="2026-10-02T10:00:00+08:00")
    # 打赏落点为传承人独立示范（伴奏师出现窗口的结束边界），原按段落合同
    # 分给伴奏师的 5% 经核对不成立，冲正追回（机构收回）
    settlement.reverse_line(
        "tip-1001", "participant:p-accompanist", 350,
        reason="打赏落点在传承人独立示范时段，伴奏师未在场，原分配冲正",
        occurred_at="2026-10-02T10:05:00+08:00")

    # ---- 发布核对（10-03）：权限到期场景——示范段一张老素材许可 9-30 到期 ----
    s.capture_consent(
        "perm-old-archival", "material", "mat-old-archival",
        channel=C.CHANNEL_WRITTEN, uses=[C.USE_REPLAY], territories=["CN"],
        valid_from="2026-01-01T00:00:00+08:00", expires_at="2026-09-30T23:59:59+08:00",
        time_ranges=[(38 * 60_000, 39 * 60_000)],
        occurred_at="2026-09-18T00:00:00+08:00")
    s.register_material("mat-old-archival", "老影像资料（许可已到期）", "archival",
                        right_holder="某档案馆",
                        occurred_at="2026-09-18T00:00:00+08:00")
    s.link_material("mat-old-archival", [
        {"segment_id": SEG_DEMO, "start_offset_ms": 38 * 60_000,
         "end_offset_ms": 39 * 60_000}], occurred_at="2026-09-18T00:00:00+08:00")

    publishing = PublishingService(svc.store, svc.ledger)
    publish_at = "2026-10-03T14:00:00+08:00"

    # 节目后核对：权利人要求收尾最后 30 秒暂不发布（只限制回放/剪辑，不回溯直播）
    s.restrict_release(
        "r-finale-takedown", SEG_FINALE, 64 * 60_000 + 30_000, 65 * 60_000,
        C.REASON_TAKEDOWN, uses=[C.USE_REPLAY, C.USE_CLIP],
        note="权利人核对后要求收尾最后 30 秒暂不提供",
        occurred_at="2026-10-03T13:30:00+08:00")

    # 编辑发布前先看矩阵；商业剪辑送审：跨示范+收尾（避开伴奏无剪辑权、老素材到期等）
    good_clip = publishing.propose_clip(
        "clip-demo-hands", "传承人针法特写",
        [{"segment_id": SEG_DEMO, "start_offset_ms": 26 * 60_000,
          "end_offset_ms": 30 * 60_000}], territories=[C.WORLDWIDE], at=publish_at)
    if not any(r["blockers"] for r in good_clip["payload"]["ranges"]):
        publishing.publish_clip("clip-demo-hands", at=publish_at)

    # 一条会被闸门拦下的剪辑：包含伴奏师出现窗口（无商业剪辑授权）
    blocked_clip = publishing.propose_clip(
        "clip-accompanied", "带伴奏的开场集锦",
        [{"segment_id": SEG_OPEN, "start_offset_ms": 60_000,
          "end_offset_ms": 90_000}], territories=[C.WORLDWIDE], at=publish_at)
    if any(r["blockers"] for r in blocked_clip["payload"]["ranges"]):
        publishing.reject_clip(
            "clip-accompanied",
            "伴奏师与平台切片均未授商业剪辑权，按权限交集不可发布", at=publish_at)

    # 回放版本：问题窗口逐段遮罩，可播部分照常上线
    replay = publishing.publish_replay(
        SESSION, label="中秋夜·苏绣回放（完整版·遮罩处理）",
        territories=[C.WORLDWIDE], at=publish_at)

    return svc.store, {
        "replay_event": replay,
        "editor": EditorView(svc.ledger),
        "holders": HolderView(svc.ledger),
        "audience_cn": AudienceView(svc.ledger, territory="CN"),
    }


def main() -> None:
    store, _ = build()
    out = Path(__file__).resolve().parents[1] / "data" / "sample_events.jsonl"
    store.save_jsonl(out)
    print(f"已写出 {len(store)} 条事件 → {out}")


if __name__ == "__main__":
    main()
