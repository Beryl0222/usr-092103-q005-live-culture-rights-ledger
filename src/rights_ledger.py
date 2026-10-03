"""权益账读模型：从不可变事件重放出场次、段落、授权、限制与发布状态。

所有时间窗口统一使用“相对整场直播的毫秒偏移”的闭开区间 [start, end)。
多个素材/参与者组合时，权限只能取交集；缺口按窗口给出原因，
而不是把整场节目一刀切下架。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from . import contracts as C
from .event_store import parse_ts

# 限制原因 → 观众端中文说明（观众端只展示获准内容，但可知道为何暂不可用）
REASON_COPY = {
    C.REASON_MINOR: "该时段有未成年人连麦，监护人书面同意补齐前暂不提供回放",
    C.REASON_MUSIC_UNKNOWN: "该时段背景音乐权利尚在识别确认中，暂时无法播放",
    C.REASON_RIGHTS_EXPIRED: "该时段所依据的授权已到期，续权完成后恢复",
    C.REASON_PENDING_CONSENT: "该时段口头同意尚待书面确认，确认后恢复",
    C.REASON_DECLINED: "权利人未同意该用途，该时段不予提供",
    C.REASON_CONSENT_MISSING: "该时段缺少参与者授权，暂不可用",
    C.REASON_UNLICENSED_MATERIAL: "该时段素材尚未取得许可，暂不可用",
    C.REASON_USE_NOT_LICENSED: "现有许可不含该用途（如商业剪辑）",
    C.REASON_TERRITORY: "现有许可不含当前地区",
    C.REASON_TAKEDOWN: "应权利人要求，该时段暂时下架",
}

# 面向编辑/权利人的原因说明
REASON_COPY_INTERNAL = {
    **REASON_COPY,
    C.REASON_MINOR: "未成年人连麦：缺监护人书面同意（minor_in_mic）",
    C.REASON_MUSIC_UNKNOWN: "背景音乐识别不确定（music_unidentified）",
}

Window = tuple[int, int]


# ---------- 区间运算 ----------

def _intersect(a: Window, b: Window) -> Window | None:
    s, e = max(a[0], b[0]), min(a[1], b[1])
    return (s, e) if s < e else None


def intersect_windows(lists: list[list[Window]]) -> list[Window]:
    """多组窗口逐组取交集（权限组合只能取交集）。"""
    if not lists:
        return []
    result = lists[0]
    for other in lists[1:]:
        merged: list[Window] = []
        for w in result:
            for o in other:
                hit = _intersect(w, o)
                if hit:
                    merged.append(hit)
        result = _union(merged)
        if not result:
            break
    return result


def _union(windows: list[Window]) -> list[Window]:
    if not windows:
        return []
    ordered = sorted(windows)
    merged = [ordered[0]]
    for s, e in ordered[1:]:
        ls, le = merged[-1]
        if s <= le:
            merged[-1] = (ls, max(le, e))
        else:
            merged.append((s, e))
    return merged


def subtract_windows(base: list[Window], holes: list[Window]) -> list[Window]:
    """从 base 中挖掉 holes。"""
    cuts = _union([h for b in base for h in holes if _intersect(b, h)])
    result: list[Window] = []
    for bs, be in base:
        cursor = bs
        for hs, he in cuts:
            hs, he = max(hs, bs), min(he, be)
            if hs <= cursor:
                cursor = max(cursor, he)
            elif hs < be:
                result.append((cursor, hs))
                cursor = he
        if cursor < be:
            result.append((cursor, be))
    return _union(result)


def _merge_blockers(blockers: list[Blocker]) -> list[Blocker]:
    """同一原因的窗口合并，便于逐段展示。"""
    by_reason: dict[str, list[Window]] = {}
    detail_by_reason: dict[str, list[str]] = {}
    for b in blockers:
        by_reason.setdefault(b.reason, []).extend(b.windows)
        if b.detail:
            detail_by_reason.setdefault(b.reason, []).append(b.detail)
    return [Blocker(r, _union(ws), "；".join(dict.fromkeys(detail_by_reason.get(r, []))))
            for r, ws in by_reason.items()]


def windows_in(scope: Window, windows: list[Window] | None) -> list[Window]:
    """把全局窗口裁剪进 scope；windows 为空表示覆盖整个 scope。"""
    if not windows:
        return [scope]
    out = []
    for w in windows:
        hit = _intersect(scope, w)
        if hit:
            out.append(hit)
    return _union(out)


# ---------- 读模型实体 ----------

@dataclass
class Segment:
    id: str
    session_id: str
    index: int
    title: str
    window: Window
    cast: list[str] = field(default_factory=list)
    cast_windows: dict[str, list[Window]] = field(default_factory=dict)


@dataclass
class Participant:
    id: str
    name: str
    role: str
    is_minor: bool = False


@dataclass
class Material:
    id: str
    title: str
    kind: str
    right_holder: str | None = None
    music_detection: str = "confirmed"          # confirmed | uncertain
    links: dict[str, list[Window]] = field(default_factory=dict)  # segment_id -> 窗口


@dataclass
class Permission:
    id: str
    subject_type: str                           # participant | material
    subject_id: str
    status: str = C.CONSENT_PENDING
    channel: str | None = None
    uses: list[str] = field(default_factory=list)
    territories: list[str] = field(default_factory=list)
    valid_from: str | None = None
    expires_at: str | None = None
    time_ranges: list[Window] = field(default_factory=list)
    note: str = ""


@dataclass
class Restriction:
    id: str
    segment_id: str
    window: Window
    reason: str
    uses: list[str]                             # 受限用途；空表示全部用途
    territories: list[str] = field(default_factory=list)  # 空表示全部地区
    active: bool = True
    applied_at: str | None = None
    lifted_at: str | None = None
    note: str = ""


@dataclass
class Blocker:
    reason: str
    windows: list[Window]
    detail: str = ""


@dataclass
class Availability:
    use: str
    status: str                                 # available | partial | blocked
    playable: list[Window]
    blockers: list[Blocker]


class RightsLedger:
    """从事件流重放的只读权益账。"""

    def __init__(self, events: list[dict]):
        self.reload(events)

    @property
    def as_of(self) -> datetime:
        """读模型的“当前时点”= 已见事件的最晚时间（事件时间，不依赖墙钟）。"""
        if not self._events:
            return datetime.now().astimezone()
        return parse_ts(max(e["occurred_at"] for e in self._events))

    def reload(self, events: list[dict]) -> "RightsLedger":
        """从完整事件流（重新）构建读模型；事件不可变，随时可整体重放。"""
        self._events = sorted((dict(e) for e in events),
                              key=lambda e: (e["occurred_at"], e["event_id"]))
        self.sessions: dict[str, dict] = {}
        self.segments: dict[str, Segment] = {}
        self.participants: dict[str, Participant] = {}
        self.materials: dict[str, Material] = {}
        self.permissions: dict[str, Permission] = {}
        self.restrictions: dict[str, Restriction] = {}
        self.contributions: list[dict] = []
        self.tips: dict[str, dict] = {}
        self.contracts: dict[str, list[dict]] = {}
        self.allocations: dict[str, list[dict]] = {}   # tip_order_id -> 分配/调整
        self.replays: dict[str, list[dict]] = {}
        self.clips: dict[str, list[dict]] = {}
        self._replay()

    # ----- 重放 -----

    def _replay(self) -> None:
        self.sessions = {}
        self.segments = {}
        self.participants = {}
        self.materials = {}
        self.permissions = {}
        self.restrictions = {}
        self.contributions = []
        self.tips = {}
        self.contracts = {}
        self.allocations = {}
        self.replays = {}
        self.clips = {}
        for e in self._events:
            p = e.get("payload", {})
            kind = e["event_type"]
            if kind in ("SESSION_SCHEDULED", "SESSION_STARTED", "SESSION_ENDED"):
                sess = self.sessions.setdefault(e["aggregate_id"], {"id": e["aggregate_id"]})
                sess["last_event"] = kind
                sess.update(p)
            elif kind == "SEGMENT_REGISTERED":
                self.segments[e["aggregate_id"]] = Segment(
                    id=e["aggregate_id"], session_id=p["session_id"], index=p["index"],
                    title=p["title"], window=(p["start_offset_ms"], p["end_offset_ms"]),
                )
            elif kind == "PARTICIPANT_REGISTERED":
                self.participants[e["aggregate_id"]] = Participant(
                    id=e["aggregate_id"], name=p["name"], role=p["role"],
                    is_minor=p.get("is_minor", False))
            elif kind == "MATERIAL_REGISTERED":
                self.materials[e["aggregate_id"]] = Material(
                    id=e["aggregate_id"], title=p["title"], kind=p["kind"],
                    right_holder=p.get("right_holder"),
                    music_detection=p.get("music_detection", "confirmed"))
            elif kind == "MATERIAL_VERIFIED":
                if e["aggregate_id"] in self.materials:
                    mat = self.materials[e["aggregate_id"]]
                    mat.music_detection = p.get("music_detection", mat.music_detection)
                    if p.get("right_holder"):
                        mat.right_holder = p["right_holder"]
            elif kind == "MATERIAL_LINKED":
                mat = self.materials[e["aggregate_id"]]
                for link in p.get("ranges", []):
                    mat.links.setdefault(link["segment_id"], []).append(
                        (link["start_offset_ms"], link["end_offset_ms"]))
            elif kind == "SEGMENT_CAST":
                seg = self.segments[e["aggregate_id"]]
                for member in p["participants"]:
                    pid = member["participant_id"]
                    if pid not in seg.cast:
                        seg.cast.append(pid)
                    ranges = [tuple(r) for r in member.get("time_ranges", [])]
                    if ranges:
                        seg.cast_windows.setdefault(pid, []).extend(ranges)
            elif kind in ("CONSENT_CAPTURED", "PERMISSION_GRANTED", "PERMISSION_EXPIRED"):
                self._apply_permission_event(e["aggregate_id"], kind, p)
            elif kind in ("RESTRICTION_APPLIED", "RELEASE_RESTRICTED"):
                self.restrictions[e["aggregate_id"]] = Restriction(
                    id=e["aggregate_id"], segment_id=p["segment_id"],
                    window=(p["start_offset_ms"], p["end_offset_ms"]), reason=p["reason"],
                    uses=p.get("uses", []), territories=p.get("territories", []),
                    applied_at=e["occurred_at"], note=p.get("note", ""))
            elif kind == "RESTRICTION_LIFTED":
                if e["aggregate_id"] in self.restrictions:
                    self.restrictions[e["aggregate_id"]].active = False
                    self.restrictions[e["aggregate_id"]].lifted_at = e["occurred_at"]
            elif kind == "CONTRIBUTIONS_RECORDED":
                for c in p.get("contributions", []):
                    self.contributions.append(
                        {"batch_id": e["aggregate_id"], "session_id": p["session_id"], **c})
            elif kind == "CONTRACT_AGREED":
                self.contracts.setdefault(e["aggregate_id"], []).append(
                    {"contract_id": e["aggregate_id"], "version": e["version"], **p})
            elif kind == "TIP_PLACED":
                self.tips[e["aggregate_id"]] = {"id": e["aggregate_id"], "status": "placed",
                                                "event_id": e["event_id"], **p}
            elif kind == "TIP_REFUNDED":
                self.tips[e["aggregate_id"]]["status"] = "refunded"
                self.tips[e["aggregate_id"]]["refund"] = p
            elif kind in ("REVENUE_ALLOCATED", "RIGHTS_ADJUSTED"):
                self.allocations.setdefault(p["tip_order_id"], []).append(
                    {"event_id": e["event_id"], "kind_event": kind, **p})
            elif kind in ("REPLAY_PUBLISHED", "REPLAY_TAKEN_DOWN"):
                self.replays.setdefault(e["aggregate_id"], []).append(
                    {"event_id": e["event_id"], "kind": kind, "version": e["version"], **p})
            elif kind in ("CLIP_PROPOSED", "CLIP_PUBLISHED", "CLIP_REJECTED"):
                self.clips.setdefault(e["aggregate_id"], []).append(
                    {"event_id": e["event_id"], "kind": kind, "version": e["version"], **p})

    def _apply_permission_event(self, agg_id: str, kind: str, p: dict) -> None:
        perm = self.permissions.get(agg_id)
        if kind == "CONSENT_CAPTURED":
            self.permissions[agg_id] = Permission(
                id=agg_id, subject_type=p["subject_type"], subject_id=p["subject_id"],
                status=p.get("status", C.CONSENT_GRANTED), channel=p.get("channel"),
                uses=list(p.get("uses", [])), territories=list(p.get("territories", [])),
                valid_from=p.get("valid_from"), expires_at=p.get("expires_at"),
                time_ranges=[tuple(r) for r in p.get("time_ranges", [])],
                note=p.get("note", ""))
            return
        if perm is None:
            raise C.ContractError(f"授权事件 {kind} 缺少对应的 CONSENT_CAPTURED：{agg_id}")
        if kind == "PERMISSION_GRANTED":
            perm.status = C.CONSENT_GRANTED
            for key in ("uses", "territories"):
                if key in p:
                    setattr(perm, key, list(p[key]))
            for key in ("channel", "valid_from", "expires_at", "note"):
                if key in p:
                    setattr(perm, key, p[key])
            if p.get("time_ranges") is not None:
                perm.time_ranges = [tuple(r) for r in p["time_ranges"]]
        elif kind == "PERMISSION_EXPIRED":
            perm.status = "expired"
            if p.get("expires_at"):
                perm.expires_at = p["expires_at"]

    # ----- 查询 -----

    def session_segments(self, session_id: str) -> list[Segment]:
        return sorted((s for s in self.segments.values() if s.session_id == session_id),
                      key=lambda s: s.index)

    def _active_permissions(self, subject_id: str, at: datetime,
                           allow_pending: bool = False) -> list[Permission]:
        out = []
        for perm in self.permissions.values():
            if perm.subject_id != subject_id:
                continue
            if perm.status == C.CONSENT_PENDING and not allow_pending:
                continue
            if perm.status not in (C.CONSENT_GRANTED, C.CONSENT_PENDING):
                continue
            if perm.valid_from and at < parse_ts(perm.valid_from):
                continue
            if perm.expires_at and at >= parse_ts(perm.expires_at):
                continue
            out.append(perm)
        return out

    @staticmethod
    def _covers_territory(perm: Permission, territories: list[str]) -> bool:
        if not perm.territories or C.WORLDWIDE in perm.territories:
            return True
        return all(t in perm.territories for t in territories)

    def _source_appearance(self, seg: Segment) -> list[tuple[str, str, list[Window]]]:
        """返回段内每个来源（参与者/素材）及其出现窗口。"""
        sources: list[tuple[str, str, list[Window]]] = []
        for pid in seg.cast:
            appear = windows_in(seg.window, seg.cast_windows.get(pid))
            sources.append(("participant", pid, appear))
        for mat in self.materials.values():
            if seg.id in mat.links:
                appear = windows_in(seg.window, mat.links[seg.id])
                if appear:
                    sources.append(("material", mat.id, appear))
        return sources

    def _covered(self, subject_id: str, appearance: list[Window], use: str,
                 territories: list[str], at: datetime,
                 allow_pending: bool = False) -> tuple[list[Window], list[Permission]]:
        """该来源在指定用途/地区/时点下被许可覆盖的窗口，以及生效许可。"""
        valid = [p for p in self._active_permissions(subject_id, at, allow_pending)
                 if use in p.uses and self._covers_territory(p, territories)]
        if not valid:
            return [], []
        licensed_ranges = _union([r for p in valid for r in (p.time_ranges or [])])
        scoped = windows_in((min(a[0] for a in appearance), max(a[1] for a in appearance)),
                            licensed_ranges) if licensed_ranges else appearance
        covered = intersect_windows([_union(appearance), _union(scoped)])
        return covered, valid

    def _gap_reason(self, subject_type: str, subject_id: str, use: str,
                    territories: list[str], at: datetime) -> str:
        related = [p for p in self.permissions.values() if p.subject_id == subject_id]
        if not related:
            return C.REASON_UNLICENSED_MATERIAL if subject_type == "material" else C.REASON_CONSENT_MISSING
        if any(p.status == C.CONSENT_DECLINED for p in related):
            return C.REASON_DECLINED
        if any(p.status == C.CONSENT_PENDING for p in related):
            return C.REASON_PENDING_CONSENT
        # 有授权记录但当前用不了：依次判别到期/用途/地区
        live_any = [p for p in related if p.status in (C.CONSENT_GRANTED, "expired")]
        expired = [p for p in live_any if p.status == "expired"
                   or (p.expires_at and at >= parse_ts(p.expires_at))]
        if expired and all(
                (p.status == "expired" or (p.expires_at and at >= parse_ts(p.expires_at)))
                for p in live_any):
            return C.REASON_RIGHTS_EXPIRED
        if use not in {u for p in related for u in p.uses}:
            return C.REASON_USE_NOT_LICENSED
        if territories and not all(
                (not p.territories or C.WORLDWIDE in p.territories
                 or all(t in p.territories for t in territories))
                for p in live_any):
            return C.REASON_TERRITORY
        return C.REASON_PENDING_CONSENT

    def segment_availability(self, segment_id: str, use: str, *,
                             territories: list[str] | None = None,
                             at: datetime | None = None) -> Availability:
        seg = self.segments[segment_id]
        # 默认按事件流最新时点判定（确权状态随事件推进，事后追认可覆盖直播记录）；
        # 发布闸门会显式传入发布时点，以判断授权是否在发布前到期。
        at = at or self.as_of
        territories = territories or [C.WORLDWIDE]
        scope = seg.window
        blockers: list[Blocker] = []
        playable: list[Window] = [scope]
        sources = self._source_appearance(seg)
        # 直播已经发生：直播用途承认现场口头同意（仍登记为待确认，不自动升级为回放/剪辑权）
        allow_pending = use == C.USE_LIVE

        for subject_type, sid, appearance in sources:
            # 未成年人与音乐识别不确定由下方派生限制统一归口，避免重复原因
            if use in (C.USE_REPLAY, C.USE_CLIP):
                if subject_type == "participant" and self.participants[sid].is_minor:
                    continue
                if subject_type == "material" and self.materials[sid].music_detection == "uncertain":
                    continue
            covered, _ = self._covered(sid, appearance, use, territories, at,
                                       allow_pending=allow_pending)
            gaps = subtract_windows(_union(appearance), covered)
            if gaps:
                reason = self._gap_reason(subject_type, sid, use, territories, at)
                name = self._subject_name(subject_type, sid)
                blockers.append(Blocker(reason, gaps, f"来源「{name}」未覆盖"))
            playable = subtract_windows(playable, gaps)

        # 派生限制（事后无法撤回直播，只约束回放与商业剪辑）：
        # 1) 未成年人连麦：监护人书面同意覆盖前限制其出现时段；
        # 2) 背景音乐识别不确定：识别结论明确前整段出现窗口限制。
        if use in (C.USE_REPLAY, C.USE_CLIP):
            for subject_type, sid, appearance in sources:
                appear = _union(appearance)
                if subject_type == "participant" and self.participants[sid].is_minor:
                    guardian = [p for p in self._active_permissions(sid, at)
                                if p.channel == C.CHANNEL_GUARDIAN
                                and use in p.uses
                                and self._covers_territory(p, territories)]
                    cleared_ranges = _union([r for p in guardian for r in (p.time_ranges or [])])
                    cleared = (intersect_windows([appear, cleared_ranges])
                               if cleared_ranges else [])
                    gaps = subtract_windows(appear, cleared)
                    if gaps:
                        playable = subtract_windows(playable, gaps)
                        blockers.append(Blocker(
                            C.REASON_MINOR, gaps,
                            f"未成年人「{self._subject_name('participant', sid)}」连麦，"
                            "缺监护人书面同意"))
                elif (subject_type == "material"
                      and self.materials[sid].music_detection == "uncertain"):
                    playable = subtract_windows(playable, appear)
                    blockers.append(Blocker(
                        C.REASON_MUSIC_UNKNOWN, appear,
                        f"素材「{self._subject_name('material', sid)}」背景音乐识别不确定"))

        for restr in self.restrictions.values():
            if not restr.active or restr.segment_id != segment_id:
                continue
            if restr.uses and use not in restr.uses:
                continue
            if restr.territories and not all(t in restr.territories for t in territories):
                continue
            hit = windows_in(scope, [restr.window])
            if hit:
                playable = subtract_windows(playable, hit)
                blockers.append(Blocker(restr.reason, hit, restr.note))

        if not playable:
            status = "blocked"
        elif playable == [scope]:
            status = "available"
        else:
            status = "partial"
        return Availability(use=use, status=status, playable=_union(playable),
                            blockers=_merge_blockers(blockers))

    def _subject_name(self, subject_type: str, subject_id: str) -> str:
        if subject_type == "participant" and subject_id in self.participants:
            return self.participants[subject_id].name
        if subject_type == "material" and subject_id in self.materials:
            return self.materials[subject_id].title
        return subject_id

    def editor_matrix(self, segment_id: str, *, territories: list[str] | None = None,
                      at: datetime | None = None) -> dict[str, Availability]:
        """编辑发布前看到：该片段可用于直播、回放还是商业剪辑。"""
        return {use: self.segment_availability(segment_id, use, territories=territories, at=at)
                for use in C.USES}

    def clip_gate(self, segment_id: str, window: Window, *,
                  territories: list[str] | None = None, at: datetime | None = None
                  ) -> tuple[list[Window], list[Blocker]]:
        """商业剪辑闸门：候选窗口内必须全程可剪，返回可剪窗口与阻断原因。"""
        av = self.segment_availability(
            segment_id, C.USE_CLIP, territories=territories, at=at)
        good = windows_in(window, av.playable)
        bad = subtract_windows(windows_in(window, [window]), good)
        blockers = [Blocker(b.reason, windows_in(window, b.windows), b.detail)
                    for b in av.blockers]
        blockers = [b for b in blockers if b.windows]
        if bad and not blockers:
            blockers = [Blocker(C.REASON_USE_NOT_LICENSED, bad, "窗口不在许可交集内")]
        return good, blockers

    def replay_plan(self, session_id: str, *, territories: list[str] | None = None,
                    at: datetime | None = None) -> dict:
        """生成回放发布计划：逐段给出可播窗口与遮罩窗口及主因。"""
        at = at or datetime.now().astimezone()
        territories = territories or [C.WORLDWIDE]
        segments = []
        for seg in self.session_segments(session_id):
            av = self.segment_availability(
                seg.id, C.USE_REPLAY, territories=territories, at=at)
            masked = subtract_windows([seg.window], av.playable)
            segments.append({
                "segment_id": seg.id,
                "title": seg.title,
                "window": list(seg.window),
                "playable": [list(w) for w in av.playable],
                "masked": [{"window": list(w), "reason": self._mask_reason(av.blockers, w)}
                           for w in masked],
                "status": av.status,
            })
        return {"session_id": session_id, "territories": territories, "segments": segments}

    @staticmethod
    def _mask_reason(blockers: list[Blocker], window: Window) -> str:
        for b in blockers:
            if windows_in(window, b.windows):
                return b.reason
        return C.REASON_CONSENT_MISSING

    def pending_permissions(self) -> list[Permission]:
        """待确认记录（直播口头同意等）。"""
        return [p for p in self.permissions.values() if p.status == C.CONSENT_PENDING]
