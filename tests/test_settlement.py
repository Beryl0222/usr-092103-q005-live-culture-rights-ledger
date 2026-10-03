import unittest

from src import contracts as C
from src.app import LedgerService
from src.settlement import SettlementService


def tipped_service(*, tip_at="2026-09-20T20:00:00+08:00",
                   allocate_at="2026-09-20T20:31:00+08:00",
                   tip_id="tip-1", amount=10_000):
    s = LedgerService()
    s.agree_contract("platform", rate_bps=3000,
                     effective_from="2026-09-01T00:00:00+08:00")
    s.agree_contract("house", rate_bps=7000,
                     effective_from="2026-09-01T00:00:00+08:00")
    s.agree_contract("participant:p1", rate_bps=2000,
                     effective_from="2026-09-01T00:00:00+08:00")
    s.schedule_session("live-1", "t", "2026-09-20T19:30:00+08:00",
                       occurred_at="2026-09-19T10:00:00+08:00")
    s.start_session("live-1", "2026-09-20T19:30:00+08:00")
    s.register_segment("seg-1", "live-1", 1, "段一", 0, 600_000,
                       occurred_at="2026-09-20T19:30:00+08:00")
    s.register_participant("p1", "周师傅", "inheritor")
    s.cast_participant("seg-1", [{"participant_id": "p1"}])
    s.record_contributions("cb-1", "live-1",
                           [{"participant_id": "p1", "kind": "cast",
                             "segment_id": "seg-1"}],
                           occurred_at=tip_at)
    s.place_tip(tip_id, "live-1", "fan-1", amount, tip_at, segment_id="seg-1")
    st = SettlementService(s.store, s.ledger)
    st.allocate_tip(tip_id, occurred_at=allocate_at)
    return s, st


class SettlementTest(unittest.TestCase):
    def test_initial_split_uses_contracts_effective_at_tip_time(self):
        s, st = tipped_service()
        sheet = st.tip_sheet("tip-1")
        net = sheet["net_by_party"]
        # 平台 30% = 3000；可分配池 7000；传承人 20%*7000=1400；机构余量 5600
        self.assertEqual(net["platform"], 3000)
        self.assertEqual(net["participant:p1"], 1400)
        self.assertEqual(net["house"], 5600)
        self.assertEqual(sheet["net_total_fen"], 10_000)

    def test_later_contract_version_does_not_retroactively_change_history(self):
        s, st = tipped_service()
        s.agree_contract("participant:p1", rate_bps=3500,
                         effective_from="2026-10-01T00:00:00+08:00",
                         supersedes="contract-participant-p1")
        # 历史订单仍是旧合同下的 1400
        self.assertEqual(st.tip_sheet("tip-1")["net_by_party"]["participant:p1"], 1400)
        # 但每一行记录了当时依据的合同版本，可核对
        initial = [a for a in st.tip_sheet("tip-1")["entries"]
                   if a["kind"] == C.REV_INITIAL][0]
        p1_line = [l for l in initial["lines"]
                   if l["party"] == "participant:p1"][0]
        self.assertEqual(p1_line["contract_effective_from"],
                         "2026-09-01T00:00:00+08:00")

    def test_refund_reverses_everything_and_keeps_original_entry(self):
        s, st = tipped_service()
        st.refund_tip("tip-1", reason="未成年消费退款",
                      occurred_at="2026-09-23T10:00:00+08:00")
        sheet = st.tip_sheet("tip-1")
        self.assertEqual(sheet["status"], "refunded")
        self.assertEqual(sheet["net_total_fen"], 0)
        self.assertTrue(all(v == 0 for v in sheet["net_by_party"].values()))
        kinds = [e["kind"] for e in sheet["entries"]]
        self.assertIn(C.REV_INITIAL, kinds)       # 原分配保留
        self.assertIn(C.REV_REVERSAL, kinds)      # 冲正追加，不改原条目

    def test_supplement_payment_balances_against_house(self):
        s, st = tipped_service()
        st.supplement_payment(
            "tip-1", [{"party": "participant:p1", "amount_fen": 1_500}],
            reason="后续确权补付", occurred_at="2026-10-02T10:00:00+08:00")
        net = st.tip_sheet("tip-1")["net_by_party"]
        self.assertEqual(net["participant:p1"], 1400 + 1500)
        self.assertEqual(net["house"], 5600 - 1500)   # 机构补差
        self.assertEqual(st.tip_sheet("tip-1")["net_total_fen"], 10_000)

    def test_reversal_line_balances_against_house(self):
        s, st = tipped_service()
        st.reverse_line("tip-1", "participant:p1", 400,
                        reason="原分配多计，冲正",
                        occurred_at="2026-10-02T10:05:00+08:00")
        net = st.tip_sheet("tip-1")["net_by_party"]
        self.assertEqual(net["participant:p1"], 1000)
        self.assertEqual(net["house"], 6000)          # 资金回归机构
        self.assertEqual(st.tip_sheet("tip-1")["net_total_fen"], 10_000)

    def test_holder_statement_lists_contract_version_per_row(self):
        s, st = tipped_service()
        report = st.holder_statement("participant:p1")
        self.assertEqual(report["net_fen"], 1400)
        self.assertEqual(report["rows"][0]["contract_effective_from"],
                         "2026-09-01T00:00:00+08:00")

    def test_double_refund_rejected(self):
        s, st = tipped_service()
        st.refund_tip("tip-1", reason="x",
                      occurred_at="2026-09-23T10:00:00+08:00")
        with self.assertRaises(C.ContractError):
            st.refund_tip("tip-1", reason="again",
                          occurred_at="2026-09-24T10:00:00+08:00")

    def test_double_allocation_rejected(self):
        s, st = tipped_service()
        with self.assertRaises(C.ContractError):
            st.allocate_tip("tip-1")


if __name__ == "__main__":
    unittest.main()
