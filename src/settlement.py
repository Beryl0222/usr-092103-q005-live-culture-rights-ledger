"""打赏结算：以打赏时点有效合同为唯一结算依据。

合同改版不追溯历史订单；退款整笔冲正；后续确权通过补付或冲正处理，
原分配永不删除，只追加相反方向/补差的条目。所有金额单位为分（整数）。
"""

from __future__ import annotations

from dataclasses import dataclass

from . import contracts as C
from .event_store import EventStore, parse_ts
from .rights_ledger import RightsLedger


@dataclass
class Contract:
    id: str
    party: str                       # platform | house | participant:<id> | material:<id>
    effective_from: str
    rate_bps: int                    # 分成比例（基点，万分比）；平台/机构用
    fixed_amount: int = 0            # 固定金额（分），优先级高于比例
    supersedes: str | None = None

    def share_of(self, pool: int) -> int:
        return self.fixed_amount if self.fixed_amount else pool * self.rate_bps // 10000


class SettlementService:
    """从权益账重放结算，并负责产生结算类事件。"""

    def __init__(self, store: EventStore, ledger: RightsLedger):
        self.store = store
        self.ledger = ledger

    # ----- 合同快照：打赏时点哪版合同有效就用哪版 -----

    def _contract_at(self, party: str, at_iso: str) -> Contract | None:
        at = parse_ts(at_iso)
        current = None
        for cid, versions in self.ledger.contracts.items():
            for v in versions:
                if v["party"] != party:
                    continue
                if parse_ts(v["effective_from"]) > at:
                    continue
                if current is None or parse_ts(v["effective_from"]) >= parse_ts(current.effective_from):
                    current = Contract(
                        id=cid, party=party, effective_from=v["effective_from"],
                        rate_bps=v.get("rate_bps", 0), fixed_amount=v.get("fixed_amount", 0),
                        supersedes=v.get("supersedes"))
        return current

    # ----- 首次分配 -----

    def allocate_tip(self, tip_order_id: str, *, event_id: str | None = None,
                     correlation_id: str | None = None,
                     occurred_at: str | None = None) -> dict:
        """按打赏时点有效合同结算：平台抽成、机构余量、贡献者按当时合同分配。"""
        tip = self.ledger.tips[tip_order_id]
        if tip["status"] != "placed":
            raise C.ContractError(f"打赏 {tip_order_id} 状态为 {tip['status']}，不可首次分配")
        if any(a.get("kind_event") == "REVENUE_ALLOCATED"
               for a in self.ledger.allocations.get(tip_order_id, [])):
            raise C.ContractError(f"打赏 {tip_order_id} 已分配，禁止重算（只能补付/冲正）")

        gross = tip["amount_fen"]
        lines: list[dict] = []

        def take(party: str, pool: int) -> int:
            contract = self._contract_at(party, tip["placed_at"])
            if contract is None:
                return 0
            amount = contract.share_of(pool)
            if amount <= 0:
                return 0
            lines.append({
                "party": party, "amount_fen": amount,
                "contract_id": contract.id,
                "contract_effective_from": contract.effective_from,
            })
            return amount

        # 平台先按毛额抽成；其余为可分配池，贡献者在池内按当时合同比例分
        platform_fee = take("platform", gross)
        pool = gross - platform_fee

        contributors = self._tip_contributors(tip)
        distributed = 0
        for party in contributors:
            amount = take(party, pool)
            if amount:
                distributed += amount
                lines[-1]["basis"] = "contributor"

        residual = pool - distributed
        if residual < 0:
            raise C.ContractError(f"合同分成之和超过可分配池：超分 {-residual} 分")
        house = self._contract_at("house", tip["placed_at"])
        lines.append({
            "party": "house", "amount_fen": residual,
            "contract_id": house.id if house else None,
            "contract_effective_from": house.effective_from if house else None,
            "basis": "residual",
        })

        total = sum(l["amount_fen"] for l in lines)
        assert total == gross, (total, gross)
        payload = {
            "tip_order_id": tip_order_id,
            "session_id": tip["session_id"],
            "gross_fen": gross,
            "kind": C.REV_INITIAL,
            "lines": lines,
        }
        event = C.make_event(
            "REVENUE_ALLOCATED", "revenue_entry",
            f"rev-{tip_order_id}", self.store.next_version("revenue_entry", f"rev-{tip_order_id}"),
            f"打赏 {tip_order_id} 按当时有效合同完成首次分配",
            payload, event_id=event_id, occurred_at=occurred_at or C.now_iso(),
            correlation_id=correlation_id, cause_id=tip.get("event_id"))
        self.store.append(event)
        self.ledger.reload(self.store.events())
        return event

    def _tip_contributors(self, tip: dict) -> list[str]:
        seg_id = tip.get("segment_id")
        parties: list[str] = []
        for c in self.ledger.contributions:
            if c["session_id"] != tip["session_id"]:
                continue
            if seg_id and c.get("segment_id") != seg_id:
                continue
            parties.append(f"participant:{c['participant_id']}" if c["kind"] == "cast"
                           else f"audience:{c['participant_id']}")
        # 段落演员表也算贡献者
        if seg_id and seg_id in self.ledger.segments:
            for pid in self.ledger.segments[seg_id].cast:
                parties.append(f"participant:{pid}")
        seen: set[str] = set()
        return [p for p in parties if not (p in seen or seen.add(p))]

    # ----- 退款：整笔冲正 -----

    def refund_tip(self, tip_order_id: str, *, reason: str, event_id: str | None = None,
                   occurred_at: str | None = None) -> list[dict]:
        tip = self.ledger.tips[tip_order_id]
        if tip["status"] != "placed":
            raise C.ContractError(f"打赏 {tip_order_id} 已退款，不能重复退款")
        at = occurred_at or C.now_iso()

        refund_event = C.make_event(
            "TIP_REFUNDED", "tip_order", tip_order_id,
            self.store.next_version("tip_order", tip_order_id),
            f"打赏 {tip_order_id} 退款：{reason}",
            {"amount_fen": tip["amount_fen"], "reason": reason, "refunded_at": at},
            event_id=event_id, occurred_at=at)
        self.store.append(refund_event)
        self.ledger.reload(self.store.events())
        net = self._net_by_party(tip_order_id)
        if not net:
            return [refund_event]
        # 冲平退款时点的全部余额（含此前可能的补付/冲正），原条目一律保留
        reversal_lines = [{"party": party, "amount_fen": -amount}
                          for party, amount in net.items() if amount]
        reversal = self._adjust(
            tip_order_id, C.REV_REVERSAL, reason, at,
            lines=reversal_lines, note="退款整笔冲正，原分配保留不删",
            balancing_party=None)
        return [refund_event, reversal]

    # ----- 后续确权：补付 / 冲正 -----

    def supplement_payment(self, tip_order_id: str, lines: list[dict], *, reason: str,
                           event_id: str | None = None, occurred_at: str | None = None) -> dict:
        """补付：确权后向某方追加（lines: [{party, amount_fen}]），资金来自机构补差。"""
        at = occurred_at or C.now_iso()
        for line in lines:
            if line.get("amount_fen", 0) <= 0:
                raise C.ContractError("补付金额必须为正")
            contract = self._contract_at(line["party"], at)
            line.setdefault("contract_id", contract.id if contract else None)
        return self._adjust(tip_order_id, C.REV_SUPPLEMENT, reason, at, lines=lines,
                            note="后续确权补付")

    def reverse_line(self, tip_order_id: str, party: str, amount_fen: int, *,
                     reason: str, occurred_at: str | None = None) -> dict:
        """冲正：把原分配中多给某方的金额追回（负数条目追加，不改原条目）。"""
        if amount_fen <= 0:
            raise C.ContractError("冲正金额必须为正（条目方向自动取反）")
        at = occurred_at or C.now_iso()
        return self._adjust(tip_order_id, C.REV_REVERSAL, reason, at,
                            lines=[{"party": party, "amount_fen": -amount_fen}],
                            note="后续确权冲正", via_rights_adjusted=True)

    def _adjust(self, tip_order_id: str, kind: str, reason: str, at: str, *,
                lines: list[dict] | None = None, note: str = "",
                via_rights_adjusted: bool = False,
                balancing_party: str | None = "house") -> dict:
        tip = self.ledger.tips[tip_order_id]
        lines = [dict(l) for l in (lines or [])]
        if kind in (C.REV_SUPPLEMENT, C.REV_REVERSAL) and balancing_party:
            # 补付资金由机构补差、冲正资金回归机构：账始终平衡
            drift = sum(l["amount_fen"] for l in lines)
            if drift:
                contract = self._contract_at(balancing_party, at)
                lines.append({
                    "party": balancing_party, "amount_fen": -drift,
                    "contract_id": contract.id if contract else None,
                    "basis": "house_balance",
                })
        payload = {
            "tip_order_id": tip_order_id,
            "session_id": tip["session_id"],
            "kind": kind,
            "reason": reason,
            "note": note,
            "lines": lines,
        }
        rev_id = f"rev-{tip_order_id}"
        event_type = ("RIGHTS_ADJUSTED" if kind == C.REV_SUPPLEMENT or via_rights_adjusted
                      else "REVENUE_ALLOCATED")
        event = C.make_event(
            event_type,
            "revenue_entry", rev_id,
            self.store.next_version("revenue_entry", rev_id),
            f"打赏 {tip_order_id} {note or reason}",
            payload, occurred_at=at)
        self.store.append(event)
        self.ledger.reload(self.store.events())
        return event

    def _initial_lines(self, tip_order_id: str) -> list[dict]:
        for a in self.ledger.allocations.get(tip_order_id, []):
            if a.get("kind") == C.REV_INITIAL:
                return a["lines"]
        return []

    def _net_by_party(self, tip_order_id: str) -> dict[str, int]:
        net: dict[str, int] = {}
        for a in self.ledger.allocations.get(tip_order_id, []):
            for line in a.get("lines", []):
                net[line["party"]] = net.get(line["party"], 0) + line["amount_fen"]
        return net

    # ----- 对账 -----

    def holder_statement(self, party: str) -> dict:
        """权利人核对：每笔相关条目的合同版本、金额、累计应付。"""
        rows = []
        for tip_id, allocs in self.ledger.allocations.items():
            for a in allocs:
                for line in a.get("lines", []):
                    if line["party"] != party:
                        continue
                    rows.append({
                        "tip_order_id": tip_id,
                        "kind": a["kind"],
                        "reason": a.get("reason", ""),
                        "contract_id": line.get("contract_id"),
                        "contract_effective_from": line.get("contract_effective_from"),
                        "amount_fen": line["amount_fen"],
                        "event_id": a["event_id"],
                    })
        net = sum(r["amount_fen"] for r in rows)
        return {"party": party, "currency": "CNY", "net_fen": net, "rows": rows}

    def tip_sheet(self, tip_order_id: str) -> dict:
        """单笔打赏的完整结算轨迹（首分→退款/补付/冲正后的净额）。"""
        tip = self.ledger.tips[tip_order_id]
        allocs = self.ledger.allocations.get(tip_order_id, [])
        net: dict[str, int] = {}
        for a in allocs:
            for line in a.get("lines", []):
                net[line["party"]] = net.get(line["party"], 0) + line["amount_fen"]
        return {
            "tip_order_id": tip_order_id,
            "status": tip["status"],
            "gross_fen": tip["amount_fen"],
            "entries": [{"kind": a["kind"], "reason": a.get("reason", ""),
                         "note": a.get("note", ""), "lines": a.get("lines", [])}
                        for a in allocs],
            "net_by_party": net,
            "net_total_fen": sum(net.values()),
        }
