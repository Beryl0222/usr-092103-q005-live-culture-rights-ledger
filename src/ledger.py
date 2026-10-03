"""事件折叠投影 + 节目权益账应用服务。

fold() 把不可变事件流折叠成当前状态（也可按某历史时点折叠）；
RightsLedger 在事件存储之上提供业务操作，所有状态变化仍然落成事件，
不产生任何就地修改。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from . import catalogs as C
from .events import EventStore
from .policies import (
    normalize,
    intersect,
    subtract,
    windows_len,
    parse_dt,
    evaluate_material,
    combine_materials,
    apply_manual_restrictions,
    primary_reason,
    REASON_NOT_LICENSED,
    Window,
)


class RightsLedgerError(ValueError):
    """业务规则被违反（在未授权区间发布、结算缺少合同等）。"""


# ---------------------------------------------------------------- 折叠状态

@dataclass
class State:
    as_of: datetime | None = None
    sessions: dict = field(default_factory=dict)
    segments: dict = field(default_factory=dict)
    participants: dict = field(default_factory=dict)
    materials: dict = field(default_factory=dict)
    permissions: dict = field(default_factory=dict)  # permission_id -> 许可（含 material_id）
    restrictions: dict = field(default_factory=dict)  # restriction_id -> 限制
    contracts: list = field(default_factory=list)     # 合同版本（追加，按时点选用）
    tips: dict = field(default_factory=dict)
    contributions: dict = field(default_factory=dict)
    revenue: list = field(default_factory=list)       # 分账分录（含调整，带符号）
    replays: dict = field(default_factory=dict)
    cuts: dict = field(default_factory=dict)

    def session_segments(self, session_id: str) -> list[dict]:
        return [s for s in self.segments.values() if s["session_id"] == session_id]

    def material_grants(self, material_id: str) -> list[dict]:
        return [p for p in self.permissions.values() if p["material_id"] == material_id]

    def effective_contract(self, session_id: str, at: datetime) -> dict | None:
        """选用 at 时点有效的最新合同版本；没有则 None。"""
        candidates = [
            k
            for k in self.contracts
            if k["session_id"] == session_id
            and (k.get("valid_from_dt") is None or at >= k["valid_from_dt"])
            and (k.get("valid_until_dt") is None or at < k["valid_until_dt"])
        ]
        return candidates[-1] if candidates else None


def fold(store: EventStore, as_of: datetime | None = None) -> State:
    state = State(as_of=as_of)

    for se in store.stream():
        occurred = parse_dt(se["occurred_at"])
        if as_of is not None and occurred > as_of:
            continue  # 事件按追加序保存，但 occurred_at 不保证跨聚合单调，故跳过而非中断
        et, agg, p = se["event_type"], se["aggregate_id"], se.get("payload") or {}
        _apply(state, et, agg, p, occurred)
    return state


def _apply(state: State, et: str, agg: str, p: dict, occurred: datetime) -> None:
    if et == C.EV_SESSION_SCHEDULED:
        state.sessions[agg] = {
            "id": agg,
            "title": p.get("title", ""),
            "scheduled_at": p.get("scheduled_at"),
            "started_at": None,
            "ended_at": None,
            "status": "SCHEDULED",
        }
    elif et == C.EV_SESSION_STARTED:
        s = state.sessions.setdefault(agg, {"id": agg})
        s["started_at"] = occurred
        s["status"] = "LIVE"
    elif et == C.EV_SESSION_ENDED:
        s = state.sessions.setdefault(agg, {"id": agg})
        s["ended_at"] = occurred
        s["status"] = "ENDED"

    elif et == C.EV_SEGMENT_REGISTERED:
        state.segments[agg] = {
            "id": agg,
            "session_id": p["session_id"],
            "title": p.get("title", ""),
            "windows": normalize(p["time_windows"]),
            "restrictions": [],
        }

    elif et == C.EV_PARTICIPANT_REGISTERED:
        state.participants[agg] = {
            "id": agg,
            "name": p.get("name", agg),
            "role": p.get("role"),
            "is_minor": bool(p.get("is_minor", False)),
        }

    elif et == C.EV_MATERIAL_LINKED:
        m = state.materials.setdefault(
            agg,
            {"id": agg, "material_type": p.get("material_type"), "appearances": [],
             "participant_id": p.get("participant_id"), "source_ref": p.get("source_ref")},
        )
        if p.get("material_type"):
            m["material_type"] = p["material_type"]
        if p.get("participant_id"):
            m["participant_id"] = p["participant_id"]
        if p.get("certainty"):
            m["certainty"] = p["certainty"]
        if p.get("source_ref"):
            m["source_ref"] = p["source_ref"]
        if "is_minor" in p:
            m["is_minor"] = bool(p["is_minor"])
        appearance = {"segment_id": p.get("segment_id"), "windows": normalize(p["time_windows"])}
        m["appearances"].append(appearance)
    elif et == C.EV_MATERIAL_VERIFIED:
        m = state.materials.get(agg)
        if m is not None:
            m["certainty"] = p.get("certainty", C.CERTAINTY_VERIFIED)
            m["verified_at"] = occurred

    elif et in (C.EV_PERMISSION_GRANTED, C.EV_CONSENT_CAPTURED):
        state.permissions[agg] = {
            "id": agg,
            "material_id": p["material_id"],
            "right_holder_id": p.get("right_holder_id"),
            "uses": list(p.get("uses", [])),
            "territories": list(p.get("territories", [])),
            "valid_from": p.get("valid_from"),
            "valid_from_dt": parse_dt(p.get("valid_from")),
            "valid_until": p.get("valid_until"),
            "valid_until_dt": parse_dt(p.get("valid_until")),
            "time_windows": normalize(p["time_windows"]) if p.get("time_windows") else None,
            "consent_state": p.get("consent_state") or C.CONSENT_CONFIRMED,
            "captured_at": occurred if et == C.EV_CONSENT_CAPTURED else None,
        }
    elif et == C.EV_CONSENT_CONFIRMED:
        perm = state.permissions.get(agg)
        if perm is None:
            raise RightsLedgerError(f"确认同意 {agg} 不存在对应待确认记录")
        perm["consent_state"] = C.CONSENT_CONFIRMED
        if p.get("uses"):
            perm["uses"] = list(p["uses"])
        if p.get("territories"):
            perm["territories"] = list(p["territories"])
        if p.get("valid_until") is not None:
            perm["valid_until"] = p["valid_until"]
            perm["valid_until_dt"] = parse_dt(p["valid_until"])
        if p.get("time_windows"):
            perm["time_windows"] = normalize(p["time_windows"])
    elif et == C.EV_CONSENT_WITHDRAWN:
        perm = state.permissions.get(agg)
        if perm is None:
            raise RightsLedgerError(f"撤回 {agg} 不存在对应许可")
        perm["consent_state"] = C.CONSENT_WITHDRAWN
        perm["withdrawn_at"] = occurred

    elif et == C.EV_RELEASE_RESTRICTED:
        r = p["restriction"]
        rid = p.get("restriction_id") or f"auto-{agg}-{occurred.isoformat()}"
        state.restrictions[rid] = {
            "restriction_id": rid,
            "segment_id": agg,
            "reason_code": r["reason_code"],
            "uses": list(r.get("uses", C.USES)),
            "territories": list(r.get("territories", [])),
            "time_windows": normalize(r["time_windows"]) if r.get("time_windows") else None,
            "detail_ref": r.get("detail_ref"),
            "status": "ACTIVE",
            "set_at": occurred,
        }
        seg = state.segments.get(agg)
        if seg is not None and rid not in seg["restrictions"]:
            seg["restrictions"].append(rid)
    elif et == C.EV_RELEASE_CLEARED:
        rid = p["restriction_id"]
        if rid in state.restrictions:
            state.restrictions[rid]["status"] = "CLEARED"
            state.restrictions[rid]["cleared_at"] = occurred

    elif et == C.EV_CONTRACT_EXECUTED:
        state.contracts.append({
            "id": agg,
            "session_id": p["session_id"],
            "revenue_share": dict(p.get("revenue_share", {})),
            "platform_rate": p.get("platform_rate", 0),
            "valid_from": p.get("valid_from"),
            "valid_from_dt": parse_dt(p.get("valid_from")) or occurred,
            "valid_until": p.get("valid_until"),
            "valid_until_dt": parse_dt(p.get("valid_until")),
            "executed_at": occurred,
        })

    elif et == C.EV_CONTRIBUTION_RECEIVED:
        state.contributions[agg] = {
            "id": agg,
            "session_id": p.get("session_id"),
            "segment_id": p.get("segment_id"),
            "viewer_id": p.get("participant_id"),
            "material_id": p.get("material_id"),
            "windows": normalize(p["time_windows"]) if p.get("time_windows") else [],
            "received_at": occurred,
        }

    elif et == C.EV_TIP_PLACED:
        state.tips[agg] = {
            "id": agg,
            "session_id": p["session_id"],
            "segment_id": p.get("segment_id"),
            "viewer_id": p.get("participant_id"),
            "amount": p["amount"],
            "order_no": p.get("order_no"),
            "status": "PAID",
            "placed_at": occurred,
            "allocations": [],
        }
    elif et == C.EV_TIP_REFUNDED:
        tip = state.tips.get(agg)
        if tip:
            tip["status"] = "REFUNDED"
            tip["refunded_at"] = occurred
            tip["refund_reason"] = p.get("reason", "")

    elif et == C.EV_REVENUE_ALLOCATED:
        entry_kind = p.get("entry_kind", C.ENTRY_INITIAL)
        sign = -1 if entry_kind == C.ENTRY_REVERSAL else 1
        for a in p.get("allocations", []):
            state.revenue.append({
                "payee_id": a["payee_id"],
                "amount": sign * int(a["amount"]),
                "session_id": _session_of_tip(state, p.get("tip_id")),
                "tip_id": p.get("tip_id"),
                "contract_id": a.get("basis"),
                "event_type": et,
                "entry_kind": entry_kind,
                "at": occurred,
                "note": p.get("note", ""),
            })

    elif et == C.EV_RIGHTS_ADJUSTED:
        # 后续确权的补付（正）/冲正（负），不改动原始分录，只追加调整
        sign = 1 if p.get("adjustment_kind") == C.ADJ_TOP_UP else -1
        for a in p.get("allocations", []):
            state.revenue.append({
                "payee_id": a["payee_id"],
                "amount": sign * int(a["amount"]),
                "session_id": p.get("session_id"),
                "tip_id": p.get("tip_id"),
                "basis": a.get("basis", p.get("reason", "")),
                "event_type": et,
                "entry_kind": p.get("adjustment_kind"),
                "at": occurred,
                "note": p.get("reason", ""),
            })

    elif et == C.EV_REPLAY_PUBLISHED:
        state.replays[agg] = {
            "id": agg, "session_id": p["session_id"], "version_no": p.get("version_no", 1),
            "source_segments": list(p.get("source_segments", [])),
            "status": "PUBLISHED", "published_at": occurred,
        }
    elif et == C.EV_REPLAY_TAKEN_DOWN:
        r = state.replays.get(agg)
        if r:
            r["status"] = "TAKEN_DOWN"
            r["taken_down_at"] = occurred
            r["take_down_reason"] = p.get("reason", "")

    elif et == C.EV_CUT_PROPOSED:
        state.cuts[agg] = {
            "id": agg, "session_id": p["session_id"], "title": p.get("title", ""),
            "source_segments": list(p.get("source_segments", [])),
            "status": "PROPOSED", "proposed_at": occurred,
        }
    elif et == C.EV_CUT_PUBLISHED:
        c = state.cuts.get(agg)
        if c:
            c["status"] = "PUBLISHED"
            c["published_at"] = occurred
    elif et == C.EV_CUT_REJECTED:
        c = state.cuts.get(agg)
        if c:
            c["status"] = "REJECTED"
            c["rejected_at"] = occurred
            c["reject_reason"] = p.get("reason", "")


def _session_of_tip(state: State, tip_id: str | None) -> str | None:
    return state.tips[tip_id]["session_id"] if tip_id and tip_id in state.tips else None


# ---------------------------------------------------------------- 时间轴判定

def material_is_minor(state: State, material: dict) -> bool:
    if material.get("is_minor"):
        return True
    pid = material.get("participant_id")
    return bool(pid and state.participants.get(pid, {}).get("is_minor"))


def segment_material_appearance(state: State, material: dict, segment_id: str) -> list[Window]:
    ws: list[Window] = []
    for a in material.get("appearances", []):
        if a.get("segment_id") == segment_id:
            ws.extend(a["windows"])
    return normalize(ws)


def segment_verdict(
    state: State,
    segment: dict,
    use: str,
    *,
    territory: str = "CN",
    at: datetime | None = None,
    axis: list[Window] | None = None,
) -> dict:
    """段落（或其给定子区间）在某用途/地域/时点的可用性。

    返回 {"allowed": [...], "blockers": {原因: 区间}, "materials": [...]}。
    判定原则：
    - 某毫秒出现的每个素材都可用，该毫秒才可用（权限交集）；
    - 没有任何素材登记的毫秒视为未授权；
    - 未成年人连麦、BGM 不确定、到期等只限制对应区间，不牵连整段。
    """
    at = at or state.as_of or datetime.now()
    seg_windows = normalize(axis) if axis else segment["windows"]

    results = []
    covered: list[Window] = []
    for material in state.materials.values():
        appearance = segment_material_appearance(state, material, segment["id"])
        m_axis = intersect(seg_windows, appearance)
        if not m_axis:
            continue
        covered.extend(m_axis)
        m_view = dict(material)
        m_view["is_minor"] = material_is_minor(state, m_view)
        grants = state.material_grants(material["id"])
        results.append(
            (material["id"], evaluate_material(m_view, grants, use=use,
                                               territory=territory, at=at, axis=m_axis))
        )

    covered = normalize(covered)
    # 各素材只在自己出现的区间有意义：把判定结果扩展到段落全轴，
    # 未出现的部分视为交集恒等元（不阻断），再做多素材权限交集。
    results_full = []
    for _mid, r in results:
        # 该素材的出现轴：blockers 与 allowed 的并集
        own_axis = normalize(r["allowed"] + [w for ws in r["blockers"].values() for w in ws])
        results_full.append({
            "allowed": normalize(r["allowed"] + subtract(seg_windows, own_axis)),
            "blockers": r["blockers"],
        })
    combined = combine_materials(results_full, seg_windows) if results_full else {
        "allowed": [], "blockers": {REASON_NOT_LICENSED: seg_windows}
    }
    uncovered = subtract(seg_windows, covered)
    blockers = {reason: intersect(ws, seg_windows)
                for reason, ws in combined["blockers"].items()}
    if uncovered:
        blockers.setdefault(REASON_NOT_LICENSED, [])
        blockers[REASON_NOT_LICENSED].extend(uncovered)
    allowed = subtract(seg_windows, normalize([w for ws in blockers.values() for w in ws]))
    verdict = {"allowed": normalize(allowed),
               "blockers": {k: normalize(v) for k, v in blockers.items() if normalize(v)}}

    # 运营人工限制
    manual = [
        state.restrictions[rid]
        for rid in segment.get("restrictions", [])
        if rid in state.restrictions
    ]
    # restrictions 也可能直接挂在段落 id 下（按 segment_id 索引）
    manual += [r for r in state.restrictions.values() if r.get("segment_id") == segment["id"]]
    if manual:
        # 去重
        seen, uniq = set(), []
        for r in manual:
            if r["restriction_id"] not in seen:
                seen.add(r["restriction_id"])
                uniq.append(r)
        # 人工限制按目标地域过滤；未指定 time_windows 的限制覆盖整个段落轴
        uniq = [r for r in uniq if not r.get("territories") or territory in r["territories"]]
        uniq = [dict(r, time_windows=r.get("time_windows") or seg_windows) for r in uniq]
        verdict = apply_manual_restrictions(verdict, uniq, use)

    verdict["materials"] = [mid for mid, _ in results]
    verdict["primary_reason"] = primary_reason(verdict["blockers"])
    return verdict


# ---------------------------------------------------------------- 应用服务

@dataclass
class Settlement:
    allocations: list           # [{"payee_id","amount","basis"}]
    contract_id: str
    platform_amount: int


class RightsLedger:
    """在事件存储上执行业务操作。每个方法都把变化追加成不可变事件。"""

    def __init__(self, store: EventStore):
        self.store = store

    def state(self, as_of: datetime | None = None) -> State:
        return fold(self.store, as_of)

    # ---- 场次 / 段落 / 参与者 / 素材 ----
    def schedule_session(self, session_id: str, title: str, scheduled_at: str, *,
                         event_id: str | None = None) -> dict:
        return self.store.append(
            event_type=C.EV_SESSION_SCHEDULED, aggregate_type=C.AGG_LIVE_SESSION,
            aggregate_id=session_id, occurred_at=scheduled_at,
            summary=f"排期直播场次：{title}", event_id=event_id,
            payload={"title": title, "scheduled_at": scheduled_at},
        ).event

    def start_session(self, session_id: str, at: str) -> dict:
        return self.store.append(
            event_type=C.EV_SESSION_STARTED, aggregate_type=C.AGG_LIVE_SESSION,
            aggregate_id=session_id, occurred_at=at, summary="直播开始",
        ).event

    def end_session(self, session_id: str, at: str) -> dict:
        return self.store.append(
            event_type=C.EV_SESSION_ENDED, aggregate_type=C.AGG_LIVE_SESSION,
            aggregate_id=session_id, occurred_at=at, summary="直播结束",
        ).event

    def register_segment(self, segment_id: str, session_id: str, title: str,
                         windows: list[Window], at: str) -> dict:
        return self.store.append(
            event_type=C.EV_SEGMENT_REGISTERED, aggregate_type=C.AGG_SEGMENT,
            aggregate_id=segment_id, occurred_at=at, summary=f"登记节目段落：{title}",
            payload={"session_id": session_id, "title": title,
                     "time_windows": normalize(windows)},
        ).event

    def register_participant(self, participant_id: str, name: str, role: str, at: str,
                             *, is_minor: bool = False) -> dict:
        return self.store.append(
            event_type=C.EV_PARTICIPANT_REGISTERED, aggregate_type=C.AGG_PARTICIPANT,
            aggregate_id=participant_id, occurred_at=at,
            summary=f"登记参与者：{name}（{role}{'，未成年人' if is_minor else ''}）",
            payload={"name": name, "role": role, "is_minor": is_minor},
        ).event

    def link_material(self, material_id: str, *, material_type: str, segment_id: str,
                      windows: list[Window], at: str, participant_id: str | None = None,
                      source_ref: str | None = None, certainty: str | None = None,
                      is_minor: bool | None = None) -> dict:
        payload: dict = {
            "material_type": material_type, "segment_id": segment_id,
            "time_windows": normalize(windows),
        }
        if participant_id:
            payload["participant_id"] = participant_id
        if source_ref:
            payload["source_ref"] = source_ref
        if certainty:
            payload["certainty"] = certainty
        if is_minor is not None:
            payload["is_minor"] = is_minor
        event = self.store.append(
            event_type=C.EV_MATERIAL_LINKED, aggregate_type=C.AGG_MATERIAL,
            aggregate_id=material_id, occurred_at=at,
            summary=f"关联素材 {material_id} 到段落 {segment_id}", payload=payload,
        ).event

        # 自动挂“可解除的时段限制”：只限制对应时间段，绝不牵连整场。
        state = fold(self.store, parse_dt(at))
        minor = bool(is_minor)
        if not minor and participant_id:
            minor = state.participants.get(participant_id, {}).get("is_minor", False)
        if minor:
            self.restrict(
                f"auto:minor:{material_id}:{segment_id}", segment_id,
                C.REASON_MINOR_ON_MIC, at,
                uses=[C.USE_REPLAY, C.USE_COMMERCIAL_CUT], windows=windows,
                detail_ref=material_id,
                summary=f"未成年人连麦时段保护限制：{material_id}（监护人补签后可解除）",
            )
        if material_type == C.MATERIAL_ACCOMPANIMENT and certainty == C.CERTAINTY_UNCERTAIN:
            self.restrict(
                f"auto:bgm:{material_id}:{segment_id}", segment_id,
                C.REASON_BGM_UNVERIFIED, at,
                uses=[C.USE_REPLAY, C.USE_COMMERCIAL_CUT], windows=windows,
                detail_ref=material_id,
                summary=f"背景音乐识别不确定，先限制相应时段：{material_id}（核实后解除）",
            )
        return event

    def verify_material(self, material_id: str, at: str) -> dict:
        """核实素材（如 BGM 识别结果确认）：解除该素材的 BGM 待核实时段限制。"""
        state = fold(self.store, parse_dt(at))
        material = state.materials.get(material_id)
        if material is None:
            raise RightsLedgerError(f"素材不存在：{material_id}")
        event = self.store.append(
            event_type=C.EV_MATERIAL_VERIFIED, aggregate_type=C.AGG_MATERIAL,
            aggregate_id=material_id, occurred_at=at,
            summary=f"素材识别核实完成：{material_id}",
            payload={"certainty": C.CERTAINTY_VERIFIED},
        ).event
        for rid, r in state.restrictions.items():
            if (r["status"] == "ACTIVE" and r["reason_code"] == C.REASON_BGM_UNVERIFIED
                    and r.get("detail_ref") == material_id):
                self.clear_restriction(rid, r["segment_id"], at,
                                       summary=f"BGM 已核实，解除限制 {rid}")
        return event

    # ---- 许可与口头同意 ----
    def grant_permission(self, permission_id: str, *, material_id: str,
                         right_holder_id: str, uses: list[str], territories: list[str],
                         valid_from: str, valid_until: str | None, at: str,
                         time_windows: list[Window] | None = None) -> dict:
        payload: dict = {
            "material_id": material_id, "right_holder_id": right_holder_id,
            "uses": uses, "territories": territories,
            "valid_from": valid_from, "valid_until": valid_until,
            "consent_state": C.CONSENT_CONFIRMED,
        }
        if time_windows:
            payload["time_windows"] = normalize(time_windows)
        return self.store.append(
            event_type=C.EV_PERMISSION_GRANTED, aggregate_type=C.AGG_PERMISSION,
            aggregate_id=permission_id, occurred_at=at,
            summary=f"正式许可 {permission_id}：{material_id}", payload=payload,
        ).event

    def capture_verbal_consent(self, permission_id: str, *, material_id: str,
                               right_holder_id: str, uses: list[str], at: str,
                               territories: list[str] | None = None,
                               time_windows: list[Window] | None = None,
                               note: str = "直播中临时口头同意，待补签") -> dict:
        """直播中的临时口头同意只能形成待确认（PENDING）记录，不能据此放行发布。"""
        payload: dict = {
            "material_id": material_id, "right_holder_id": right_holder_id,
            "uses": uses, "territories": territories or ["CN"],
            "valid_from": at, "valid_until": None,
            "consent_state": C.CONSENT_PENDING, "note": note,
        }
        if time_windows:
            payload["time_windows"] = normalize(time_windows)
        return self.store.append(
            event_type=C.EV_CONSENT_CAPTURED, aggregate_type=C.AGG_PERMISSION,
            aggregate_id=permission_id, occurred_at=at,
            summary=f"口头同意待确认：{material_id}", payload=payload,
        ).event

    def confirm_consent(self, permission_id: str, at: str, *,
                        uses: list[str] | None = None,
                        territories: list[str] | None = None,
                        valid_until: str | None = None) -> dict:
        state = fold(self.store, parse_dt(at))
        event = self.store.append(
            event_type=C.EV_CONSENT_CONFIRMED, aggregate_type=C.AGG_PERMISSION,
            aggregate_id=permission_id, occurred_at=at,
            summary=f"口头同意已补签确认：{permission_id}",
            payload={k: v for k, v in {
                "uses": uses, "territories": territories, "valid_until": valid_until,
            }.items() if v is not None},
        ).event
        # 补签覆盖的若是未成年人连麦素材，解除其保护性时段限制（仅解除对应素材）
        perm = state.permissions.get(permission_id)
        if perm is not None:
            material_id = perm["material_id"]
            for rid, r in state.restrictions.items():
                if (r["status"] == "ACTIVE" and r["reason_code"] == C.REASON_MINOR_ON_MIC
                        and r.get("detail_ref") == material_id):
                    self.clear_restriction(rid, r["segment_id"], at,
                                           summary=f"监护人已补签，解除未成年人保护限制 {rid}")
        return event

    def withdraw_consent(self, permission_id: str, at: str, *, reason: str = "") -> dict:
        return self.store.append(
            event_type=C.EV_CONSENT_WITHDRAWN, aggregate_type=C.AGG_PERMISSION,
            aggregate_id=permission_id, occurred_at=at,
            summary=f"权利人撤回授权：{permission_id}（{reason}）", payload={"reason": reason},
        ).event

    # ---- 时段限制 / 解除 ----
    def restrict(self, restriction_id: str, segment_id: str, reason_code: str, at: str,
                 *, uses: list[str] | None = None, windows: list[Window] | None = None,
                 territories: list[str] | None = None, detail_ref: str | None = None,
                 summary: str | None = None) -> dict:
        restriction: dict = {"reason_code": reason_code, "uses": uses or list(C.USES)}
        if windows:
            restriction["time_windows"] = normalize(windows)
        if territories:
            restriction["territories"] = territories
        if detail_ref:
            restriction["detail_ref"] = detail_ref
        return self.store.append(
            event_type=C.EV_RELEASE_RESTRICTED, aggregate_type=C.AGG_SEGMENT,
            aggregate_id=segment_id, occurred_at=at,
            summary=summary or f"限制段落 {segment_id} 的相应时段（{reason_code}）",
            payload={"restriction_id": restriction_id, "restriction": restriction},
        ).event

    def clear_restriction(self, restriction_id: str, segment_id: str, at: str,
                          *, summary: str | None = None) -> dict:
        return self.store.append(
            event_type=C.EV_RELEASE_CLEARED, aggregate_type=C.AGG_SEGMENT,
            aggregate_id=segment_id, occurred_at=at,
            summary=summary or f"解除限制 {restriction_id}",
            payload={"restriction_id": restriction_id},
        ).event

    # ---- 合同 ----
    def execute_contract(self, contract_id: str, session_id: str, at: str,
                         revenue_share: dict[str, float], *,
                         valid_from: str | None = None, valid_until: str | None = None,
                         platform_rate: float = 0.0) -> dict:
        payload: dict = {
            "session_id": session_id, "revenue_share": revenue_share,
            "platform_rate": platform_rate, "valid_from": valid_from or at,
            "valid_until": valid_until,
        }
        return self.store.append(
            event_type=C.EV_CONTRACT_EXECUTED, aggregate_type=C.AGG_CONTRACT,
            aggregate_id=contract_id, occurred_at=at,
            summary=f"签订分成合同 {contract_id}", payload=payload,
        ).event

    # ---- 观众贡献（连麦内容）----
    def receive_contribution(self, contribution_id: str, *, session_id: str,
                             segment_id: str, participant_id: str, material_id: str,
                             windows: list[Window], at: str) -> dict:
        return self.store.append(
            event_type=C.EV_CONTRIBUTION_RECEIVED, aggregate_type=C.AGG_CONTRIBUTION,
            aggregate_id=contribution_id, occurred_at=at,
            summary=f"观众 {participant_id} 连麦贡献入账",
            payload={"session_id": session_id, "segment_id": segment_id,
                     "participant_id": participant_id, "material_id": material_id,
                     "time_windows": normalize(windows)},
        ).event

    # ---- 打赏与结算：以“下单时点有效合同”为准 ----
    def place_tip(self, tip_id: str, *, session_id: str, segment_id: str | None,
                  participant_id: str, amount: int, at: str,
                  order_no: str | None = None, settle: bool = True) -> dict:
        event = self.store.append(
            event_type=C.EV_TIP_PLACED, aggregate_type=C.AGG_TIP,
            aggregate_id=tip_id, occurred_at=at, summary=f"打赏订单 {tip_id}，{amount} 分",
            payload={"session_id": session_id, "segment_id": segment_id,
                     "participant_id": participant_id, "amount": amount,
                     "currency": "CNY", "order_no": order_no or tip_id},
        ).event
        if settle:
            self.settle_tip(tip_id, at=at)
        return event

    def _contract_at(self, state: State, session_id: str, at: datetime) -> dict | None:
        contract = state.effective_contract(session_id, at)
        if contract is None:
            raise RightsLedgerError(
                f"打赏结算失败：{session_id} 在 {at.isoformat()} 没有有效合同，"
                "不得凭当前合同倒推历史订单"
            )
        return contract

    def compute_settlement(self, state: State, tip: dict, at: datetime) -> Settlement:
        """按当时有效合同拆分一笔打赏（金额单位：分）。

        revenue_share：各收款方占打赏总额的比例；未分完的余额归平台，
        即平台抽成 = 总额 - 各方分配之和。platform_rate 仅作合同里的
        抽成约定留痕，余额以实际分平为准，避免四舍五入产生出入。
        """
        contract = self._contract_at(state, tip["session_id"], at)
        share = {k: float(v) for k, v in contract.get("revenue_share", {}).items()}
        if sum(share.values()) > 1 + 1e-9:
            raise RightsLedgerError(f"合同 {contract['id']} 分成比例之和超过 1")
        allocations = [
            {"payee_id": payee, "amount": int(round(tip["amount"] * ratio)),
             "basis": contract["id"]}
            for payee, ratio in sorted(share.items()) if ratio > 0
        ]
        allocated = sum(a["amount"] for a in allocations)
        platform_amount = tip["amount"] - allocated
        if platform_amount < 0:
            raise RightsLedgerError(f"合同 {contract['id']} 分成金额超出打赏总额")
        if platform_amount > 0:
            allocations.append({"payee_id": C.ROLE_PLATFORM,
                                "amount": platform_amount, "basis": contract["id"]})
        return Settlement(allocations=allocations, contract_id=contract["id"],
                          platform_amount=platform_amount)

    def settle_tip(self, tip_id: str, *, at: str) -> dict:
        when = parse_dt(at)
        state = fold(self.store, when)  # 只看下单时点之前的事件：当时有效合同
        tip = state.tips.get(tip_id)
        if tip is None:
            raise RightsLedgerError(f"打赏订单不存在：{tip_id}")
        settlement = self.compute_settlement(state, tip, when)
        return self.store.append(
            event_type=C.EV_REVENUE_ALLOCATED, aggregate_type=C.AGG_REVENUE,
            aggregate_id=f"rev-{tip_id}", occurred_at=at,
            summary=f"按合同 {settlement.contract_id} 结算打赏 {tip_id}",
            payload={"tip_id": tip_id, "session_id": tip["session_id"],
                     "entry_kind": C.ENTRY_INITIAL, "allocations": settlement.allocations,
                     "note": f"以 {at} 有效合同 {settlement.contract_id} 结算"},
        ).event

    def refund_tip(self, tip_id: str, at: str, *, reason: str = "") -> dict:
        """退款：原分账不删不改，追加与原分配相反的冲正分录，再记退款。"""
        before = fold(self.store, parse_dt(at))
        tip = before.tips.get(tip_id)
        if tip is None:
            raise RightsLedgerError(f"打赏订单不存在：{tip_id}")
        if tip["status"] == "REFUNDED":
            raise RightsLedgerError(f"打赏订单已退款：{tip_id}")
        original = [e for e in before.revenue
                    if e.get("tip_id") == tip_id and e["entry_kind"] == C.ENTRY_INITIAL]
        if not original:
            raise RightsLedgerError(f"打赏 {tip_id} 尚未结算，无法冲正")
        reversal = [{"payee_id": e["payee_id"], "amount": abs(e["amount"]),
                     "basis": e.get("contract_id")} for e in original]
        self.store.append(
            event_type=C.EV_REVENUE_ALLOCATED, aggregate_type=C.AGG_REVENUE,
            aggregate_id=f"rev-{tip_id}-refund", occurred_at=at,
            summary=f"退款冲正打赏 {tip_id}",
            payload={"tip_id": tip_id, "session_id": tip["session_id"],
                     "entry_kind": C.ENTRY_REVERSAL, "allocations": reversal,
                     "reason": reason, "note": "退款按原合同分账比例反向冲正"},
        )
        return self.store.append(
            event_type=C.EV_TIP_REFUNDED, aggregate_type=C.AGG_TIP,
            aggregate_id=tip_id, occurred_at=at,
            summary=f"打赏订单退款 {tip_id}（{reason}）", payload={"reason": reason},
        ).event

    def adjust_rights(self, adjustment_id: str, *, session_id: str, at: str,
                      kind: str, allocations: list[dict], tip_id: str | None = None,
                      reason: str = "") -> dict:
        """后续确权：补付 TOP_UP 或冲正 REVERSAL，只追加调整，不动历史分录。"""
        if kind not in C.ADJUSTMENT_KINDS:
            raise RightsLedgerError(f"未知调整类型：{kind}")
        return self.store.append(
            event_type=C.EV_RIGHTS_ADJUSTED, aggregate_type=C.AGG_REVENUE,
            aggregate_id=adjustment_id, occurred_at=at,
            summary=f"确权{'补付' if kind == C.ADJ_TOP_UP else '冲正'} {adjustment_id}（{reason}）",
            payload={"session_id": session_id, "tip_id": tip_id,
                     "adjustment_kind": kind, "allocations": allocations, "reason": reason},
        ).event

    # ---- 回放与二次剪辑：发布前过闸门 ----
    def publish_replay(self, replay_id: str, *, session_id: str,
                       source_segments: list[dict], at: str, territory: str = "CN",
                       version_no: int = 1) -> dict:
        verdict = self.check_release(session_id, C.USE_REPLAY, at, territory=territory,
                                     source_segments=source_segments)
        if verdict["gate"] == C.GATE_BLOCKED:
            raise RightsLedgerError(
                f"回放 {replay_id} 存在整段未授权区间，拒绝发布；受限区间："
                f"{_fmt_blocked(verdict)}"
            )
        return self.store.append(
            event_type=C.EV_REPLAY_PUBLISHED, aggregate_type=C.AGG_REPLAY,
            aggregate_id=replay_id, occurred_at=at,
            summary=f"回放版本 v{version_no} 发布（含 {len(verdict['restricted'])} 处受限时段，"
                    "播放端按时段屏蔽）",
            payload={"session_id": session_id, "version_no": version_no,
                     "source_segments": source_segments},
        ).event

    def take_down_replay(self, replay_id: str, at: str, *, reason: str) -> dict:
        return self.store.append(
            event_type=C.EV_REPLAY_TAKEN_DOWN, aggregate_type=C.AGG_REPLAY,
            aggregate_id=replay_id, occurred_at=at,
            summary=f"回放 {replay_id} 下架：{reason}", payload={"reason": reason},
        ).event

    def propose_cut(self, cut_id: str, *, session_id: str, title: str,
                    source_segments: list[dict], at: str) -> dict:
        return self.store.append(
            event_type=C.EV_CUT_PROPOSED, aggregate_type=C.AGG_CUT,
            aggregate_id=cut_id, occurred_at=at, summary=f"二次剪辑送审：{title}",
            payload={"session_id": session_id, "title": title,
                     "source_segments": source_segments},
        ).event

    def publish_cut(self, cut_id: str, at: str, *, territory: str = "CN") -> dict:
        state = fold(self.store, parse_dt(at))
        cut = state.cuts.get(cut_id)
        if cut is None:
            raise RightsLedgerError(f"剪辑不存在：{cut_id}")
        verdict = self.check_release(cut["session_id"], C.USE_COMMERCIAL_CUT, at,
                                     territory=territory,
                                     source_segments=cut["source_segments"])
        if verdict["gate"] != C.GATE_ALLOWED:
            self.store.append(
                event_type=C.EV_CUT_REJECTED, aggregate_type=C.AGG_CUT,
                aggregate_id=cut_id, occurred_at=at,
                summary=f"商业剪辑 {cut_id} 未通过权益闸门，驳回",
                payload={"reason": f"存在受限/未授权区间：{_fmt_blocked(verdict)}"},
            )
            raise RightsLedgerError(
                f"商业剪辑 {cut_id} 必须整段获准，当前受限：{_fmt_blocked(verdict)}"
            )
        return self.store.append(
            event_type=C.EV_CUT_PUBLISHED, aggregate_type=C.AGG_CUT,
            aggregate_id=cut_id, occurred_at=at,
            summary=f"二次剪辑 {cut_id} 发布", payload={},
        ).event

    # ---- 发布闸门：逐段求权限交集 ----
    def check_release(self, session_id: str, use: str, at: str, *,
                      territory: str = "CN",
                      source_segments: list[dict] | None = None) -> dict:
        when = parse_dt(at)
        state = fold(self.store, when)
        segments = state.session_segments(session_id)
        report = []
        any_allowed = False
        any_blocked = False
        for seg in segments:
            sub = None
            for ss in source_segments or []:
                if ss["segment_id"] == seg["id"]:
                    sub = ss["time_windows"]
            if source_segments is not None and sub is None:
                continue
            v = segment_verdict(state, seg, use, territory=territory, at=when, axis=sub)
            total = windows_len(normalize(sub)) if sub else windows_len(seg["windows"])
            blocked_len = windows_len(normalize([w for ws in v["blockers"].values() for w in ws]))
            allowed_len = windows_len(v["allowed"])
            if allowed_len > 0:
                any_allowed = True
            if blocked_len > 0:
                any_blocked = True
            report.append({
                "segment_id": seg["id"], "title": seg.get("title", ""),
                "total_ms": total, "allowed_ms": allowed_len,
                "restricted_ms": blocked_len,
                "allowed": v["allowed"], "blockers": v["blockers"],
                "materials": v["materials"], "primary_reason": v["primary_reason"],
            })
        if any_blocked and any_allowed:
            gate = C.GATE_PARTIAL
        elif any_blocked:
            gate = C.GATE_BLOCKED
        else:
            gate = C.GATE_ALLOWED
        return {"gate": gate, "use": use, "territory": territory,
                "segments": report,
                "restricted": [r for r in report if r["restricted_ms"] > 0]}


def _fmt_blocked(verdict: dict) -> str:
    parts = []
    for r in verdict["segments"]:
        for reason, ws in r["blockers"].items():
            parts.append(f"{r['segment_id']} {reason} {[(w['start_ms'], w['end_ms']) for w in ws]}")
    return "；".join(parts) or "无"
