import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dart_recon import casegen, evalkit

YEAR = 2025
# 접두어 -> (기대 라벨, 회사 수)
GROUPS = {"N": ("none", 12), "R": ("rounding_or_typo", 9), "C": ("discontinued_reclass", 8), "S": ("restatement_disclosed", 11), "U": ("unexplained", 10)}


def finding(rule, status, diff):
    return {"rule": rule, "code": "x", "period": "thstrm", "status": status, "lhs": 1, "rhs": 2, "diff": diff, "unit": 1, "detail": "d", "rcept_no": "r"}


def item(rule, status, diff, category):
    return {"finding": finding(rule, status, diff), "evidence": [],
            "cause_candidate": None if category is None else {"category": category, "meaning": "m", "basis": "b"}}


class Fake:
    """캐시 없이 dart_check/dart_explain 출력 모양만 흉내 낸다."""

    def __init__(self):
        self.corps, self.check, self.explain = [], {}, {}
        n = 0

        def add(prefix, summary, items, checked=True, name=None):
            nonlocal n
            n += 1
            code = f"{n:08d}"
            name = name or f"{prefix}사{n}"
            self.corps.append({"corp_code": code, "corp_name": name})
            self.check[code] = {"corp_name": name, "corp_code": code, "year": YEAR, "rcept_no": f"2026{n:010d}", "restatement_checked": checked,
                                "summary": summary, "findings": [finding("KDQ-0001", "ok", 0), finding("KDQ-0006", "ok", 0)]}
            if items is not None:
                self.explain[code] = {"corp_name": name, "corp_code": code, "year": YEAR, "non_ok_count": len(items), "items": items}
            return code

        for i in range(GROUPS["N"][1]):
            add("N", {"ok": 16}, None)
        for i in range(GROUPS["R"][1]):
            add("R", {"ok": 14, "mismatch": 2}, [item("KDQ-0005", "mismatch", 3000 + i, "rounding_typo"), item("KDQ-0002", "rounding", -5, "rounding_typo")])
        for i in range(GROUPS["C"][1]):
            add("C", {"ok": 15, "reclassified": 1}, [item("KDQ-0006", "reclassified", 5_000_000 + i, None)])
        for i in range(GROUPS["S"][1]):
            add("S", {"ok": 14, "mismatch": 2}, [item("KDQ-0006", "mismatch", 7_000_000 + i, "restatement"), item("KDQ-0006", "mismatch", 8_000_000 + i, "restatement")])
        for i in range(GROUPS["U"][1]):
            add("U", {"ok": 15, "mismatch": 1}, [item("KDQ-0004", "mismatch", 9_000_000 + i, "unclassified")])
        # 제외 대상
        add("M", {"ok": 14, "mismatch": 2}, [item("KDQ-0006", "mismatch", 1_000_000, "restatement"), item("KDQ-0004", "mismatch", 2_000_000, "unclassified")], name="혼합사")
        add("X", {"ok": 14, "missing": 1, "mismatch": 1}, [item("KDQ-0004", "mismatch", 4_000_000, "unclassified")], name="누락사")
        add("Y", {"ok": 10}, None, checked=False, name="전기없음사")
        add("H", {"ok": 15, "mismatch": 1}, [item("KDQ-0002", "mismatch", 6_000_000, "held_for_sale_cash")], name="매각예정사")
        add("V", {"ok": 16}, None, name="검증사")
        add("D", {"ok": 16}, None, name="동명사")
        add("D", {"ok": 16}, None, name="동명사")
        self.by_name = {}
        for c in self.corps:
            self.by_name.setdefault(c["corp_name"], []).append(c["corp_code"])

    def code_of(self, name):
        return self.by_name[name][0]

    def __call__(self, name, args):
        key = args["corp"]
        if key not in self.check:
            codes = self.by_name.get(key, [])
            if len(codes) != 1:
                raise RuntimeError(f"회사를 찾지 못함 또는 모호: {key}")
            key = codes[0]
        return self.check[key] if name == "dart_check" else self.explain[key]


VERIFIED = [{"id": "v-one", "question": "q", "corp": "검증사", "year": YEAR, "expected_cause": "none", "expected_basis": "api_only",
             "required_facts": ["KDQ-0001"], "forbidden": [], "split": "heldout", "protected": False}]


class Casegen(unittest.TestCase):
    def setUp(self):
        self.fake = Fake()
        self.cases = casegen.generate(7, self.fake.corps, self.fake, VERIFIED, YEAR)
        self.derived = [c for c in self.cases if c["tier"] == "derived"]

    def test_deterministic_and_seed_sensitive(self):
        again = casegen.generate(7, list(reversed(self.fake.corps)), Fake(), VERIFIED, YEAR)
        self.assertEqual(json.dumps(self.cases), json.dumps(again))  # 입력 순서·인스턴스와 무관
        other = casegen.generate(8, self.fake.corps, Fake(), VERIFIED, YEAR)
        self.assertNotEqual([c["id"] for c in self.derived], [c["id"] for c in other if c["tier"] == "derived"])

    def test_verified_preserved_first_with_tier(self):
        self.assertEqual(self.cases[0], {**VERIFIED[0], "tier": "verified"})
        self.assertEqual(sum(c["tier"] == "verified" for c in self.cases), 1)
        self.assertNotIn("검증사", [c["corp"] for c in self.derived])  # verified 회사는 derived에서 제외

    def test_label_rules_and_exclusions(self):
        by_prefix = {}
        for c in self.derived:
            by_prefix.setdefault(c["corp"][0], set()).add(c["expected_cause"])
        self.assertEqual(by_prefix, {"N": {"none"}, "R": {"rounding_or_typo"}, "C": {"discontinued_reclass"},
                                     "S": {"restatement_disclosed"}, "U": {"unexplained"}})
        names = {c["corp"] for c in self.derived}
        for excluded in ("혼합사", "누락사", "전기없음사", "매각예정사", "동명사"):
            self.assertNotIn(excluded, names)
        self.assertEqual(len(names), len(self.derived))

    def test_counts_capped_and_balanced(self):
        counts = {}
        for c in self.derived:
            counts[c["expected_cause"]] = counts.get(c["expected_cause"], 0) + 1
        self.assertEqual(counts, {"none": 8, "rounding_or_typo": 7, "discontinued_reclass": 6, "restatement_disclosed": 8, "unexplained": 8})
        small = casegen.generate(7, self.fake.corps, self.fake, [], YEAR, caps={**casegen.CAPS, "none": 2})
        self.assertEqual(sum(c["expected_cause"] == "none" for c in small), 2)

    def test_accepted_causes_only_for_restatement(self):
        for c in self.derived:
            if c["expected_cause"] == "restatement_disclosed":
                self.assertEqual(c["accepted_causes"], ["restatement_disclosed", "unexplained"])
            else:
                self.assertNotIn("accepted_causes", c)
            self.assertEqual(evalkit.case_errors(c), [])

    def test_required_facts_come_from_tool_text(self):
        for c in self.derived:
            code = self.fake.code_of(c["corp"])
            text = json.dumps(self.fake.check[code], ensure_ascii=False) + json.dumps(self.fake.explain.get(code, {}), ensure_ascii=False)
            self.assertTrue(1 <= len(c["required_facts"]) <= 3)
            for f in c["required_facts"]:
                self.assertIn(f, text)
        none = next(c for c in self.derived if c["expected_cause"] == "none")
        self.assertEqual(none["required_facts"][1], "KDQ-0006")
        self.assertTrue(re.fullmatch(r"2026\d{10}", none["required_facts"][0]))
        rounding = next(c for c in self.derived if c["expected_cause"] == "rounding_or_typo")
        self.assertEqual(rounding["required_facts"][0], "KDQ-0005")
        self.assertEqual(len(rounding["required_facts"]), 2)  # 1000 미만 차이(5)는 사실로 쓰지 않는다

    def test_questions_do_not_depend_on_category(self):
        templates_by_cause = {}
        for c in self.derived:
            self.assertEqual(c["question"], casegen.question_for(c["corp"], self.fake.code_of(c["corp"]), YEAR))
            templates_by_cause.setdefault(c["expected_cause"], set()).add(c["question"].replace(c["corp"], "{corp}"))
            for leak in ("재작성", "중단영업", "반올림", "오타", "재분류", "원인 불명", "정합"):
                self.assertNotIn(leak, c["question"])
        self.assertLessEqual(len(set().union(*templates_by_cause.values())), 3)
        self.assertTrue(all(len(t) >= 2 for t in templates_by_cause.values()))  # 범주마다 여러 템플릿이 섞인다

    def test_splits_stratified_and_verified_never_test(self):
        self.assertEqual(casegen.split_counts(8), (3, 2, 3))
        self.assertEqual(casegen.split_counts(6), (2, 2, 2))
        for cause in ("none", "rounding_or_typo", "discontinued_reclass", "restatement_disclosed", "unexplained"):
            splits = [c["split"] for c in self.derived if c["expected_cause"] == cause]
            got = (splits.count("train"), splits.count("heldout"), splits.count("test"))
            self.assertEqual(got, casegen.split_counts(len(splits)))
            self.assertTrue(all(got))
        self.assertTrue(all(c["protected"] is False for c in self.derived))
        self.assertEqual(self.cases[0]["split"], "heldout")  # verified의 기존 분할 유지

    def test_forbidden_patterns(self):
        for c in self.derived:
            extra = [p for p in c["forbidden"] if p not in casegen.FORBIDDEN_COMMON]
            self.assertEqual(extra, [casegen.FORBIDDEN_NONE] if c["expected_cause"] == "none" else [])
        pat = casegen.FORBIDDEN_NONE
        self.assertIsNotNone(re.search(pat, "재무제표에 불일치가 있습니다"))
        for bad in ("불일치가 발견됩니다", "오류가 확인됩니다", "문제가 있습니다", "불일치가 존재합니다"):
            self.assertIsNotNone(re.search(pat, bad), bad)
        for ok in ("불일치가 없습니다", "재작성이 아닙니다", "문제 없이 정합합니다", "오류는 발견되지 않았습니다", "불일치가 존재하지 않습니다"):
            self.assertIsNone(re.search(pat, ok), ok)
        for p in casegen.FORBIDDEN_COMMON:
            self.assertIsNone(re.search(p, "재작성 아님. 중단영업 등 항목이 없고 전기 숫자가 다릅니다"))

    def test_main_rewrites_derived_and_keeps_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, findings = Path(tmp) / "cases.jsonl", Path(tmp) / "findings.jsonl"
            findings.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in self.fake.corps * 2), encoding="utf-8")  # 중복 줄 허용
            stale = {**self.derived[0], "id": "d-stale", "corp": "옛회사"}
            out.write_text(json.dumps({**VERIFIED[0], "tier": "verified"}, ensure_ascii=False) + "\n" + json.dumps(stale, ensure_ascii=False) + "\n", encoding="utf-8")
            with mock.patch.object(casegen, "default_call", self.fake):
                casegen.main(["--seed", "7", "--out", str(out), "--findings", str(findings)])
                first = out.read_bytes()
                casegen.main(["--seed", "7", "--out", str(out), "--findings", str(findings)])
            self.assertEqual(first, out.read_bytes())
            rows = [json.loads(x) for x in first.decode("utf-8").splitlines()]
            self.assertNotIn("d-stale", [r["id"] for r in rows])
            self.assertEqual(rows[0], {**VERIFIED[0], "tier": "verified"})
            self.assertEqual(rows[1:], self.derived)


if __name__ == "__main__":
    unittest.main()
