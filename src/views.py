"""发布闸门与角色视图。

- 编辑：发布前逐段看到可用于直播/回放/商业剪辑的窗口，发布动作必须过闸门；
- 权利人：核对自己被使用的范围、状态与每笔收入；
- 观众：只看到已获准内容，被遮罩的时段给出“为何暂不可用”，不暴露内部合同细节。
"""

from __future__ import annotations

from datetime import datetime

from . import contracts as C
from .event_store import EventStore, parse_ts
from .rights_ledger import REASON_COPY, RightsLedger, windows_in


def _emit(store: EventStore, event_type: str, aggregate_type: str, aggregate_id: str,
          summary: str, payload: dict, *, occurred_at: str | None = None,
          correlation_id: str | None = None, cause_id: str | None = None) -> dict:
    event = C.make_event(
        event_type, aggregate_type, aggregate_id,
        store.next_version(aggregate_type, aggregate_id), summary, payload,
        occurred_at=occurred_at, correlation_id=correlation_id, cause_id=cause_id)
    store.append(event)
    return event


class PublishingService:
    """回放版本与二次剪辑的发布都从这里过闸。"""

    def __init__(self, store: EventStore, ledger: RightsLedger):
        self.store = store
        self.ledger = ledger

    # ----- 回放版本：遮罩不可用窗口，而不是下架整场 -----

    def publish_replay(self, session_id: str, *, label: str,
                       territories: list[str] | None = None,
                       at: str | None = None,
                       correlation_id: str | None = None) -> dict:
        at = at or C.now_iso()
        plan = self.ledger.replay_plan(session_id, territories=territories,
                                      at=parse_ts(at))
        playable_ms = sum(e - s for seg in plan["segments"] for s, e in
                          [(w[0], w[1]) for w in seg["playable"]])
        if playable_ms == 0:
            raise C.ContractError("没有任何可播放窗口，不能发布回放版本")
        replay_id = f"replay-{session_id}"
        payload = {
            "session_id": session_id,
            "label": label,
            "territories": plan["territories"],
            "segments": plan["segments"],
            "playable_ms": playable_ms,
            "published_at": at,
            "status": "published",
        }
        event = _emit(self.store, "REPLAY_PUBLISHED", "replay_version", replay_id,
                      f"发布回放版本：{label}（{playable_ms} 毫秒可播，其余遮罩）",
                      payload, occurred_at=at, correlation_id=correlation_id)
        self.ledger.reload(self.store.events())
        return event

    def takedown_replay(self, session_id: str, *, reason: str,
                        ranges: list[dict] | None = None, at: str | None = None) -> dict:
        at = at or C.now_iso()
        replay_id = f"replay-{session_id}"
        if replay_id not in self.ledger.replays:
            raise C.ContractError(f"场次 {session_id} 尚无回放版本")
        payload = {"session_id": session_id, "reason": reason, "taken_down_at": at}
        if ranges:
            norm = [C.normalize_range(r) for r in ranges]
            payload["ranges"] = [[s, e] for s, e in norm]
        event = _emit(self.store, "REPLAY_TAKEN_DOWN", "replay_version", replay_id,
                      f"回放下架：{reason}", payload, occurred_at=at)
        self.ledger.reload(self.store.events())
        return event

    # ----- 二次剪辑：候选区间必须全程在权限交集内 -----

    def propose_clip(self, clip_id: str, title: str, ranges: list[dict], *,
                     territories: list[str] | None = None,
                     at: str | None = None) -> dict:
        at = at or C.now_iso()
        checked = self._check_clip_ranges(ranges, territories, parse_ts(at))
        payload = {
            "clip_id": clip_id, "title": title, "territories": territories or [C.WORLDWIDE],
            "ranges": checked, "proposed_at": at, "status": "proposed",
        }
        event = _emit(self.store, "CLIP_PROPOSED", "secondary_clip", clip_id,
                      f"提交商业剪辑送审：{title}", payload, occurred_at=at)
        self.ledger.reload(self.store.events())
        return event

    def _check_clip_ranges(self, ranges: list[dict], territories: list[str] | None,
                           at: datetime) -> list[dict]:
        if not ranges:
            raise C.ContractError("剪辑至少包含一个区间")
        checked = []
        for r in ranges:
            C.normalize_range(r)
            good, blockers = self.ledger.clip_gate(
                r["segment_id"], (r["start_offset_ms"], r["end_offset_ms"]),
                territories=territories, at=at)
            checked.append({
                "segment_id": r["segment_id"],
                "start_offset_ms": r["start_offset_ms"],
                "end_offset_ms": r["end_offset_ms"],
                "clearable": [[s, e] for s, e in good],
                "blockers": [{"reason": b.reason, "windows": [list(w) for w in b.windows],
                              "detail": b.detail} for b in blockers],
            })
        return checked

    def publish_clip(self, clip_id: str, *, at: str | None = None) -> dict:
        at = at or C.now_iso()
        history = self.ledger.clips.get(clip_id)
        if not history:
            raise C.ContractError(f"剪辑 {clip_id} 尚未送审")
        proposal = history[-1]
        if proposal["kind"] != "CLIP_PROPOSED":
            raise C.ContractError(f"剪辑 {clip_id} 当前状态不能发布")
        blockers = [b for r in proposal["ranges"] for b in r["blockers"]]
        if blockers:
            raise C.ContractError(
                "存在未获准窗口，禁止发布：" + "；".join(
                    f"{b['detail'] or b['reason']} {b['windows']}" for b in blockers))
        payload = {"clip_id": proposal["clip_id"], "title": proposal["title"],
                   "territories": proposal["territories"], "ranges": proposal["ranges"],
                   "published_at": at, "status": "published"}
        event = _emit(self.store, "CLIP_PUBLISHED", "secondary_clip", clip_id,
                      f"商业剪辑发布：{proposal['title']}", payload, occurred_at=at,
                      cause_id=proposal["event_id"])
        self.ledger.reload(self.store.events())
        return event

    def reject_clip(self, clip_id: str, reason: str, *, at: str | None = None) -> dict:
        at = at or C.now_iso()
        history = self.ledger.clips.get(clip_id)
        if not history or history[-1]["kind"] != "CLIP_PROPOSED":
            raise C.ContractError(f"剪辑 {clip_id} 没有待审送审记录")
        payload = {"clip_id": history[-1]["clip_id"], "reason": reason,
                   "rejected_at": at, "status": "rejected"}
        event = _emit(self.store, "CLIP_REJECTED", "secondary_clip", clip_id,
                      f"驳回商业剪辑：{reason}", payload, occurred_at=at)
        self.ledger.reload(self.store.events())
        return event


# ---------- 编辑视图 ----------

class EditorView:
    def __init__(self, ledger: RightsLedger):
        self.ledger = ledger

    def segment_card(self, segment_id: str, *, territories: list[str] | None = None) -> dict:
        seg = self.ledger.segments[segment_id]
        matrix = self.ledger.editor_matrix(segment_id, territories=territories)
        return {
            "segment_id": seg.id,
            "title": seg.title,
            "window": list(seg.window),
            "uses": {
                use: {
                    "status": av.status,
                    "playable": [list(w) for w in av.playable],
                    "blockers": [{"reason": b.reason, "windows": [list(w) for w in b.windows],
                                  "detail": b.detail} for b in av.blockers],
                }
                for use, av in matrix.items()
            },
        }

    def pending_consents(self) -> list[dict]:
        return [{"permission_id": p.id, "subject_type": p.subject_type,
                 "subject_id": p.subject_id, "channel": p.channel, "note": p.note,
                 "uses": p.uses} for p in self.ledger.pending_permissions()]


# ---------- 权利人视图 ----------

class HolderView:
    def __init__(self, ledger: RightsLedger):
        self.ledger = ledger

    def usage_report(self, *, participant_id: str | None = None,
                     material_id: str | None = None) -> dict:
        if participant_id is None and material_id is None:
            raise C.ContractError("必须指定 participant_id 或 material_id")
        subject_type = "participant" if participant_id else "material"
        subject_id = participant_id or material_id
        perms = [p for p in self.ledger.permissions.values()
                 if p.subject_type == subject_type and p.subject_id == subject_id]

        segment_ids: set[str] = set()
        if participant_id:
            segment_ids = {s.id for s in self.ledger.segments.values()
                           if participant_id in s.cast}
        else:
            segment_ids = {sid for m in self.ledger.materials.values()
                           if m.id == material_id for sid in m.links}

        restrictions = [{
            "restriction_id": r.id, "segment_id": r.segment_id,
            "window": list(r.window), "reason": r.reason, "uses": r.uses,
            "active": r.active, "note": r.note,
        } for r in self.ledger.restrictions.values() if r.segment_id in segment_ids]

        replays, clips = [], []
        for replay_id, versions in self.ledger.replays.items():
            latest = versions[-1]
            for seg in latest.get("segments", []):
                if seg["segment_id"] in segment_ids:
                    replays.append({"replay_id": replay_id, "kind": latest["kind"],
                                    "label": latest.get("label"),
                                    "segment": seg})
        for clip_id, history in self.ledger.clips.items():
            latest = history[-1]
            if any(r["segment_id"] in segment_ids for r in latest.get("ranges", [])):
                clips.append({"clip_id": clip_id, "status": latest.get("status"),
                              "title": latest.get("title"),
                              "ranges": [r for r in latest["ranges"]
                                         if r["segment_id"] in segment_ids]})

        return {
            "subject_type": subject_type,
            "subject_id": subject_id,
            "name": self.ledger._subject_name(subject_type, subject_id),
            "permissions": [{
                "permission_id": p.id, "status": p.status, "channel": p.channel,
                "uses": p.uses, "territories": p.territories,
                "valid_from": p.valid_from, "expires_at": p.expires_at,
            } for p in perms],
            "segments": sorted(segment_ids),
            "restrictions": restrictions,
            "replay_usages": replays,
            "clip_usages": clips,
        }


# ---------- 观众视图 ----------

class AudienceView:
    """只返回已获准内容；遮罩窗口给出稳定中文口径，不暴露合同与识别细节。"""

    def __init__(self, ledger: RightsLedger, territory: str = C.WORLDWIDE):
        self.ledger = ledger
        self.territory = territory

    def _territories(self) -> list[str]:
        return [C.WORLDWIDE] if self.territory == C.WORLDWIDE else [self.territory]

    def replay_feed(self, session_id: str) -> dict | None:
        versions = self.ledger.replays.get(f"replay-{session_id}")
        if not versions:
            return None
        latest = versions[-1]
        if latest["kind"] == "REPLAY_TAKEN_DOWN":
            return {"session_id": session_id, "status": "unavailable",
                    "message": REASON_COPY.get(latest.get("reason", C.REASON_TAKEDOWN),
                                               "回放暂时无法提供，请稍后再试")}
        territories = latest.get("territories", [C.WORLDWIDE])
        if self.territory not in territories and C.WORLDWIDE not in territories:
            return {"session_id": session_id, "status": "territory_blocked",
                    "message": REASON_COPY[C.REASON_TERRITORY]}
        segments = []
        for seg in latest["segments"]:
            notes = [{"window": m["window"],
                      "message": REASON_COPY.get(m["reason"], "该时段暂时无法播放")}
                     for m in seg["masked"]]
            segments.append({
                "segment_id": seg["segment_id"], "title": seg["title"],
                "status": "available" if seg["status"] == "available"
                          else ("partial" if seg["playable"] else "unavailable"),
                "playable": seg["playable"], "unavailable_notes": notes,
            })
        return {"session_id": session_id, "status": "available",
                "label": latest.get("label"), "segments": segments}

    def clip_feed(self) -> list[dict]:
        feed = []
        for clip_id, history in self.ledger.clips.items():
            latest = history[-1]
            if latest["kind"] != "CLIP_PUBLISHED":
                continue
            territories = latest.get("territories", [C.WORLDWIDE])
            if self.territory not in territories and C.WORLDWIDE not in territories:
                continue
            feed.append({"clip_id": clip_id, "title": latest["title"],
                         "ranges": [{"segment_id": r["segment_id"],
                                     "start_offset_ms": r["start_offset_ms"],
                                     "end_offset_ms": r["end_offset_ms"]}
                                    for r in latest["ranges"]]})
        return feed

    def segment_notice(self, segment_id: str) -> dict:
        """观众点开某段：可播窗口与不可用原因（只给口径化说明）。"""
        av = self.ledger.segment_availability(
            segment_id, C.USE_REPLAY, territories=self._territories())
        return {
            "segment_id": segment_id,
            "status": av.status,
            "playable": [list(w) for w in av.playable],
            "unavailable_notes": [
                {"window": list(w),
                 "message": REASON_COPY.get(b.reason, "该时段暂时无法播放")}
                for b in av.blockers for w in windows_in(
                    self.ledger.segments[segment_id].window, b.windows)],
        }
