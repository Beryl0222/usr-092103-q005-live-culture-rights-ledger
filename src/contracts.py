"""领域词汇与事件工厂。

事件是权益账唯一的交换与持久化单位：任何授权、限制、发布、结算变化都只能
追加事件，不允许修改已发生的事件。本模块只负责造事件与公共字段校验，
业务投影见 rights_ledger 与 settlement。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .validator import validate_event

# --- 稳定枚举（与 contracts/domain.schema.json 保持一致）---

USE_LIVE = "LIVE_WEBCAST"
USE_REPLAY = "REPLAY"
USE_CLIP = "COMMERCIAL_CLIP"
USES = (USE_LIVE, USE_REPLAY, USE_CLIP)

# 同意状态：直播中的口头同意只能是待确认
CONSENT_GRANTED = "granted"
CONSENT_PENDING = "pending_confirmation"
CONSENT_DECLINED = "declined"

CHANNEL_WRITTEN = "written"
CHANNEL_ORAL = "oral"
CHANNEL_GUARDIAN = "guardian"
CHANNEL_PLATFORM = "platform_license"

# 限制原因稳定口径
REASON_MINOR = "minor_in_mic"
REASON_MUSIC_UNKNOWN = "music_unidentified"
REASON_RIGHTS_EXPIRED = "rights_expired"
REASON_PENDING_CONSENT = "pending_consent"
REASON_DECLINED = "declined_consent"
REASON_CONSENT_MISSING = "consent_missing"
REASON_UNLICENSED_MATERIAL = "unlicensed_material"
REASON_USE_NOT_LICENSED = "use_not_licensed"
REASON_TERRITORY = "territory_not_licensed"
REASON_TAKEDOWN = "takedown_request"

RESTRICTION_REASONS = (
    REASON_MINOR,
    REASON_MUSIC_UNKNOWN,
    REASON_RIGHTS_EXPIRED,
    REASON_PENDING_CONSENT,
    REASON_DECLINED,
    REASON_CONSENT_MISSING,
    REASON_UNLICENSED_MATERIAL,
    REASON_USE_NOT_LICENSED,
    REASON_TERRITORY,
    REASON_TAKEDOWN,
)

# 结算条目类型
REV_INITIAL = "initial"          # 按当时有效合同的首次分配
REV_REVERSAL = "reversal"        # 冲正（退款/合同纠偏）
REV_SUPPLEMENT = "supplement"    # 补付（后续确权）

ADJ_SUPPLEMENT = "supplement_payment"
ADJ_REVERSAL = "reversal"

# 世界地域："*" 表示全球
WORLDWIDE = "*"

EVENT_TYPES = (
    "SESSION_SCHEDULED",
    "SESSION_STARTED",
    "SESSION_ENDED",
    "SEGMENT_REGISTERED",
    "PARTICIPANT_REGISTERED",
    "MATERIAL_REGISTERED",
    "MATERIAL_VERIFIED",
    "MATERIAL_LINKED",
    "SEGMENT_CAST",
    "CONSENT_CAPTURED",
    "PERMISSION_GRANTED",
    "PERMISSION_EXPIRED",
    "RESTRICTION_APPLIED",
    "RESTRICTION_LIFTED",
    "RELEASE_RESTRICTED",
    "CONTRIBUTIONS_RECORDED",
    "CONTRACT_AGREED",
    "TIP_PLACED",
    "TIP_REFUNDED",
    "REVENUE_ALLOCATED",
    "RIGHTS_ADJUSTED",
    "REPLAY_PUBLISHED",
    "REPLAY_TAKEN_DOWN",
    "CLIP_PROPOSED",
    "CLIP_PUBLISHED",
    "CLIP_REJECTED",
)

AGGREGATE_TYPES = (
    "live_session",
    "program_segment",
    "participant",
    "material_source",
    "usage_permission",
    "audience_contribution",
    "contract_terms",
    "tip_order",
    "revenue_entry",
    "replay_version",
    "secondary_clip",
)


class ContractError(ValueError):
    """事件本身不符合领域契约。"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_event(
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    version: int,
    summary: str,
    payload: dict[str, Any] | None = None,
    *,
    event_id: str | None = None,
    occurred_at: str | None = None,
    correlation_id: str | None = None,
    cause_id: str | None = None,
) -> dict[str, Any]:
    """构造一条不可变事件（不负责落库，version 由调用方/存储保证）。"""
    event: dict[str, Any] = {
        "event_id": event_id or f"{aggregate_id}-v{version}-{event_type.lower()}",
        "event_type": event_type,
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "occurred_at": occurred_at or now_iso(),
        "version": version,
        "summary": summary,
    }
    if correlation_id is not None:
        event["correlation_id"] = correlation_id
    if cause_id is not None:
        event["cause_id"] = cause_id
    if payload is not None:
        event["payload"] = payload
    errors = validate_event(event)
    if errors:
        raise ContractError("；".join(errors))
    if event_type not in EVENT_TYPES:
        raise ContractError(f"未知事件类型：{event_type}")
    if aggregate_type not in AGGREGATE_TYPES:
        raise ContractError(f"未知聚合类型：{aggregate_type}")
    return event


def normalize_range(time_range: dict[str, int] | None) -> tuple[int, int] | None:
    """校验毫秒时间区间，返回闭开区间 [start, end)；None 表示全程。"""
    if time_range is None:
        return None
    start = time_range["start_offset_ms"]
    end = time_range["end_offset_ms"]
    if not isinstance(start, int) or not isinstance(end, int):
        raise ContractError("时间区间端点必须是整数毫秒")
    if start < 0 or end <= start:
        raise ContractError("时间区间必须满足 0 <= start < end")
    return start, end
