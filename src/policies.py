"""权益判定的纯函数：毫秒区间运算、许可交集、时段限制。

这些规则不读取事件存储，只接收折叠后的状态片段，便于单测与复用。
约定：所有区间都是半开 [start, end)，偏移量相对于直播场次时间轴。
"""

from __future__ import annotations

from datetime import datetime

from . import catalogs as C
from .catalogs import (
    USES,
    USE_LIVE,
    CONSENT_PENDING,
    CONSENT_WITHDRAWN,
    REASON_PENDING_CONSENT,
    REASON_RIGHTS_EXPIRED,
    REASON_NOT_LICENSED,
    REASON_GEO_OUT_OF_SCOPE,
    REASON_WITHDRAWN,
)

Window = dict  # {"start_ms": int, "end_ms": int}
UNIVERSE = (0, 2**62)


def parse_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


# ---------- 区间运算 ----------

def normalize(windows: list[Window]) -> list[Window]:
    """合并重叠/相邻区间，返回排序后的不相交区间列表。"""
    pts = sorted((int(w["start_ms"]), int(w["end_ms"])) for w in windows)
    merged: list[list[int]] = []
    for start, end in pts:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        elif end > merged[-1][1]:
            merged[-1][1] = end
    return [{"start_ms": s, "end_ms": e} for s, e in merged]


def intersect(windows_a: list[Window], windows_b: list[Window]) -> list[Window]:
    """两组区间的交集。任一参数为 None 表示全集（不约束时间）。"""
    if windows_a is None or windows_b is None:
        return normalize(windows_a if windows_b is None else windows_b)
    out: list[Window] = []
    for a in normalize(windows_a):
        for b in normalize(windows_b):
            s, e = max(a["start_ms"], b["start_ms"]), min(a["end_ms"], b["end_ms"])
            if s < e:
                out.append({"start_ms": s, "end_ms": e})
    return normalize(out)


def subtract(positive: list[Window] | None, blocked: list[Window]) -> list[Window]:
    """从 positive 中挖掉 blocked。positive 为 None 时需要全集兜底，调用方应给定有限轴。"""
    if positive is None:
        positive = [{"start_ms": UNIVERSE[0], "end_ms": UNIVERSE[1]}]
    blocked = normalize(blocked)
    out: list[list[int]] = [[w["start_ms"], w["end_ms"]] for w in normalize(positive)]
    for bs, be in [(w["start_ms"], w["end_ms"]) for w in blocked]:
        nxt = []
        for s, e in out:
            if be <= s or bs >= e:
                nxt.append([s, e])
            else:
                if bs > s:
                    nxt.append([s, min(bs, e)])
                if be < e:
                    nxt.append([max(be, s), e])
        out = nxt
    return [{"start_ms": s, "end_ms": e} for s, e in out if s < e]


def windows_len(windows: list[Window]) -> int:
    return sum(w["end_ms"] - w["start_ms"] for w in windows)


def is_within(windows: list[Window], cover: list[Window]) -> bool:
    """windows 是否完全被 cover 覆盖。"""
    return windows_len(windows) == windows_len(intersect(windows, cover))


# ---------- 单条许可在某时点/地域是否有效 ----------

def grant_active(grant: dict, at: datetime, territory: str) -> bool:
    vf = parse_dt(grant.get("valid_from"))
    vu = parse_dt(grant.get("valid_until"))
    if vf and at < vf:
        return False
    if vu and at >= vu:
        return False
    if grant.get("territories") and territory not in grant["territories"]:
        return False
    return True


def grant_window(grant: dict) -> list[Window] | None:
    """许可覆盖的场次时间区间；未指定 time_windows 表示覆盖其素材出现的全部区间。"""
    tw = grant.get("time_windows")
    return normalize(tw) if tw else None


# ---------- 素材 → 某用途在某地域/时点的可使用区间与阻断原因 ----------

# 阻断原因的优先级：统一以 catalogs.REASON_PRIORITY 为准
REASON_PRIORITY = C.REASON_PRIORITY


def evaluate_material(
    material: dict,
    grants: list[dict],
    *,
    use: str,
    territory: str,
    at: datetime,
    axis: list[Window],
) -> dict:
    """评估单个素材在 use/territory/at 下的可用区间。

    grants：与该素材+权利人相关、已折叠到当前时点的许可列表。
    axis：评估轴（素材出现区间 ∩ 段落区间）。
    返回 {"allowed": [区间], "blockers": {原因: [区间]}}。
    """
    mtype = material.get("material_type")
    candidate_sets: dict[str, list[Window]] = {}

    def collect(reason: str, windows: list[Window]) -> None:
        if windows:
            candidate_sets.setdefault(reason, [])
            candidate_sets[reason].extend(windows)

    allowed_so_far: list[Window] = []
    for g in grants:
        gw = grant_window(g)
        gw = intersect(gw, axis) if gw else list(axis)
        if use not in g.get("uses", []):
            continue
        state = g.get("consent_state")
        if state == CONSENT_PENDING:
            # 口头待确认同意：直播当时已记录，可覆盖直播用途（既成播出），
            # 回放、商业剪辑必须等补签确认后才放行。
            if use == USE_LIVE:
                allowed_so_far.extend(gw)
            else:
                collect(REASON_PENDING_CONSENT, gw)
            continue
        if state == CONSENT_WITHDRAWN:
            collect(REASON_WITHDRAWN, gw)
            continue
        # CONFIRMED 正式许可：查生效时点与地域
        if grant_active(g, at, territory):
            allowed_so_far.extend(gw)
        else:
            vu = parse_dt(g.get("valid_until"))
            territories = g.get("territories") or []
            if vu and at >= vu:
                collect(REASON_RIGHTS_EXPIRED, gw)
            elif territories and territory not in territories:
                collect(REASON_GEO_OUT_OF_SCOPE, gw)
            else:
                # 许可尚未生效等其他情况：此刻等于没有授权
                collect(REASON_NOT_LICENSED, gw)

    allowed = normalize(allowed_so_far)
    unavailable = subtract(axis, allowed)

    # 已撤回的旧许可若与更新的候选（到期/地域不符/待补签等）落在同一区间，
    # 对外应显示更新的那个原因；撤回原因只保留在没有其他解释的区间。
    if REASON_WITHDRAWN in candidate_sets:
        other_cover = normalize([
            w for reason, ws in candidate_sets.items()
            if reason != REASON_WITHDRAWN for w in ws
        ])
        candidate_sets[REASON_WITHDRAWN] = subtract(
            normalize(candidate_sets[REASON_WITHDRAWN]), other_cover
        )
        if not candidate_sets[REASON_WITHDRAWN]:
            del candidate_sets[REASON_WITHDRAWN]

    # 同一毫秒可能有多条冲突许可，按展示优先级取一个对外原因
    blockers: dict[str, list[Window]] = {}
    remaining = unavailable
    for reason in C.REASON_PRIORITY:
        ws = candidate_sets.get(reason)
        if not ws or not remaining:
            continue
        hit = intersect(remaining, ws)
        if hit:
            blockers[reason] = hit
            remaining = subtract(remaining, hit)
    if remaining:
        blockers[REASON_NOT_LICENSED] = normalize(
            blockers.get(REASON_NOT_LICENSED, []) + remaining
        )
    return {"allowed": allowed, "blockers": blockers}


def combine_materials(results: list[dict], axis: list[Window]) -> dict:
    """多个素材组合：可使用区间取交集；阻断原因取并集并分别记录区间。

    任一素材在某毫秒不可用，该毫秒组合即不可用（权限交集原则）。
    """
    if not results:
        return {"allowed": [], "blockers": {REASON_NOT_LICENSED: axis}}
    allowed = list(axis)
    merged_blockers: dict[str, list[Window]] = {}
    for r in results:
        allowed = intersect(allowed, r["allowed"])
        for reason, ws in r["blockers"].items():
            merged_blockers.setdefault(reason, [])
            merged_blockers[reason].extend(ws)

    # 组合级阻断原因落在“交集失败”的区间上，并按优先级逐毫秒归一个原因
    failed = subtract(axis, allowed)
    blockers: dict[str, list[Window]] = {}
    for reason in C.REASON_PRIORITY:
        ws = merged_blockers.get(reason)
        if not ws:
            continue
        already = normalize([w for w2 in blockers.values() for w in w2])
        hit = subtract(intersect(failed, ws), already)
        if hit:
            blockers[reason] = hit
    leftovers = subtract(failed, normalize([w for ws in blockers.values() for w in ws]))
    if leftovers:
        blockers[REASON_NOT_LICENSED] = normalize(
            blockers.get(REASON_NOT_LICENSED, []) + leftovers
        )
    return {"allowed": normalize(allowed), "blockers": blockers}


def apply_manual_restrictions(
    verdict: dict, restrictions: list[dict], use: str
) -> dict:
    """叠加运营人工限制（RELEASE_RESTRICTED / RELEASE_CLEARED 折叠结果）。

    restrictions 中每条带 status：ACTIVE 生效、CLEARED 已解除。
    解除只作用于同一 restriction_id 的人工限制，不影响权益硬规则；
    没有 restriction_id 的临时限制无法被定向解除（只能等新事件覆盖）。
    """
    # 按 restriction_id 折叠：CLEARED 移除同 id 的 ACTIVE
    active_by_id: dict[str, dict] = {}
    ad_hoc: list[dict] = []
    for r in restrictions:
        if use not in r.get("uses", USES):
            continue
        rid = r.get("restriction_id")
        if r["status"] == "CLEARED":
            if rid:
                active_by_id.pop(rid, None)
            continue
        if r["status"] != "ACTIVE":
            continue
        if rid:
            active_by_id[rid] = r
        else:
            ad_hoc.append(r)

    allowed = verdict["allowed"]
    blockers = {reason: list(ws) for reason, ws in verdict.get("blockers", {}).items()}
    for r in list(active_by_id.values()) + ad_hoc:
        ws = r.get("time_windows")
        if not ws:
            continue
        allowed = subtract(allowed, ws)
        reason = r["reason_code"]
        blockers.setdefault(reason, [])
        blockers[reason].extend(ws)

    blockers = {reason: normalize(ws) for reason, ws in blockers.items() if normalize(ws)}
    return {"allowed": normalize(allowed), "blockers": blockers}


def primary_reason(blockers: dict[str, list[Window]]) -> str | None:
    for reason in REASON_PRIORITY:
        if blockers.get(reason):
            return reason
    return next(iter(blockers), None)
