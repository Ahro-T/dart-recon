import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dart_recon import client, pit


def payload(year, rows, rcept):
    """rows: (account_id, thstrm, frmtrm)"""
    return {"status": "000", "list": [
        {"corp_code": "00000001", "rcept_no": rcept, "sj_div": "BS", "account_id": aid, "account_nm": aid,
         "account_detail": "-", "thstrm_amount": str(cur), "frmtrm_amount": str(prev)}
        for aid, cur, prev in rows]}


class Pit(unittest.TestCase):
    def setUp(self):
        self.conn = pit.connect(":memory:")
        # 2024 보고서: 자산 당기 1000, 전기 900 / 자본 당기 400, 전기 300
        self.r24 = payload(2024, [("ifrs-full_Assets", 1000, 900), ("ifrs-full_Equity", 400, 300)], "A24")
        # 2025 보고서: 전기(=2024) 자산이 1000 -> 1100으로 재작성, 자본은 그대로
        self.r25 = payload(2025, [("ifrs-full_Assets", 1500, 1100), ("ifrs-full_Equity", 600, 400)], "A25")

    def load_both(self):
        pit.load_payload(self.conn, self.r24, "가짜", 2024, "CFS")
        pit.load_payload(self.conn, self.r25, "가짜", 2025, "CFS")

    def values(self, view, account):
        return self.conn.execute(
            f"SELECT value, reported_in_year FROM {view} WHERE fiscal_year=2024 AND account_key LIKE ?",
            (f"%{account}%",)).fetchall()

    def test_first_and_latest_differ_for_restated_value(self):
        self.load_both()
        self.assertEqual(self.values("as_first_reported", "Assets"), [(1000, 2024)])
        self.assertEqual(self.values("latest", "Assets"), [(1100, 2025)])
        self.assertEqual(self.values("as_first_reported", "Equity"), [(400, 2024)])
        self.assertEqual(self.values("latest", "Equity"), [(400, 2025)])

    def test_restatement_candidates_only_changed_value(self):
        self.load_both()
        rows = self.conn.execute("SELECT account_key, first_value, latest_value, diff FROM restatement_candidates").fetchall()
        self.assertEqual(rows, [("BS|ifrs-full_Assets|-", 1000, 1100, 100)])
        s = pit.summarize(self.conn)
        self.assertEqual((s["restatement_candidate_rows"], s["restatement_candidate_corps"]), (1, 1))

    def test_loading_twice_is_idempotent(self):
        self.load_both()
        before = self.conn.execute("SELECT * FROM pit ORDER BY 1,2,3,4,5,7").fetchall()
        self.load_both()
        after = self.conn.execute("SELECT * FROM pit ORDER BY 1,2,3,4,5,7").fetchall()
        self.assertEqual(before, after)
        self.assertEqual(len(after), 8)  # 2 보고서 x 2 계정 x (당기, 전기)

    def test_duplicate_key_in_one_report_is_skipped(self):
        dup = payload(2025, [("ifrs-full_Assets", 1, 2), ("ifrs-full_Assets", 3, 4)], "A25")
        self.assertEqual(pit.load_payload(self.conn, dup, "가짜", 2025, "CFS"), (0, 1))

    def test_failed_payload_loads_nothing(self):
        self.assertEqual(pit.load_payload(self.conn, {"status": "013", "list": []}, "가짜", 2025, "CFS"), (0, 0))


class ApiKeyPath(unittest.TestCase):
    def test_reads_repo_root_env(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".env").write_text("DART_API_KEY=abc\n")
            with mock.patch.dict("os.environ", {}, clear=True), mock.patch.object(client, "ROOT", root):
                self.assertEqual(client.api_key(), "abc")


if __name__ == "__main__":
    unittest.main()
