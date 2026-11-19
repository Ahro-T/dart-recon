"""Classify reconciliation findings by likely cause.

    python3 -m dart_recon.triage --year 2025 --sample 600 --seed 7

Reads results/findings_<tag>.jsonl (and the cached statements, offline) and writes
results/triage_<tag>.json. Categories are *hypotheses derived from the data*; the ones
confirmed against the filing text are listed in results/verified_cases.md.

  rounding_typo        원 단위 공시에서 |차이| <= 1만 원 (반올림·오타급)
  held_for_sale_cash   현금흐름표 기말현금이 재무상태표 현금보다 크고, 같은 기간 재무상태표에 매각예정 자산이 있음
  column_shift         '순증감' 줄의 값이 같은 공시의 기초/기말 현금 값과 같음 (줄·열 밀림 또는 계정 태깅 오류)
  restatement_discontinued  매출만 바뀌고 순이익·자산·자본은 그대로, 당해 전기 열에 중단영업 손익이 있음
  restatement          그 외 전기 숫자 변경 (재작성·재분류·연결범위 변경 등 원인은 주석 확인 필요)
  unclassified         위에 해당하지 않음
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter, defaultdict
from pathlib import Path

from . import client
from .rules import Statements, amount

RESULTS = Path(__file__).resolve().parent.parent / "results"
ROUNDING_WON = 10_000
HELD_FOR_SALE_KEYS = ("매각예정",)


def _statements(finding: dict, cache: dict) -> Statements | None:
    key = (finding["corp_code"], finding["year"], finding["fs_div"])
    if key not in cache:
        os.environ["DART_OFFLINE"] = "1"
        try:
            payload = client.financials(*key)
            cache[key] = Statements(payload, finding["corp_name"], finding["year"], finding["fs_div"])
        except (OSError, RuntimeError, ValueError):
            cache[key] = None
    return cache[key]


def _has_held_for_sale(s: Statements, period: str) -> bool:
    for r in s.rows:
        if r["sj_div"] == "BS" and any(k in r["account_nm"] for k in HELD_FOR_SALE_KEYS):
            if (amount(r.get(f"{period}_amount")) or 0) != 0:
                return True
    return False


def _has_discontinued(s: Statements) -> bool:
    for r in s.rows:
        if r["sj_div"] in ("IS", "CIS") and r["account_id"].endswith("ProfitLossFromDiscontinuedOperations"):
            if (amount(r.get("frmtrm_amount")) or 0) != 0:
                return True
    return False


def classify_finding(f: dict, s: Statements | None, restated_accounts: set[str]) -> str:
    if f["unit"] == 1 and abs(f["diff"]) <= ROUNDING_WON:
        return "rounding_typo"
    if f["rule"] == "restatement":
        only_revenue = restated_accounts == {"revenue"}
        if only_revenue and s is not None and _has_discontinued(s):
            return "restatement_discontinued"
        return "restatement"
    if s is None:
        return "unclassified"
    if f["rule"] == "cf_end_vs_bs_cash" and f["diff"] > 0 and _has_held_for_sale(s, f["period"]):
        return "held_for_sale_cash"
    if f["rule"] == "cf_net_change_label":
        p = f["period"]
        levels = {s.get("cf_begin", q) for q in ("thstrm", "frmtrm")} | {s.get("cf_end", q) for q in ("thstrm", "frmtrm")}
        if f["lhs"] in levels:
            return "column_shift"
    return "unclassified"


def triage(findings: list[dict]) -> dict:
    bad = [f for f in findings if f["status"] in ("mismatch", "rounding")]
    restated: dict[str, set[str]] = defaultdict(set)
    for f in bad:
        if f["rule"] == "restatement" and f["status"] == "mismatch":
            restated[f["corp_code"]].add(f["detail"].split(":")[0])
    cache: dict = {}
    rows = []
    for f in bad:
        cat = classify_finding(f, _statements(f, cache), restated.get(f["corp_code"], set()))
        rows.append({"category": cat, **{k: f[k] for k in ("corp_name", "corp_code", "rule", "fs_div", "period",
                                                            "status", "diff", "unit", "rcept_no")},
                     "detail": f["detail"]})
    by_cat = Counter(r["category"] for r in rows)
    corps_by_cat = {c: len({r["corp_code"] for r in rows if r["category"] == c}) for c in by_cat}
    by_rule_cat: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        by_rule_cat[r["rule"]][r["category"]] += 1
    return {
        "findings_total": len(findings),
        "non_ok_findings": len(rows),
        "corps_with_non_ok": len({r["corp_code"] for r in rows}),
        "by_category": dict(by_cat.most_common()),
        "corps_by_category": corps_by_cat,
        "by_rule_and_category": {r: dict(c) for r, c in sorted(by_rule_cat.items())},
        "rows": rows,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=2025)
    ap.add_argument("--sample", type=int, default=300)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--findings", help="override findings file (default results/findings_<tag>.jsonl)")
    args = ap.parse_args()
    tag = f"{args.year}_n{args.sample}_s{args.seed}"
    src = Path(args.findings) if args.findings else RESULTS / f"findings_{tag}.jsonl"
    findings = [json.loads(line) for line in src.read_text().splitlines() if line]
    out = triage(findings)
    (RESULTS / f"triage_{tag}.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in out.items() if k != "rows"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
