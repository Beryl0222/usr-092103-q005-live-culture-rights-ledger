"""节目权益账应用门面：命令侧统一在这里落事件，读侧用 RightsLedger 重放。

用法：每个业务动作都是一次不可变事件；口头同意落 pending、授权到期落 expired，
任何纠偏都追加新事件而不是改旧事件。
"""

from __future__ import annotations

from typing import Any

from . import contracts as C
from .event_store import EventStore
from .rights_ledger import RightsLedger


class LedgerService:
    def __init__(self, store: EventStore | None = None):
        self.store = store or EventStore()
        self.ledger = RightsLedger(self.store.events())

    # ---- 内部：发事件并刷新读模型 ----

    def _emit(self, event_type: str, aggregate_type: str, aggregate_id: str,
              summary: str, payload: dict[str, Any] | None = None, *,
              occurred_at: str | None = None, event_id: str | None = None,
              correlation_id: str | None = None, cause_id: str | None = None) -> dict:
        event = C.make_event(
            event_type, aggregate_type, aggregate_id,
            self.store.next_version(aggregate_type, aggregate_id),
            summary, payload, occurred_at=occurred_at, event_id=event_id,
            correlation_id=correlation_id, cause_id=cause_id)
        self.store.append(event)
        self.ledger.reload(self.store.events())
        return event

    # ---- 场次 ----

    def schedule_session(self, session_id: str, title: str, scheduled_start: str,
                         *, occurred_at: str | None = None) -> dict:
        return self._emit("SESSION_SCHEDULED", "live_session", session_id,
                          f"排期直播：{title}",
                          {"title": title, "scheduled_start": scheduled_start},
                          occurred_at=occurred_at)

    def start_session(self, session_id: str, started_at: str) -> dict:
        return self._emit("SESSION_STARTED", "live_session", session_id,
                          "直播开始", {"started_at": started_at}, occurred_at=started_at)

    def end_session(self, session_id: str, ended_at: str) -> dict:
        return self._emit("SESSION_ENDED", "live_session", session_id,
                          "直播结束", {"ended_at": ended_at}, occurred_at=ended_at)

    # ---- 段落 ----

    def register_segment(self, segment_id: str, session_id: str, index: int,
                         title: str, start_ms: int, end_ms: str | int,
                         *, occurred_at: str | None = None) -> dict:
        C.normalize_range({"start_offset_ms": start_ms, "end_offset_ms": end_ms})
        return self._emit("SEGMENT_REGISTERED", "program_segment", segment_id,
                          f"登记节目段落：{title}",
                          {"session_id": session_id, "index": index, "title": title,
                           "start_offset_ms": start_ms, "end_offset_ms": end_ms},
                          occurred_at=occurred_at)

    # ---- 参与者 / 素材 ----

    def register_participant(self, participant_id: str, name: str, role: str, *,
                             is_minor: bool = False, occurred_at: str | None = None) -> dict:
        return self._emit("PARTICIPANT_REGISTERED", "participant", participant_id,
                          f"登记参与者：{name}（{role}）",
                          {"name": name, "role": role, "is_minor": is_minor},
                          occurred_at=occurred_at)

    def cast_participant(self, segment_id: str, participants: list[dict],
                         *, occurred_at: str | None = None) -> dict:
        """participants: [{participant_id, role, time_ranges?: [[s,e],...]}]"""
        for m in participants:
            for r in m.get("time_ranges", []):
                C.normalize_range({"start_offset_ms": r[0], "end_offset_ms": r[1]})
        return self._emit("SEGMENT_CAST", "program_segment", segment_id,
                          "记录段落参与者及出现时段",
                          {"participants": [
                              {"participant_id": m["participant_id"],
                               "role": m.get("role", "guest"),
                               "time_ranges": [list(r) for r in m.get("time_ranges", [])]}
                              for m in participants]},
                          occurred_at=occurred_at)

    def register_material(self, material_id: str, title: str, kind: str, *,
                          right_holder: str | None = None,
                          music_detection: str = "confirmed",
                          occurred_at: str | None = None) -> dict:
        if music_detection not in ("confirmed", "uncertain"):
            raise C.ContractError("music_detection 只能是 confirmed 或 uncertain")
        return self._emit("MATERIAL_REGISTERED", "material_source", material_id,
                          f"登记素材来源：{title}",
                          {"title": title, "kind": kind, "right_holder": right_holder,
                           "music_detection": music_detection},
                          occurred_at=occurred_at)

    def link_material(self, material_id: str, ranges: list[dict], *,
                      occurred_at: str | None = None) -> dict:
        """ranges: [{segment_id, start_offset_ms, end_offset_ms}]"""
        for r in ranges:
            C.normalize_range(r)
        return self._emit("MATERIAL_LINKED", "material_source", material_id,
                          f"素材 {material_id} 关联到节目时段", {"ranges": ranges},
                          occurred_at=occurred_at)

    def verify_material(self, material_id: str, *, music_detection: str | None = None,
                        right_holder: str | None = None,
                        occurred_at: str | None = None) -> dict:
        """背景音乐识别结论：uncertain（识别不确定）→ confirmed（已确认）。"""
        if music_detection is not None and music_detection not in ("confirmed", "uncertain"):
            raise C.ContractError("music_detection 只能是 confirmed 或 uncertain")
        return self._emit("MATERIAL_VERIFIED", "material_source", material_id,
                          "素材权利/音乐识别核实更新",
                          {"music_detection": music_detection,
                           "right_holder": right_holder},
                          occurred_at=occurred_at)

    # ---- 同意与授权 ----

    def capture_consent(self, permission_id: str, subject_type: str, subject_id: str,
                        *, channel: str, uses: list[str] | None = None,
                        territories: list[str] | None = None,
                        valid_from: str | None = None, expires_at: str | None = None,
                        time_ranges: list[tuple[int, int]] | None = None,
                        status: str | None = None, note: str = "",
                        occurred_at: str | None = None) -> dict:
        """登记同意。口头同意一律落 pending_confirmation（直播中只能待确认）。"""
        if channel == C.CHANNEL_ORAL:
            status = C.CONSENT_PENDING
            note = note or "直播现场口头同意，待书面确认"
        elif channel == C.CHANNEL_GUARDIAN:
            status = status or C.CONSENT_GRANTED
        else:
            status = status or C.CONSENT_GRANTED
        if status == C.CONSENT_GRANTED and not uses:
            raise C.ContractError("已授予的许可必须声明用途 uses")
        ranges = [list(r) for r in (time_ranges or [])]
        for r in ranges:
            C.normalize_range({"start_offset_ms": r[0], "end_offset_ms": r[1]})
        return self._emit("CONSENT_CAPTURED", "usage_permission", permission_id,
                          f"登记{('待确认' if status == C.CONSENT_PENDING else '')}"
                          f"同意：{subject_type}:{subject_id}",
                          {"subject_type": subject_type, "subject_id": subject_id,
                           "channel": channel, "status": status, "uses": uses or [],
                           "territories": territories or [], "valid_from": valid_from,
                           "expires_at": expires_at, "time_ranges": ranges, "note": note},
                          occurred_at=occurred_at)

    def confirm_consent(self, permission_id: str, *, uses: list[str],
                        territories: list[str] | None = None,
                        valid_from: str | None = None, expires_at: str | None = None,
                        time_ranges: list[tuple[int, int]] | None = None,
                        occurred_at: str | None = None) -> dict:
        """口头同意经书面追认后转授予。"""
        perm = self.ledger.permissions[permission_id]
        ranges = [list(r) for r in (time_ranges or perm.time_ranges)]
        return self._emit("PERMISSION_GRANTED", "usage_permission", permission_id,
                          f"口头同意完成书面确认：{perm.subject_id}",
                          {"subject_type": perm.subject_type, "subject_id": perm.subject_id,
                           "channel": C.CHANNEL_WRITTEN, "uses": uses,
                           "territories": territories if territories is not None
                           else perm.territories,
                           "valid_from": valid_from or perm.valid_from,
                           "expires_at": expires_at if expires_at is not None
                           else perm.expires_at,
                           "time_ranges": ranges},
                          occurred_at=occurred_at)

    def expire_permission(self, permission_id: str, expires_at: str, *,
                          occurred_at: str | None = None) -> dict:
        return self._emit("PERMISSION_EXPIRED", "usage_permission", permission_id,
                          f"授权到期：{permission_id}",
                          {"expires_at": expires_at}, occurred_at=occurred_at or expires_at)

    # ---- 时间段限制 / 解除 ----

    def apply_restriction(self, restriction_id: str, segment_id: str,
                          start_ms: int, end_ms: int, reason: str, *,
                          uses: list[str] | None = None,
                          territories: list[str] | None = None,
                          note: str = "", occurred_at: str | None = None,
                          event_type: str = "RESTRICTION_APPLIED") -> dict:
        C.normalize_range({"start_offset_ms": start_ms, "end_offset_ms": end_ms})
        if reason not in C.RESTRICTION_REASONS:
            raise C.ContractError(f"未知限制原因：{reason}")
        if event_type not in ("RESTRICTION_APPLIED", "RELEASE_RESTRICTED"):
            raise C.ContractError("限制事件类型只能是 RESTRICTION_APPLIED 或 RELEASE_RESTRICTED")
        return self._emit(event_type, "usage_permission", restriction_id,
                          f"时段限制：{reason}",
                          {"segment_id": segment_id, "start_offset_ms": start_ms,
                           "end_offset_ms": end_ms, "reason": reason,
                           "uses": uses or [], "territories": territories or [],
                           "note": note},
                          occurred_at=occurred_at)

    def restrict_release(self, restriction_id: str, segment_id: str,
                         start_ms: int, end_ms: int, reason: str, **kw) -> dict:
        """RELEASE_RESTRICTED：节目后发布核对时发现问题，阻止相应发布用途。"""
        return self.apply_restriction(
            restriction_id, segment_id, start_ms, end_ms, reason,
            event_type="RELEASE_RESTRICTED", **kw)

    def lift_restriction(self, restriction_id: str, *, note: str = "",
                         occurred_at: str | None = None) -> dict:
        return self._emit("RESTRICTION_LIFTED", "usage_permission", restriction_id,
                          f"解除时段限制：{restriction_id}",
                          {"note": note}, occurred_at=occurred_at)

    # ---- 观众贡献 ----

    def record_contributions(self, batch_id: str, session_id: str,
                             contributions: list[dict], *,
                             occurred_at: str | None = None) -> dict:
        """contributions: [{participant_id, kind(cast/co_host/mic_audience),
                           segment_id?, window?:[s,e]}]"""
        return self._emit("CONTRIBUTIONS_RECORDED", "audience_contribution", batch_id,
                          f"记录 {len(contributions)} 条参与者/观众贡献",
                          {"session_id": session_id, "contributions": contributions},
                          occurred_at=occurred_at)

    # ---- 合同（按方各一聚合，版本即先后） ----

    def agree_contract(self, party: str, *, rate_bps: int = 0, fixed_amount: int = 0,
                       effective_from: str, note: str = "",
                       supersedes: str | None = None,
                       occurred_at: str | None = None) -> dict:
        contract_id = f"contract-{party.replace(':', '-')}"
        return self._emit("CONTRACT_AGREED", "contract_terms", contract_id,
                          f"合同生效：{party} 自 {effective_from} 起",
                          {"party": party, "rate_bps": rate_bps,
                           "fixed_amount": fixed_amount,
                           "effective_from": effective_from, "note": note,
                           "supersedes": supersedes},
                          occurred_at=occurred_at or effective_from)

    # ---- 打赏 ----

    def place_tip(self, tip_order_id: str, session_id: str, fan_id: str,
                  amount_fen: int, placed_at: str, *,
                  segment_id: str | None = None) -> dict:
        if amount_fen <= 0:
            raise C.ContractError("打赏金额必须为正")
        return self._emit("TIP_PLACED", "tip_order", tip_order_id,
                          f"观众 {fan_id} 打赏 {amount_fen} 分",
                          {"session_id": session_id, "fan_id": fan_id,
                           "amount_fen": amount_fen, "segment_id": segment_id,
                           "placed_at": placed_at},
                          occurred_at=placed_at)
