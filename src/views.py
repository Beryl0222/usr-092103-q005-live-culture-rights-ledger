"""三类端视图：编辑发布台、权利人对账单、观众播放页。

视图全部由折叠状态即时计算，不落任何数据；同一个状态在三端看到的
差异只体现在“暴露多少细节”，事实口径保持一致。
"""

from __future__ import annotations

from datetime import datetime

from . import catalogs as C
from .ledger import State, fold, segment_verdict
from .policies import normalize, intersect, subtract, windows_len, Window


# ---------------------------------------------------------------- 编辑发布台

def editor_workspace(store_or_state, session_id: str, *,
                     at: datetime | None = None, territory: str = "CN") -> dict:
    """编辑在发布前看到每个片段可用于直播、回放还是商业剪辑，以及卡点原因。"""
    state = store_or_state if isinstance(store_or_state, State) else fold(store_or_state, at)
    when = at or state.as_of or datetime.now()
    session = state.sessions.get(session_id)
    segments_out = []
    for seg in state.session_segments(session_id):
        uses_out = {}
        for use in C.USES:
            v = segment_verdict(state, seg, use, territory=territory, at=when)
            uses_out[use] = {
                "use_label": C.USE_LABELS[use],
                "allowed_windows": v["allowed"],
                "allowed_ms": windows_len(v["allowed"]),
                "total_ms": windows_len(seg["windows"]),
                "restrictions": [
                    {
                        "reason_code": reason,
                        "reason": C.REASON_LABELS.get(reason, reason),
                        "windows": ws,
                        "duration_ms": windows_len(ws),
                    }
                    for reason, ws in sorted(
                        v["blockers"].items(),
                        key=lambda kv: C.REASON_PRIORITY.index(kv[0])
                        if kv[0] in C.REASON_PRIORITY else 99,
                    )
                ],
                "materials": v["materials"],
            }
        segments_out.append({
            "segment_id": seg["id"],
            "title": seg.get("title", ""),
            "windows": seg["windows"],
            "uses": uses_out,
        })

    pending_consents = [
        {
            "permission_id": p["id"],
            "material_id": p["material_id"],
            "right_holder_id": p["right_holder_id"],
            "uses": p["uses"],
            "captured_at": p.get("captured_at").isoformat() if p.get("captured_at") else None,
        }
        for p in state.permissions.values()
        if p["consent_state"] == C.CONSENT_PENDING
    ]
    return {
        "session_id": session_id,
        "session_title": session.get("title") if session else None,
        "session_status": session.get("status") if session else None,
        "territory": territory,
        "as_of": when.isoformat(),
        "pending_consents": pending_consents,
        "segments": segments_out,
    }


# ---------------------------------------------------------------- 权利人对账单

def _used_in_releases(state: State, material_ids: set[str]) -> list[dict]:
    """找出这些素材出现在哪些已发布回放/剪辑中（按时间区间重叠判断）。"""
    # material_id -> [(segment_id, windows)]
    apps: dict[str, list[tuple[str, list[Window]]]] = {}
    for mid in material_ids:
        m = state.materials.get(mid)
        if not m:
            continue
        for a in m["appearances"]:
            apps.setdefault(a["segment_id"], []).append((mid, a["windows"]))

    used = []

    def hit(source_segments):
        hits = []
        for ss in source_segments:
            for mid, ws in apps.get(ss["segment_id"], []):
                overlap = intersect(ws, ss["time_windows"])
                if overlap:
                    hits.append({"material_id": mid, "segment_id": ss["segment_id"],
                                 "windows": overlap})
        return hits

    for replay in state.replays.values():
        h = hit(replay.get("source_segments", []))
        if h:
            used.append({"release_type": "REPLAY", "release_id": replay["id"],
                         "status": replay["status"],
                         "version_no": replay.get("version_no"), "usages": h})
    for cut in state.cuts.values():
        h = hit(cut.get("source_segments", []))
        if h:
            used.append({"release_type": "CUT", "release_id": cut["id"],
                         "title": cut.get("title"), "status": cut["status"], "usages": h})
    return used


def right_holder_statement(store_or_state, holder_id: str, *,
                           at: datetime | None = None) -> dict:
    """权利人核对：我的素材、授权状态、被用在哪里、收入与调整明细。"""
    state = store_or_state if isinstance(store_or_state, State) else fold(store_or_state, at)
    holder = state.participants.get(holder_id, {"id": holder_id})

    materials = [
        {
            "material_id": mid,
            "material_type": m.get("material_type"),
            "appearances": m.get("appearances", []),
            "permissions": [
                {
                    "permission_id": p["id"],
                    "state": p["consent_state"],
                    "state_label": {
                        C.CONSENT_PENDING: "口头同意待补签",
                        C.CONSENT_CONFIRMED: "已正式授权",
                        C.CONSENT_WITHDRAWN: "已撤回",
                    }[p["consent_state"]],
                    "uses": p["uses"],
                    "territories": p["territories"],
                    "valid_from": p["valid_from"],
                    "valid_until": p["valid_until"],
                    "time_windows": p["time_windows"],
                }
                for p in state.permissions.values() if p["material_id"] == mid
            ],
        }
        for mid, m in sorted(state.materials.items())
        if any(p["material_id"] == mid and p.get("right_holder_id") == holder_id
               for p in state.permissions.values())
        or m.get("participant_id") == holder_id
    ]
    material_ids = {m["material_id"] for m in materials}

    entries = [
        {
            "at": e["at"].isoformat(), "amount": e["amount"],
            "kind": e.get("entry_kind"),
            "tip_id": e.get("tip_id"),
            "contract_id": e.get("contract_id"),
            "note": e.get("note", ""),
            "event_type": e["event_type"],
        }
        for e in state.revenue
        if e["payee_id"] == holder_id
    ]
    total = sum(e["amount"] for e in entries)
    return {
        "right_holder_id": holder_id,
        "name": holder.get("name"),
        "role": holder.get("role"),
        "materials": materials,
        "used_in": _used_in_releases(state, material_ids),
        "revenue_entries": entries,
        "revenue_total_fen": total,
        "currency": "CNY",
    }


# ---------------------------------------------------------------- 观众播放页

def _viewer_timeline(state: State, source_segments: list[dict], *,
                     use: str, territory: str, at: datetime) -> list[dict]:
    timeline = []
    for ss in source_segments:
        seg = state.segments.get(ss["segment_id"])
        if seg is None:
            continue
        v = segment_verdict(state, seg, use, territory=territory, at=at,
                            axis=ss["time_windows"])
        blocked = normalize([w for ws in v["blockers"].values() for w in ws])
        playable = subtract(ss["time_windows"], blocked)
        reasons = [
            {
                "reason_code": reason,
                "message": C.VIEWER_REASON_LABELS.get(reason, reason),
                "windows": ws,
            }
            for reason, ws in sorted(
                v["blockers"].items(),
                key=lambda kv: C.REASON_PRIORITY.index(kv[0])
                if kv[0] in C.REASON_PRIORITY else 99,
            )
        ]
        timeline.append({
            "segment_id": seg["id"],
            "title": seg.get("title", ""),
            "playable_windows": playable,
            "unavailable_windows": blocked,
            "reasons": reasons,
        })
    return timeline


def viewer_replay(store_or_state, replay_id: str, *,
                  territory: str = "CN", at: datetime | None = None) -> dict:
    """观众端回放：只给已获准的可播区间，暂不可用区间给中性原因。"""
    state = store_or_state if isinstance(store_or_state, State) else fold(store_or_state, at)
    when = at or state.as_of or datetime.now()
    replay = state.replays.get(replay_id)
    if replay is None:
        return {"available": False, "message": "回放不存在"}
    if replay["status"] == "TAKEN_DOWN":
        return {
            "available": False,
            "replay_id": replay_id,
            "message": "回放暂不可用",
            "reason": replay.get("take_down_reason", ""),
        }
    timeline = _viewer_timeline(state, replay.get("source_segments", []),
                                use=C.USE_REPLAY, territory=territory, at=when)
    return {
        "available": True,
        "replay_id": replay_id,
        "version_no": replay.get("version_no"),
        "territory": territory,
        "timeline": timeline,
    }


def viewer_cut(store_or_state, cut_id: str, *,
               territory: str = "CN", at: datetime | None = None) -> dict:
    """观众端短视频切片：只有整段获准才上架，否则不出现（不展示半成品）。"""
    state = store_or_state if isinstance(store_or_state, State) else fold(store_or_state, at)
    when = at or state.as_of or datetime.now()
    cut = state.cuts.get(cut_id)
    if cut is None or cut["status"] != "PUBLISHED":
        return {"available": False, "message": "内容暂不可用"}
    timeline = _viewer_timeline(state, cut.get("source_segments", []),
                                use=C.USE_COMMERCIAL_CUT, territory=territory, at=when)
    if any(item["unavailable_windows"] for item in timeline):
        # 已发布切片在新限制出现后应立即对观众隐身（发布状态下架由运营事件跟进）
        return {"available": False, "cut_id": cut_id, "message": "内容暂不可用"}
    return {
        "available": True,
        "cut_id": cut_id,
        "title": cut.get("title"),
        "territory": territory,
        "timeline": timeline,
    }
