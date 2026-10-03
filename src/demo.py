"""端到端联调故事线：一场非遗剪纸直播的完整权益账。

运行：python3 -m src.demo
会重建 data/livestream_scenario.jsonl（不可变事件日志），并在关键时点
打印编辑发布台、观众播放页、权利人对账单三类视图。
"""

from __future__ import annotations

import json
from pathlib import Path

from . import catalogs as C
from .events import JsonlEventStore
from .ledger import RightsLedger, RightsLedgerError, parse_dt
from .views import editor_workspace, viewer_replay, right_holder_statement

DATA = Path(__file__).resolve().parents[1] / "data" / "livestream_scenario.jsonl"

SESSION = "live-20261001-papercut"
SEG_A, SEG_B, SEG_C = "seg-A-示范", "seg-B-连麦", "seg-C-伴奏谢幕"
P_INH, P_MUS, P_V1, P_V2 = (
    "p-inheritor-zhou", "p-musician-lin", "p-viewer-wu", "p-viewer-minor"
)
MAT_DEMO, MAT_LINK1, MAT_LINK2 = "mat-demo-zhou", "mat-link-wu", "mat-link-minor"
MAT_BGM, MAT_BGM2, MAT_CLIP = "mat-bgm-uncertain", "mat-bgm-licensed", "mat-platform-slice"
MIN = 60_000


def build(store: JsonlEventStore) -> RightsLedger:
    L = RightsLedger(store)

    # ---- 演前：合同、参与者、正式许可 ----
    L.execute_contract(
        "contract-v1", SESSION, "2026-09-25T10:00:00+08:00",
        revenue_share={P_INH: 0.5, P_MUS: 0.1},  # 余额 0.4 归平台抽成
        platform_rate=0.4,
    )
    L.register_participant(P_INH, "周师傅（非遗传承人）", C.ROLE_INHERITOR,
                           "2026-09-26T10:00:00+08:00")
    L.register_participant(P_MUS, "林乐手", C.ROLE_MUSICIAN, "2026-09-26T10:00:00+08:00")
    L.register_participant(P_V1, "吴同学（连麦观众）", C.ROLE_VIEWER,
                           "2026-09-26T10:00:00+08:00")
    L.register_participant(P_V2, "小观众（未成年人）", C.ROLE_VIEWER,
                           "2026-09-26T10:00:00+08:00", is_minor=True)

    # 传承人示范：演前已签全用途正式许可
    L.grant_permission(
        "perm-demo", material_id=MAT_DEMO, right_holder_id=P_INH,
        uses=list(C.USES), territories=["CN"],
        valid_from="2026-09-01T00:00:00+08:00", valid_until="2027-09-01T00:00:00+08:00",
        at="2026-09-26T11:00:00+08:00",
    )
    # 谢幕伴奏曲目：已授权，但 10 月 11 日到期（用于演示到期只限制时段）
    L.grant_permission(
        "perm-bgm2", material_id=MAT_BGM2, right_holder_id=P_MUS,
        uses=list(C.USES), territories=["CN"],
        valid_from="2026-09-01T00:00:00+08:00",
        valid_until="2026-10-11T00:00:00+08:00",
        at="2026-09-26T11:05:00+08:00",
    )

    # ---- 10-01 直播当天 ----
    L.schedule_session(SESSION, "非遗剪纸公开课直播", "2026-10-01T19:00:00+08:00",
                       event_id="live-1001-schedule")
    L.register_segment(SEG_A, SESSION, "传承人剪纸示范", [{"start_ms": 0, "end_ms": 20 * MIN}],
                       "2026-10-01T19:00:00+08:00")
    L.register_segment(SEG_B, SESSION, "观众连麦体验",
                       [{"start_ms": 20 * MIN, "end_ms": 40 * MIN}],
                       "2026-10-01T19:00:00+08:00")
    L.register_segment(SEG_C, SESSION, "伴奏与谢幕",
                       [{"start_ms": 40 * MIN, "end_ms": 60 * MIN}],
                       "2026-10-01T19:00:00+08:00")
    L.start_session(SESSION, "2026-10-01T19:00:00+08:00")

    # 段落一：传承人示范 + 平台切片
    L.link_material(MAT_DEMO, material_type=C.MATERIAL_HERITAGE_DEMO, segment_id=SEG_A,
                    windows=[{"start_ms": 0, "end_ms": 20 * MIN}],
                    at="2026-10-01T19:02:00+08:00", participant_id=P_INH,
                    source_ref="studio-cam-01")
    L.link_material(MAT_CLIP, material_type=C.MATERIAL_PLATFORM_CLIP, segment_id=SEG_A,
                    windows=[{"start_ms": 5 * MIN, "end_ms": 15 * MIN}],
                    at="2026-10-01T19:03:00+08:00",
                    source_ref="platform-auto-slice")
    L.grant_permission(
        "perm-clip", material_id=MAT_CLIP, right_holder_id=C.ROLE_PLATFORM,
        uses=list(C.USES), territories=["CN"],
        valid_from="2026-10-01T00:00:00+08:00", valid_until=None,
        at="2026-10-01T19:03:30+08:00",
    )

    # 段落二：成年观众连麦——直播中只有口头同意，形成待确认记录
    L.link_material(MAT_LINK1, material_type=C.MATERIAL_VOICE_LINK, segment_id=SEG_B,
                    windows=[{"start_ms": 20 * MIN, "end_ms": 30 * MIN}],
                    at="2026-10-01T19:22:00+08:00", participant_id=P_V1)
    L.capture_verbal_consent(
        "perm-link-wu", material_id=MAT_LINK1, right_holder_id=P_V1,
        uses=list(C.USES), at="2026-10-01T19:22:30+08:00",
    )
    L.receive_contribution(
        "contrib-wu", session_id=SESSION, segment_id=SEG_B, participant_id=P_V1,
        material_id=MAT_LINK1, windows=[{"start_ms": 20 * MIN, "end_ms": 30 * MIN}],
        at="2026-10-01T19:23:00+08:00",
    )

    # 段落二：未成年人连麦——口头同意待确认，系统自动只限制该 10 分钟
    L.link_material(MAT_LINK2, material_type=C.MATERIAL_VOICE_LINK, segment_id=SEG_B,
                    windows=[{"start_ms": 30 * MIN, "end_ms": 40 * MIN}],
                    at="2026-10-01T19:31:00+08:00", participant_id=P_V2, is_minor=True)
    L.capture_verbal_consent(
        "perm-link-minor", material_id=MAT_LINK2, right_holder_id=P_V2,
        uses=list(C.USES), at="2026-10-01T19:31:30+08:00",
        note="未成年人口头同意，需监护人补签",
    )
    L.receive_contribution(
        "contrib-minor", session_id=SESSION, segment_id=SEG_B, participant_id=P_V2,
        material_id=MAT_LINK2, windows=[{"start_ms": 30 * MIN, "end_ms": 40 * MIN}],
        at="2026-10-01T19:32:00+08:00",
    )

    # 段落三：BGM 自动识别不确定——先限制对应时段，不删整场
    L.link_material(MAT_BGM, material_type=C.MATERIAL_ACCOMPANIMENT, segment_id=SEG_C,
                    windows=[{"start_ms": 40 * MIN, "end_ms": 50 * MIN}],
                    at="2026-10-01T19:41:00+08:00",
                    certainty=C.CERTAINTY_UNCERTAIN, source_ref="bgm-fingerprint-hit?")
    L.link_material(MAT_BGM2, material_type=C.MATERIAL_ACCOMPANIMENT, segment_id=SEG_C,
                    windows=[{"start_ms": 50 * MIN, "end_ms": 60 * MIN}],
                    at="2026-10-01T19:51:00+08:00", participant_id=P_MUS,
                    certainty=C.CERTAINTY_VERIFIED, source_ref="repertoire/lin-07")

    # ---- 打赏：按下单时点有效合同 v1 立即结算 ----
    L.place_tip("tip-1001", session_id=SESSION, segment_id=SEG_B, participant_id=P_V1,
                amount=10_000, at="2026-10-01T19:25:00+08:00", order_no="WX202610011925")
    L.place_tip("tip-1002", session_id=SESSION, segment_id=SEG_C, participant_id=P_V1,
                amount=5_000, at="2026-10-01T19:50:00+08:00", order_no="WX202610011950")
    L.end_session(SESSION, "2026-10-01T20:00:00+08:00")

    # ---- 演后：补签、回放、退款、核实、确权调整、二剪、到期、续约 ----
    # 10-02 上午：成年观众补签；当晚发布回放 v1（仍有两处时段受限，按期屏蔽）
    L.confirm_consent("perm-link-wu", "2026-10-02T10:00:00+08:00")
    L.publish_replay(
        "replay-v1", session_id=SESSION, version_no=1,
        at="2026-10-02T12:00:00+08:00",
        source_segments=[
            {"segment_id": SEG_A, "time_windows": [{"start_ms": 0, "end_ms": 20 * MIN}]},
            {"segment_id": SEG_B, "time_windows": [{"start_ms": 20 * MIN, "end_ms": 40 * MIN}]},
            {"segment_id": SEG_C, "time_windows": [{"start_ms": 40 * MIN, "end_ms": 60 * MIN}]},
        ],
    )
    # tip-1001 退款：原分账保留，追加反向冲正
    L.refund_tip("tip-1001", "2026-10-02T18:00:00+08:00", reason="观众申诉未成年充值，平台退款")

    # 10-03：商业剪辑送审。示范段可发；未成年人段被权益闸门拦下
    L.propose_cut("cut-demo", session_id=SESSION, title="剪纸精华一分钟", at="2026-10-03T09:00:00+08:00",
                  source_segments=[
                      {"segment_id": SEG_A, "time_windows": [{"start_ms": 5 * MIN, "end_ms": 15 * MIN}]}])
    L.publish_cut("cut-demo", "2026-10-03T09:05:00+08:00")
    L.propose_cut("cut-link-minor", session_id=SESSION, title="连麦名场面（暂不可发）",
                  at="2026-10-03T09:10:00+08:00",
                  source_segments=[
                      {"segment_id": SEG_B, "time_windows": [{"start_ms": 30 * MIN, "end_ms": 40 * MIN}]}])
    try:
        L.publish_cut("cut-link-minor", "2026-10-03T09:15:00+08:00")
    except RightsLedgerError:
        pass  # 闸门已同时落下 CUT_REJECTED 事件

    # 10-04：监护人补签 → 未成年人保护限制按时段解除；BGM 核实 → 解除 BGM 限制
    L.confirm_consent("perm-link-minor", "2026-10-04T09:00:00+08:00")
    L.verify_material(MAT_BGM, "2026-10-04T10:00:00+08:00")
    # 识别核实后，由曲库确认权属并补发正式许可（核实与授权是两件事）
    L.grant_permission(
        "perm-bgm-cleared", material_id=MAT_BGM, right_holder_id=P_MUS,
        uses=list(C.USES), territories=["CN"],
        valid_from="2026-10-04T10:30:00+08:00", valid_until="2027-10-04T00:00:00+08:00",
        at="2026-10-04T10:30:00+08:00",
    )
    # 演后新合同 v1 不变；确权发现乐手伴奏贡献被少计，补付（不改历史分录）
    L.adjust_rights(
        "adj-musician-topup", session_id=SESSION, at="2026-10-04T11:00:00+08:00",
        kind=C.ADJ_TOP_UP, tip_id="tip-1002",
        allocations=[{"payee_id": P_MUS, "amount": 300, "basis": "确权补付"}],
        reason="伴奏曲目核实后，按贡献补付乐手",
    )
    # 限制解除后，连麦切片重新送审通过
    L.propose_cut("cut-link-minor-v2", session_id=SESSION, title="连麦名场面",
                  at="2026-10-04T12:00:00+08:00",
                  source_segments=[
                      {"segment_id": SEG_B, "time_windows": [{"start_ms": 30 * MIN, "end_ms": 40 * MIN}]}])
    L.publish_cut("cut-link-minor-v2", "2026-10-04T12:05:00+08:00")

    # 10-11 谢幕伴奏授权到期：观众端相应时段变为不可用，整场回放不下架
    # （10-12 的观众视图可见效果，无需额外操作事件）

    # 10-15 续约：同一素材拿到新许可，发布回放 v2，受限时段恢复
    L.grant_permission(
        "perm-bgm2-renewed", material_id=MAT_BGM2, right_holder_id=P_MUS,
        uses=list(C.USES), territories=["CN"],
        valid_from="2026-10-15T00:00:00+08:00", valid_until="2027-10-15T00:00:00+08:00",
        at="2026-10-15T09:00:00+08:00",
    )
    L.publish_replay(
        "replay-v2", session_id=SESSION, version_no=2,
        at="2026-10-15T10:00:00+08:00",
        source_segments=[
            {"segment_id": SEG_A, "time_windows": [{"start_ms": 0, "end_ms": 20 * MIN}]},
            {"segment_id": SEG_B, "time_windows": [{"start_ms": 20 * MIN, "end_ms": 40 * MIN}]},
            {"segment_id": SEG_C, "time_windows": [{"start_ms": 40 * MIN, "end_ms": 60 * MIN}]},
        ],
    )
    return L


def pp(title: str, obj: object) -> None:
    print(f"\n===== {title} =====")
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def main() -> None:
    if DATA.exists():
        DATA.unlink()
    store = JsonlEventStore(DATA)
    L = build(store)
    print(f"已写入 {len(store)} 条不可变事件 -> {DATA.relative_to(Path.cwd())}")

    # 编辑视角：10-02 上午（口头同意未补签、BGM 未核实时），发布前逐片段看可用性
    ed = editor_workspace(L.store, SESSION, at=parse_dt("2026-10-02T09:00:00+08:00"))
    pp("编辑发布台 2026-10-02 09:00（按用途汇总，毫秒）", [
        {"segment": s["title"],
         **{C.USE_LABELS[u]: {"可用": v["allowed_ms"], "受限": v["total_ms"] - v["allowed_ms"],
                              "原因": [r["reason"] for r in v["restrictions"]]}
            for u, v in s["uses"].items()}}
        for s in ed["segments"]
    ])

    # 观众视角：回放 v1 上线当晚，未成年人段与 BGM 段被按时段屏蔽，原因中性化
    vw = viewer_replay(L.store, "replay-v1", at=parse_dt("2026-10-02T13:00:00+08:00"))
    pp("观众端回放 v1 2026-10-02（只给可播区间与中性提示）", [
        {"段落": t["title"],
         "可播": [(w["start_ms"], w["end_ms"]) for w in t["playable_windows"]],
         "暂不可用": [{"区间": (w["start_ms"], w["end_ms"]), "提示": r["message"]}
                      for w in t["unavailable_windows"] for r in t["reasons"]
                      if any(a <= w["start_ms"] < b for a, b in
                             [(x["start_ms"], x["end_ms"]) for x in r["windows"]])]}
        for t in vw["timeline"]
    ])

    # 观众视角：10-12 授权到期，仅谢幕伴奏 10 分钟变灰，整场仍在
    vw_expired = viewer_replay(L.store, "replay-v1", at=parse_dt("2026-10-12T12:00:00+08:00"))
    pp("观众端回放 v1 2026-10-12（授权到期只限制对应时段）", [
        {"段落": t["title"],
         "可播": [(w["start_ms"], w["end_ms"]) for w in t["playable_windows"]],
         "暂不可用": [r["message"] for r in t["reasons"]]}
        for t in vw_expired["timeline"]
    ])

    # 权利人视角：乐手核对使用与收入（含退款冲正与确权补付）
    pp("权利人对账单：林乐手",
       {k: v for k, v in right_holder_statement(
           L.store, P_MUS, at=parse_dt("2026-10-15T12:00:00+08:00")).items()
        if k in ("name", "revenue_entries", "revenue_total_fen", "used_in")})
    pp("权利人对账单：周师傅",
       {k: v for k, v in right_holder_statement(
           L.store, P_INH, at=parse_dt("2026-10-15T12:00:00+08:00")).items()
        if k in ("name", "revenue_entries", "revenue_total_fen", "used_in")})


if __name__ == "__main__":
    main()
