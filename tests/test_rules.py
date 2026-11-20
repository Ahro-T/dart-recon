import unittest

from dart_recon.rules import (Statements, bs_equation, cf_bridge, cf_end_vs_bs_cash, cf_net_change_label,
                              cf_prior_end_is_current_begin, classify, restatement)


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

    def test_end_cash_gap_explained_by_held_for_sale_cash_line(self):
        # 롯데지주 2025: 기말현금 - 재무상태표 현금 = '당기말 매각예정자산집단에 포함된 현금및현금성자산'
        rows = [row("CF", "dart_CashAndCashEquivalentsAtEndOfPeriodCf", "기말의 현금및현금성자산", 1_000_359, 1_001_227),
                row("BS", "ifrs-full_CashAndCashEquivalents", "현금및현금성자산", 1_000_000, 1_000_000),
                row("CF", "-표준계정코드 미사용-", "당기말 매각예정자산집단에 포함된 현금및현금성자산", 359, 1_227)]
        th, fr = cf_end_vs_bs_cash(stmts(rows))
        self.assertEqual((th.status, fr.status), ("ok", "ok"))  # 전기 열도 같은 줄의 전기 열 값(1,227)으로 설명됨
        self.assertIn("매각예정", th.detail)

    def test_end_cash_gap_not_explained_when_amount_differs(self):
        rows = [row("CF", "dart_CashAndCashEquivalentsAtEndOfPeriodCf", "기말의 현금및현금성자산", 1_000_400),
                row("BS", "ifrs-full_CashAndCashEquivalents", "현금및현금성자산", 1_000_000),
                row("CF", "-표준계정코드 미사용-", "당기말 매각예정자산집단에 포함된 현금및현금성자산", 359)]
        self.assertEqual(cf_end_vs_bs_cash(stmts(rows))[0].status, "mismatch")

    def test_bridge_keeps_held_for_sale_cash_flow_line_but_not_period_end_memo(self):
        flow = CF_BASE + [row("CF", "-", "매각예정자산에 포함된 현금및현금성자산", 10, "")]
        self.assertEqual(cf_bridge(stmts(flow))[0].status, "ok")  # 신한투자증권형: 실제 조정 항목
        memo = CF_BASE + [row("CF", "-", "당기말 매각예정자산집단에 포함된 현금및현금성자산", 10, "")]
        self.assertEqual(cf_bridge(stmts(memo))[0].status, "mismatch")  # 롯데지주형: 기말 구성 주석

    def test_net_change_tag_on_ending_cash_row_is_ignored(self):
        # 퀀타매트릭스 2025: '순증감' 코드가 '기말의 현금및현금성자산' 줄에 붙어 있다
        rows = CF_BASE + [
            row("CF", "ifrs-full_IncreaseDecreaseInCashAndCashEquivalents", "기말의 현금및현금성자산", 130, 100),
            row("CF", "ifrs-full_IncreaseDecreaseInCashAndCashEquivalentsBeforeEffectOfExchangeRateChanges",
                "환율변동효과 반영전 현금및현금성자산의 순증가(감소)", 20, 10)]
        self.assertEqual(cf_net_change_label(stmts(rows))[0].status, "ok")

    def test_revenue_only_restatement_with_discontinued_ops_is_reclassified(self):
        def fs(year, rev_now, rev_prior, extra):
            rows = [row("IS", "ifrs-full_Revenue", "매출액", rev_now, rev_prior),
                    row("IS", "ifrs-full_ProfitLoss", "당기순이익", 10, 10)] + extra
            return Statements({"list": rows}, "t", year, "CFS")
        disc = [row("IS", "ifrs-full_ProfitLossFromDiscontinuedOperations", "중단영업이익", 0, 5)]
        now, then = fs(2025, 90, 80, disc), fs(2024, 100, 100, [])
        f = next(x for x in restatement(now, then) if x.detail.startswith("revenue"))
        self.assertEqual((f.status, f.diff), ("reclassified", -20))
        # 중단영업 손익이 없으면 그대로 mismatch
        f = next(x for x in restatement(fs(2025, 90, 80, []), then) if x.detail.startswith("revenue"))
        self.assertEqual(f.status, "mismatch")
        # 순이익도 바뀌면 재분류로 보지 않는다
        changed = Statements({"list": [row("IS", "ifrs-full_Revenue", "매출액", 90, 80),
                                       row("IS", "ifrs-full_ProfitLoss", "당기순이익", 10, 5)] + disc}, "t", 2025, "CFS")
        f = next(x for x in restatement(changed, then) if x.detail.startswith("revenue"))
        self.assertEqual(f.status, "mismatch")

    def test_current_begin_must_equal_prior_end(self):
        [f] = cf_prior_end_is_current_begin(stmts(CF_BASE))
        self.assertEqual((f.period, f.status, f.lhs, f.rhs), ("thstrm", "ok", 100, 100))
        # 기초현금이 전기 기말현금과 다르면 mismatch, 차이 = 당기 기초 - 전기 기말
        shifted = [row("CF", "dart_CashAndCashEquivalentsAtBeginningOfPeriodCf", "기초현금", 69, 90)] + CF_BASE[1:]
        [f] = cf_prior_end_is_current_begin(stmts(shifted))
        self.assertEqual((f.status, f.diff), ("mismatch", -31))
        # 전기 기말현금이 없으면 missing
        no_prior = [row("CF", "dart_CashAndCashEquivalentsAtEndOfPeriodCf", "기말현금", 130)] + CF_BASE[:1]
        [f] = cf_prior_end_is_current_begin(stmts(no_prior))
        self.assertEqual(f.status, "missing")


if __name__ == "__main__":
    unittest.main()
