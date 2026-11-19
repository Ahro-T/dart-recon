import unittest

from dart_recon.rules import Statements, bs_equation, cf_bridge, cf_net_change_label, classify, restatement


def row(sj, aid, name, th, fr="", detail="-"):
    return {"rcept_no": "R", "corp_code": "C", "sj_div": sj, "account_id": aid, "account_nm": name,
            "account_detail": detail, "thstrm_amount": str(th), "frmtrm_amount": str(fr)}


CF_BASE = [
    row("CF", "dart_CashAndCashEquivalentsAtBeginningOfPeriodCf", "기초현금", 100, 90),
    row("CF", "dart_CashAndCashEquivalentsAtEndOfPeriodCf", "기말현금", 130, 100),
    row("CF", "ifrs-full_CashFlowsFromUsedInOperatingActivities", "영업", 50, 10),
    row("CF", "ifrs-full_CashFlowsFromUsedInInvestingActivities", "투자", -20, -5),
    row("CF", "ifrs-full_CashFlowsFromUsedInFinancingActivities", "재무", -10, 5),
]


def stmts(rows):
    return Statements({"list": rows}, "테스트", 2025, "CFS")


class Rules(unittest.TestCase):
    def test_classify_uses_reporting_unit(self):
        self.assertEqual(classify(0, 1_000_000), "ok")
        self.assertEqual(classify(2_000_000, 1_000_000), "rounding")
        self.assertEqual(classify(2_000_001, 1_000_000), "mismatch")

    def test_bs_equation_flags_gap(self):
        s = stmts([row("BS", "ifrs-full_Assets", "자산총계", 100, 90),
                   row("BS", "ifrs-full_Liabilities", "부채총계", 40, 30),
                   row("BS", "ifrs-full_Equity", "자본총계", 60, 50)])
        self.assertEqual([f.status for f in bs_equation(s)], ["ok", "mismatch"])

    def test_bridge_explains_gap_with_consolidation_change(self):
        rows = CF_BASE + [row("CF", "dart_ChangesInConsolidatedCompanies", "연결대상범위의 변동", 10, "")]
        th = cf_bridge(stmts(rows))[0]
        self.assertEqual(th.status, "ok")
        self.assertIn("연결대상범위의 변동", th.detail)

    def test_bridge_mismatch_when_unexplained(self):
        th = cf_bridge(stmts(CF_BASE))[0]  # 30 vs 20, no reconciling item
        self.assertEqual((th.status, th.diff), ("mismatch", 10))

    def test_bridge_ignores_subtotal_lines(self):
        rows = CF_BASE + [row("CF", "ifrs-full_IncreaseDecreaseInCashAndCashEquivalents",
                              "환율변동효과 반영전 현금및현금성자산의 순증가", 20, 10)]
        self.assertEqual(cf_bridge(stmts(rows))[0].status, "mismatch")

    def test_net_change_label_flags_value_matching_neither_definition(self):
        rows = CF_BASE + [row("CF", "ifrs-full_IncreaseDecreaseInCashAndCashEquivalents", "순증가", 25, 10)]
        self.assertEqual(cf_net_change_label(stmts(rows))[0].status, "mismatch")

    def test_restatement_compares_prior_column_to_last_years_report(self):
        now = Statements({"list": [row("BS", "ifrs-full_Assets", "자산총계", 200, 105)]}, "t", 2025, "CFS")
        then = Statements({"list": [row("BS", "ifrs-full_Assets", "자산총계", 100, 80)]}, "t", 2024, "CFS")
        f = next(x for x in restatement(now, then) if "assets" in x.detail)
        self.assertEqual((f.status, f.diff), ("mismatch", 5))


if __name__ == "__main__":
    unittest.main()
