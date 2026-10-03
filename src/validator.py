"""领域事件公共校验（仅依赖标准库，与 contracts/domain.schema.json 的约束保持一致）。"""

from datetime import datetime

from .catalogs import (
    AGGREGATE_TYPES,
    EVENT_TYPES,
    USES,
    REASONS,
    CONSENT_STATES,
    CERTAINTY_LEVELS,
    ROLES,
    MATERIAL_TYPES,
    ADJUSTMENT_KINDS,
    REVENUE_ENTRY_KINDS,
)

REQUIRED = (
    "event_id",
    "event_type",
    "aggregate_type",
    "aggregate_id",
    "occurred_at",
    "version",
    "summary",
)


def _parse_dt(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _check_windows(windows, path: str, errors: list[str]) -> None:
    if not isinstance(windows, list) or not windows:
        errors.append(f"{path} 必须是非空数组")
        return
    for i, w in enumerate(windows):
        if not isinstance(w, dict):
            errors.append(f"{path}[{i}] 必须是对象")
            continue
        start, end = w.get("start_ms"), w.get("end_ms")
        if not isinstance(start, int) or isinstance(start, bool) or start < 0:
            errors.append(f"{path}[{i}].start_ms 必须是非负整数")
        if not isinstance(end, int) or isinstance(end, bool) or end <= 0:
            errors.append(f"{path}[{i}].end_ms 必须是正整数")
        if isinstance(start, int) and isinstance(end, int) and start >= end:
            errors.append(f"{path}[{i}] 区间必须满足 start_ms < end_ms")


def validate_event(record: dict) -> list[str]:
    """返回中文错误信息列表；空列表表示通过。"""
    if not isinstance(record, dict):
        return ["事件必须是对象"]

    errors = [f"缺少字段：{name}" for name in REQUIRED if name not in record]
    if errors:
        return errors

    if not isinstance(record["event_id"], str) or not record["event_id"].strip():
        errors.append("event_id 必须是非空字符串")
    if record["event_type"] not in EVENT_TYPES:
        errors.append(f"未知 event_type：{record['event_type']}")
    if record["aggregate_type"] not in AGGREGATE_TYPES:
        errors.append(f"未知 aggregate_type：{record['aggregate_type']}")
    if not isinstance(record["aggregate_id"], str) or not record["aggregate_id"].strip():
        errors.append("aggregate_id 必须是非空字符串")
    if (
        not isinstance(record["version"], int)
        or isinstance(record["version"], bool)
        or record["version"] < 1
    ):
        errors.append("version 必须是正整数")
    if not isinstance(record["summary"], str) or not record["summary"].strip():
        errors.append("summary 必须是非空字符串")
    if _parse_dt(record["occurred_at"]) is None:
        errors.append("occurred_at 必须是 ISO-8601 日期时间")

    payload = record.get("payload")
    if payload is not None:
        if not isinstance(payload, dict):
            errors.append("payload 必须是对象")
        else:
            errors.extend(_validate_payload(payload))
    return errors


def _validate_payload(p: dict) -> list[str]:
    errors: list[str] = []

    if "uses" in p:
        uses = p["uses"]
        if not isinstance(uses, list) or not uses:
            errors.append("payload.uses 必须是非空数组")
        elif any(u not in USES for u in uses):
            errors.append("payload.uses 含未知用途")
    if "role" in p and p["role"] not in ROLES:
        errors.append(f"未知 role：{p['role']}")
    if "material_type" in p and p["material_type"] not in MATERIAL_TYPES:
        errors.append(f"未知 material_type：{p['material_type']}")
    if "consent_state" in p and p["consent_state"] not in CONSENT_STATES:
        errors.append(f"未知 consent_state：{p['consent_state']}")
    if "certainty" in p and p["certainty"] not in CERTAINTY_LEVELS:
        errors.append(f"未知 certainty：{p['certainty']}")
    if "adjustment_kind" in p and p["adjustment_kind"] not in ADJUSTMENT_KINDS:
        errors.append(f"未知 adjustment_kind：{p['adjustment_kind']}")
    if "entry_kind" in p and p["entry_kind"] not in REVENUE_ENTRY_KINDS:
        errors.append(f"未知 entry_kind：{p['entry_kind']}")

    if "time_windows" in p:
        _check_windows(p["time_windows"], "payload.time_windows", errors)

    if "territories" in p:
        terr = p["territories"]
        if not isinstance(terr, list) or not terr:
            errors.append("payload.territories 必须是非空数组")

    vf, vu = p.get("valid_from"), p.get("valid_until")
    if vf is not None and _parse_dt(vf) is None:
        errors.append("payload.valid_from 必须是日期时间")
    if vu is not None and _parse_dt(vu) is None:
        errors.append("payload.valid_until 必须是日期时间或 null")
    if vf and vu:
        dtf, dtu = _parse_dt(vf), _parse_dt(vu)
        if dtf and dtu and dtu <= dtf:
            errors.append("授权期限必须满足 valid_from < valid_until")

    for money_field in ("amount",):
        if money_field in p:
            v = p[money_field]
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                errors.append(f"payload.{money_field} 必须是非负整数（单位：分）")

    for rate_field in ("platform_rate",):
        if rate_field in p:
            v = p[rate_field]
            if not isinstance(v, (int, float)) or not 0 <= v <= 1:
                errors.append(f"payload.{rate_field} 必须在 0~1 之间")
    if "revenue_share" in p and isinstance(p["revenue_share"], dict):
        for k, v in p["revenue_share"].items():
            if not isinstance(v, (int, float)) or not 0 <= v <= 1:
                errors.append(f"revenue_share[{k}] 必须在 0~1 之间")

    if "allocations" in p and isinstance(p["allocations"], list):
        for i, a in enumerate(p["allocations"]):
            if not isinstance(a, dict) or not a.get("payee_id"):
                errors.append(f"allocations[{i}] 缺少 payee_id")
                continue
            amt = a.get("amount")
            if not isinstance(amt, int) or isinstance(amt, bool):
                errors.append(f"allocations[{i}].amount 必须是整数（分）")

    if "source_segments" in p and isinstance(p["source_segments"], list):
        for i, s in enumerate(p["source_segments"]):
            if not isinstance(s, dict) or not s.get("segment_id"):
                errors.append(f"source_segments[{i}] 缺少 segment_id")
                continue
            _check_windows(s.get("time_windows"), f"source_segments[{i}].time_windows", errors)

    r = p.get("restriction")
    if r is not None:
        if not isinstance(r, dict):
            errors.append("payload.restriction 必须是对象")
        else:
            if r.get("reason_code") not in REASONS:
                errors.append(f"未知 restriction.reason_code：{r.get('reason_code')}")
            uses = r.get("uses")
            if not isinstance(uses, list) or not uses or any(u not in USES for u in uses):
                errors.append("restriction.uses 必须是已登记用途的非空数组")
            if "time_windows" in r:
                _check_windows(r["time_windows"], "restriction.time_windows", errors)
            if "territories" in r:
                if not isinstance(r["territories"], list) or not r["territories"]:
                    errors.append("restriction.territories 必须是非空数组")
    return errors
