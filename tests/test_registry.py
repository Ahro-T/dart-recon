"""관문: 모든 규칙은 ID, 문서(rules/<ID>.md), 테스트를 갖는다."""

import re
import unittest
from pathlib import Path

from dart_recon.rules import RULE_IDS, SINGLE_RULES, restatement

ROOT = Path(__file__).resolve().parent.parent
REQUIRED_SECTIONS = ("## 검사", "## 근거", "## 해결하는 문제", "## 상태와 허용오차", "## 알려진 예외", "## 변경 이력")


class Registry(unittest.TestCase):
    def test_every_rule_has_unique_id(self):
        names = [f.__name__ for f in (*SINGLE_RULES, restatement)]
        self.assertEqual(sorted(names), sorted(RULE_IDS))
        ids = list(RULE_IDS.values())
        self.assertEqual(len(ids), len(set(ids)))
        for rule_id in ids:
            self.assertRegex(rule_id, r"^KDQ-\d{4}$")

    def test_every_rule_has_document(self):
        for name, rule_id in RULE_IDS.items():
            doc = ROOT / "rules" / f"{rule_id}.md"
            self.assertTrue(doc.exists(), f"{doc} 없음")
            text = doc.read_text()
            self.assertIn(f"규칙 ID: {rule_id}", text)
            self.assertIn(f"코드: `{name}`", text)
            for section in REQUIRED_SECTIONS:
                self.assertIn(section, text, f"{doc.name}: {section} 없음")

    def test_no_orphan_documents(self):
        docs = {p.stem for p in (ROOT / "rules").glob("KDQ-*.md")}
        self.assertEqual(docs, set(RULE_IDS.values()))

    def test_every_rule_is_exercised_by_a_test(self):
        source = (ROOT / "tests" / "test_rules.py").read_text()
        for name in RULE_IDS:
            self.assertTrue(re.search(rf"\b{name}\(", source), f"tests/test_rules.py가 {name}을 호출하지 않음")


if __name__ == "__main__":
    unittest.main()
