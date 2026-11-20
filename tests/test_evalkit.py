import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dart_recon import client, evalkit

REAL_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REAL_ROOT / "tests" / "fixtures" / "raw"


def amounts(text):
    return [(a["value"], a["tol"]) for a in evalkit.extract_amounts(text)]


class Amounts(unittest.TestCase):
    def test_plain_with_commas(self):
        self.assertEqual(amounts("차이는 359,586,889원입니다"), [(359586889, 0)])

    def test_excludes_ids_years_dates_percent_small(self):
        self.assertEqual(amounts("KDQ-0006 규칙, 2025년 보고서, 2026-03-11 공시, 13.6% 감소, 500원"), [])

    def test_eok_forms_with_rounding_tolerance(self):
        (v, tol), = amounts("약 3,860억원")
        self.assertEqual((v, tol), (386_000_000_000, 0.5e8))
        (v, tol), = amounts("3,859.7억")
        self.assertAlmostEqual(v, 385_970_000_000)
        self.assertAlmostEqual(tol, 0.05e8)

    def test_compound_and_man_forms(self):
        (v, _), = amounts("1조 2,000억")
        self.assertEqual(v, 1_200_000_000_000)
        (v, tol), = amounts("1,123만원")
        self.assertEqual((v, tol), (11_230_000, 5000))
        (v, _), = amounts("385,968백만원")
        self.assertEqual(v, 385_968_000_000)

    def test_ungrouped_numbers_split_not_merged(self):
        self.assertEqual([a["value"] for a in evalkit.extract_amounts("12,345 와 67,890")], [12345, 67890])


class Grounding(unittest.TestCase):
    RECS = [{"tool": "dart_check", "args": {}, "result": {
        "findings": [{"lhs": 2452219662527, "rhs": 2838188216213, "diff": -385968553686, "rcept_no": "20260311004423"}],
        "evidence": [{"thstrm_amount": "5000000", "frmtrm_amount": "3500000"}]}}]

    def h(self, text):
        return evalkit.hallucinated_amounts(text, self.RECS)

    def test_raw_and_diff_grounded(self):
        self.assertEqual(self.h("전기 매출 2,452,219,662,527원, 차이 385,968,553,686원(음수 -385,968,553,686)"), [])

    def test_eok_rounding_grounded(self):
        self.assertEqual(self.h("약 3,860억원 감소"), [])
        self.assertEqual(self.h("약 3,859.7억원 감소"), [])

    def test_eok_outside_rounding_is_hallucinated(self):
        self.assertEqual(self.h("약 3,900억원 감소"), ["3900억"])

    def test_invented_number(self):
        self.assertEqual(self.h("차이는 123,456,789원"), ["123456789"])

    def test_derived_diff_within_object_grounded(self):
        self.assertEqual(self.h("전기와 당기 차이 1,500,000원"), [])

    def test_numbers_inside_detail_strings_grounded(self):
        recs = [{"result": {"detail": "사용: 매각예정자산집단에 포함된 현금=359586889"}}]
        self.assertEqual(evalkit.hallucinated_amounts("현금 359,586,889원", recs), [])

    def test_no_tool_calls_means_everything_ungrounded(self):
        self.assertEqual(evalkit.hallucinated_amounts("1,000원", []), ["1000"])


class Recall(unittest.TestCase):
    def test_numeric_fact_normalization_and_boundaries(self):
        r, miss = evalkit.facts_recall("차이 3,100,000,000원, KDQ-0005", ["3100000000", "kdq-0005", "1000"])
        self.assertEqual(miss, ["1000"])  # 3,100,000,000 안의 000을 1000으로 오인하지 않는다
        self.assertAlmostEqual(r, 2 / 3)

    def test_unit_form_matches_numeric_fact(self):
        self.assertEqual(evalkit.facts_recall("감소액은 약 3,860억원", ["385968553686"])[0], 1.0)

    def test_whitespace_insensitive_token_and_empty(self):
        self.assertEqual(evalkit.facts_recall("규칙 KDQ - 0006", ["KDQ-0006"])[0], 1.0)
        self.assertEqual(evalkit.facts_recall("x", [])[0], 1.0)


CASE = {"id": "c1", "split": "train", "protected": False, "expected_cause": "none", "expected_basis": "api_only",
        "required_facts": ["KDQ-0006", "1000"], "forbidden": ["분식"]}
TOOLS = [{"tool": "dart_check", "args": {}, "result": {"diff": 1000, "rule": "KDQ-0006"}}]


class Scoring(unittest.TestCase):
    def ans(self, text, cause="none", basis="api_only"):
        return {"case_id": "c1", "answer": text, "cause": cause, "basis": basis}

    def test_perfect(self):
        s = evalkit.score_case(CASE, self.ans("KDQ-0006 차이 1,000원"), TOOLS)
        self.assertEqual((s["cause_match"], s["facts_recall"], s["basis_ok"], s["score"]), (1, 1.0, 1, 1.0))

    def test_partial_mean(self):
        s = evalkit.score_case(CASE, self.ans("KDQ-0006만 언급", cause="unexplained"), TOOLS)
        self.assertAlmostEqual(s["score"], (0 + 0.5 + 1) / 3, places=3)
        self.assertEqual(s["missing_facts"], ["1000"])

    def test_hallucination_zeroes_score(self):
        s = evalkit.score_case(CASE, self.ans("KDQ-0006 차이 1,000원, 합계 9,999,999원"), TOOLS)
        self.assertEqual(s["hallucinated_amounts"], ["9999999"])
        self.assertEqual(s["score"], 0.0)

    def test_forbidden_zeroes_score(self):
        s = evalkit.score_case(CASE, self.ans("KDQ-0006 1,000원 분식 의심"), TOOLS)
        self.assertEqual((s["forbidden_hits"], s["score"]), (["분식"], 0.0))

    def test_unknown_basis_never_ok(self):
        self.assertEqual(evalkit.score_case(CASE, self.ans("KDQ-0006 1,000원", basis="unknown"), TOOLS)["basis_ok"], 0)

    def test_missing_answer(self):
        s = evalkit.score_case(CASE, None, [])
        self.assertEqual((s["missing_answer"], s["score"]), (True, 0.0))

    def test_aggregate_splits_with_n(self):
        per = {"a": {**evalkit.score_case(CASE, None, []), "score": 1.0}, "b": {**evalkit.score_case({**CASE, "split": "heldout"}, None, [])}}
        agg = evalkit.aggregate(per)
        self.assertEqual((agg["train"]["n"], agg["train"]["mean_score"]), (1, 1.0))
        self.assertEqual((agg["heldout"]["n"], agg["heldout"]["mean_score"], agg["all"]["n"]), (1, 0.0, 2))


class TmpRoot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(evalkit, "ROOT", self.root)
        p.start()
        self.addCleanup(p.stop)
        (self.root / "eval").mkdir()
        shutil_copy = (REAL_ROOT / "eval" / "judge_schema.json").read_text(encoding="utf-8")
        (self.root / "eval" / "judge_schema.json").write_text(shutil_copy, encoding="utf-8")
        self.cases = [
            {"id": "t1", "question": "q1", "corp": "A", "year": 2025, "expected_cause": "none", "expected_basis": "api_only",
             "required_facts": ["KDQ-0001"], "forbidden": [], "split": "train", "protected": True},
            {"id": "t2", "question": "q2", "corp": "B", "year": 2025, "expected_cause": "unexplained", "expected_basis": "api_only",
             "required_facts": ["KDQ-0006"], "forbidden": [], "split": "train", "protected": False},
            {"id": "h1", "question": "h-secret-question", "corp": "C", "year": 2025, "expected_cause": "none", "expected_basis": "api_only",
             "required_facts": ["KDQ-0001"], "forbidden": [], "split": "heldout", "protected": False},
        ]
        (self.root / "eval" / "cases.jsonl").write_text("\n".join(json.dumps(c, ensure_ascii=False) for c in self.cases) + "\n", encoding="utf-8")
        sk = self.root / "skills" / "dart-recon"
        sk.mkdir(parents=True)
        (sk / "SKILL.v0.md").write_text("SKILL V0 TEXT", encoding="utf-8")

    def write_answer(self, rnd, skill, cid, text, cause="none", basis="api_only", tools=None):
        d = evalkit.run_dir(rnd, skill)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{cid}.answer.json").write_text(json.dumps({"case_id": cid, "answer": text, "cause": cause, "basis": basis}, ensure_ascii=False), encoding="utf-8")
        if tools is not None:
            (d / f"{cid}.tools.jsonl").write_text("".join(json.dumps(t, ensure_ascii=False) + "\n" for t in tools), encoding="utf-8")


class Run(TmpRoot):
    def test_prompt_contains_skill_question_and_paths_but_no_expectations(self):
        p = evalkit.analyst_prompt("t1", "v0", 3)
        for s in ("SKILL V0 TEXT", "q1", "--round 3 --skill v0 --case t1", "results/loop/runs/r3/v0/t1.answer.json", "unexplained"):
            self.assertIn(s, p)
        self.assertNotIn("KDQ-0001", p)

    def test_tool_runner_logs_with_fixture_cache(self):
        with mock.patch.object(client, "CACHE", FIXTURES):
            out = evalkit.run_tool(0, "v0", "t1", "dart_check", {"corp": "켐트로닉스", "year": 2025})
            evalkit.run_tool(0, "v0", "t1", "dart_check", {"corp": "없는회사", "year": 2025})
        recs = evalkit.read_jsonl(evalkit.run_dir(0, "v0") / "t1.tools.jsonl")
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[0]["tool"], "dart_check")
        self.assertEqual(recs[0]["result"], json.loads(out))
        self.assertTrue(recs[1]["is_error"])

    def test_tool_runner_rejects_unknown_tool_and_bad_ids(self):
        with self.assertRaises(SystemExit):
            evalkit.run_tool(0, "v0", "t1", "rm", {})
        with self.assertRaises(SystemExit):
            evalkit.run_tool(0, "v0", "../x", "dart_rules", {})

    def test_score_run_writes_scores_and_missing_counts_zero(self):
        self.write_answer(0, "v0", "t1", "KDQ-0001 정합", tools=[])
        res = evalkit.score_run(0, "v0")
        self.assertEqual(res["cases"]["t1"]["score"], 1.0)
        self.assertTrue(res["cases"]["t2"]["missing_answer"])
        self.assertEqual(res["aggregates"]["train"]["n"], 2)
        self.assertEqual(res["aggregates"]["train"]["mean_score"], 0.5)
        self.assertTrue((evalkit.run_dir(0, "v0") / "scores.json").exists())


def good_judge(rnd=0, skill="v0", ids=("t1", "t2", "h1"), label="grounded", correctness=5):
    return {"round": rnd, "skill": skill, "cases": [
        {"case_id": i, "claims": [{"text": "c", "label": label, "evidence_ref": "line 1"}],
         "rubric": {"correctness": correctness, "grounding": 4, "calibration": 4}, "failure_types": [], "diagnosis": "ok"} for i in ids]}


class JudgeSchema(TmpRoot):
    def errs(self, doc):
        return evalkit.validate(doc, evalkit.judge_schema())

    def test_valid(self):
        self.assertEqual(self.errs(good_judge()), [])

    def test_invalid_variants(self):
        d = good_judge(); d["cases"][0]["claims"][0]["label"] = "maybe"
        self.assertTrue(self.errs(d))
        d = good_judge(); d["cases"][0]["rubric"]["correctness"] = 6
        self.assertTrue(self.errs(d))
        d = good_judge(); d["cases"][0]["failure_types"] = ["bogus"]
        self.assertTrue(self.errs(d))
        d = good_judge(); del d["cases"][0]["diagnosis"]
        self.assertTrue(self.errs(d))
        d = good_judge(); d["extra"] = 1
        self.assertTrue(self.errs(d))
        d = good_judge(); d["cases"][0]["rubric"]["correctness"] = True
        self.assertTrue(self.errs(d))

    def test_validate_command_reports_agreement(self):
        self.write_answer(0, "v0", "t1", "KDQ-0001 합계 9,999,999원", tools=[])  # hallucinated, cause ok
        self.write_answer(0, "v0", "t2", "KDQ-0006", cause="none", tools=[])  # wrong cause
        evalkit.score_run(0, "v0")
        d = good_judge(ids=("t1", "t2", "h1"))
        d["cases"][0]["claims"][0]["label"] = "hallucinated"
        d["cases"][1]["rubric"]["correctness"] = 2
        jp = evalkit.run_dir(0, "v0") / "judge.json"
        jp.write_text(json.dumps(d), encoding="utf-8")
        res = evalkit.judge_validate(0, "v0")
        self.assertTrue(res["valid"], res)
        ag = json.loads((evalkit.run_dir(0, "v0") / "agreement.json").read_text())
        rows = {r["case_id"]: r for r in ag["cases"]}
        self.assertTrue(rows["t1"]["hallucination_agree"])
        self.assertTrue(rows["t2"]["correctness_agree"])  # 판정 정확도 낮음 vs cause_match 0
        self.assertEqual(ag["n"], 2)  # 답변 없는 h1은 제외
        self.assertIn("agreement", json.loads((evalkit.run_dir(0, "v0") / "scores.json").read_text()))

    def test_validate_command_rejects_unknown_or_missing_case(self):
        self.write_answer(0, "v0", "t1", "KDQ-0001", tools=[])
        evalkit.score_run(0, "v0")
        jp = evalkit.run_dir(0, "v0") / "judge.json"
        jp.write_text(json.dumps(good_judge(ids=("t1", "zzz"))), encoding="utf-8")
        res = evalkit.judge_validate(0, "v0")
        self.assertFalse(res["valid"])
        self.assertTrue(any("zzz" in e for e in res["errors"]) and any("t2" in e for e in res["errors"]))
        jp.write_text("{not json", encoding="utf-8")
        self.assertFalse(evalkit.judge_validate(0, "v0")["valid"])


def mk_scores(rnd, skill, train, heldout_score, hall=0, protected_score=1.0):
    """train: {id: score}; t1 보호 케이스."""
    cases = {}
    for cid, sc in train.items():
        cases[cid] = {"split": "train", "protected": cid == "t1", "missing_answer": False, "cause_match": 1, "facts_recall": 1.0, "missing_facts": [],
                      "basis_ok": 1, "hallucinated_amounts": [], "forbidden_hits": [], "score": sc}
    cases["h1"] = {"split": "heldout", "protected": False, "missing_answer": False, "cause_match": 1, "facts_recall": 1.0, "missing_facts": [],
                   "basis_ok": 1, "hallucinated_amounts": ["1"] * hall, "forbidden_hits": [], "score": heldout_score}
    return {"round": rnd, "skill": skill, "cases": cases, "aggregates": evalkit.aggregate(cases)}


class Gate(TmpRoot):
    def setup_round(self, base, cand, target=("t2",), cand_text="CAND TEXT"):
        runs = evalkit.run_dir(1, "v0").parent
        for v, s in (("v0", base), ("v1", cand)):
            evalkit.run_dir(1, v).mkdir(parents=True, exist_ok=True)
            (evalkit.run_dir(1, v) / "scores.json").write_text(json.dumps(s), encoding="utf-8")
        (runs / "candidate.json").write_text(json.dumps({"target_failures": ["wrong_cause"], "target_case_ids": list(target),
                                                         "change_summary": "cs", "scope": "sc"}), encoding="utf-8")
        cd = self.root / "skills" / "dart-recon" / "candidates"
        cd.mkdir(parents=True, exist_ok=True)
        (cd / "v1.md").write_text(cand_text, encoding="utf-8")

    def gate(self):
        activated = []
        line = evalkit.run_gate(1, "v0", "v1", activate=activated.append)
        return line, activated

    BASE = lambda self: mk_scores(1, "v0", {"t1": 1.0, "t2": 0.3}, 0.5)

    def test_pass_adopts_copies_activates_and_logs(self):
        self.setup_round(self.BASE(), mk_scores(1, "v1", {"t1": 1.0, "t2": 0.9}, 0.5))
        line, activated = self.gate()
        self.assertEqual(line, "round=1 candidates=1 adopted=1 failed_gates=none")
        self.assertEqual(activated, ["v1"])
        self.assertEqual((self.root / "skills" / "dart-recon" / "SKILL.v1.md").read_text(), "CAND TEXT")
        rec = evalkit.read_jsonl(evalkit.ledger_path())
        self.assertEqual(len(rec), 1)
        self.assertTrue(rec[0]["adopted"])
        self.assertEqual(set(rec[0]["gates"]), {"G1", "G2", "G3", "G4"})

    def check_rejected(self, cand, expect, **kw):
        self.setup_round(self.BASE(), cand, **kw)
        line, activated = self.gate()
        self.assertEqual(line, f"round=1 candidates=1 adopted=0 failed_gates={expect}")
        self.assertEqual(activated, [])
        self.assertFalse((self.root / "skills" / "dart-recon" / "SKILL.v1.md").exists())
        lesson = (self.root / "lessons" / "r1-v1.md").read_text(encoding="utf-8")
        self.assertIn(expect, lesson)
        self.assertIn("cs", lesson)

    def test_g1_target_not_improved(self):
        self.check_rejected(mk_scores(1, "v1", {"t1": 1.0, "t2": 0.3}, 0.5), "G1_targeted")

    def test_g1_target_outside_train_or_empty(self):
        self.check_rejected(mk_scores(1, "v1", {"t1": 1.0, "t2": 0.9}, 0.5), "G1_targeted", target=("h1",))

    def test_g2_protected_regression(self):
        self.check_rejected(mk_scores(1, "v1", {"t1": 0.9, "t2": 0.9}, 0.5), "G2_protected")

    def test_g3_hallucination(self):
        self.check_rejected(mk_scores(1, "v1", {"t1": 1.0, "t2": 0.9}, 0.5, hall=1), "G3_hallucination")

    def test_g4_heldout_lower(self):
        self.check_rejected(mk_scores(1, "v1", {"t1": 1.0, "t2": 0.9}, 0.4), "G4_heldout")

    def test_multiple_failures_listed_and_heldout_equal_is_ok(self):
        self.setup_round(self.BASE(), mk_scores(1, "v1", {"t1": 0.5, "t2": 0.3}, 0.5))
        line, _ = self.gate()
        self.assertEqual(line, "round=1 candidates=1 adopted=0 failed_gates=G1_targeted,G2_protected")

    def test_ledger_appends(self):
        self.setup_round(self.BASE(), mk_scores(1, "v1", {"t1": 1.0, "t2": 0.3}, 0.5))
        self.gate()
        (self.root / "skills" / "dart-recon" / "candidates" / "v1.md").write_text("x")
        self.gate()
        self.assertEqual(len(evalkit.read_jsonl(evalkit.ledger_path())), 2)

    def test_case_set_mismatch_is_error(self):
        b = self.BASE()
        c = mk_scores(1, "v1", {"t1": 1.0}, 0.5)
        self.setup_round(b, c)
        with self.assertRaises(SystemExit):
            self.gate()


class ImproveAndReport(Gate):
    def test_improve_prompt_hides_heldout_and_shows_train_failures_and_lessons(self):
        s = mk_scores(0, "v0", {"t1": 1.0, "t2": 0.3}, 0.1)
        evalkit.run_dir(0, "v0").mkdir(parents=True)
        (evalkit.run_dir(0, "v0") / "scores.json").write_text(json.dumps(s), encoding="utf-8")
        (self.root / "lessons").mkdir()
        (self.root / "lessons" / "r0-v1.md").write_text("LESSON BODY", encoding="utf-8")
        p = evalkit.improve_prompt(0, "v0")
        self.assertIn("### t2", p)
        self.assertNotIn("### t1", p)  # 만점 케이스는 실패 목록에 없다
        self.assertIn("t1: 현재 점수 1.0", p)  # 보호 케이스로는 표시
        self.assertIn("LESSON BODY", p)
        self.assertIn("SKILL V0 TEXT", p)
        self.assertIn("candidates/v1.md", p)
        for leak in ("h1", "h-secret-question"):
            self.assertNotIn(leak, p)

    def test_next_version_skips_rejected_candidates(self):
        cand = self.root / "skills" / "dart-recon" / "candidates"
        cand.mkdir(parents=True, exist_ok=True)
        self.assertEqual(evalkit.next_version("v0"), "v1")
        (cand / "v1.md").write_text("rejected", encoding="utf-8")
        self.assertEqual(evalkit.next_version("v0"), "v2")  # 기각된 v1을 덮어쓰지 않는다
        (cand.parent / "SKILL.v3.md").write_text("adopted", encoding="utf-8")
        self.assertEqual(evalkit.next_version("v3"), "v4")

    def test_report_rows_and_status(self):
        self.setup_round(self.BASE(), mk_scores(1, "v1", {"t1": 1.0, "t2": 0.9}, 0.4))
        self.gate()
        rep = evalkit.write_report()
        self.assertEqual([(r["skill"], r["status"]) for r in rep["rows"]], [("v0", "baseline"), ("v1", "rejected: G4_heldout")])
        self.assertEqual((rep["adopted"], rep["rejected"]), (0, 1))
        md = (self.root / "results" / "loop" / "summary.md").read_text(encoding="utf-8")
        self.assertIn("| 1 | v1 |", md)
        self.assertTrue((self.root / "results" / "loop" / "summary.json").exists())


class RepoCases(unittest.TestCase):
    """저장소의 실제 eval/cases.jsonl 구조 점검(데이터 캐시는 쓰지 않는다)."""

    def test_cases_wellformed(self):
        cases = [json.loads(x) for x in (REAL_ROOT / "eval" / "cases.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
        ids = [c["id"] for c in cases]
        self.assertEqual(len(ids), len(set(ids)))
        for c in cases:
            self.assertEqual(set(c), {"id", "question", "corp", "year", "expected_cause", "expected_basis", "required_facts", "forbidden", "split", "protected"})
            self.assertIn(c["expected_cause"], evalkit.TAXONOMY)
            self.assertIn(c["expected_basis"], ("original_checked", "api_only"))
            self.assertIn(c["split"], ("train", "heldout"))
            self.assertTrue(c["required_facts"])
            for pat in c["forbidden"]:
                __import__("re").compile(pat)
            if c["protected"]:
                self.assertEqual(c["split"], "train")
        self.assertTrue(10 <= len(cases) <= 12)


if __name__ == "__main__":
    unittest.main()
