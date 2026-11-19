"""Run reconciliation rules over a sample of listed companies.

    python3 -m dart_recon.run --year 2025 --sample 300 --seed 7
"""

from __future__ import annotations

import argparse
import json
import random
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import client
from .rules import Statements, restatement, run_single

RESULTS = Path(__file__).resolve().parent.parent / "results"


def load(corp: dict, year: int) -> Statements | None:
    """Consolidated statements if filed, otherwise separate (별도)."""
    for fs_div in ("CFS", "OFS"):
        payload = client.financials(corp["corp_code"], year, fs_div)
        if payload.get("status") == "000" and payload.get("list"):
            return Statements(payload, corp["corp_name"], year, fs_div)
    return None


def check_corp(corp: dict, year: int) -> tuple[str, list[dict]]:
    try:
        return _check_corp(corp, year)
    except (OSError, RuntimeError):
        return "fetch_error", []


def _check_corp(corp: dict, year: int) -> tuple[str, list[dict]]:
    current = load(corp, year)
    if current is None:
        return "no_filing", []
    findings = run_single(current)
    previous = load(corp, year - 1)
    if previous is not None and previous.fs_div == current.fs_div:
        findings.extend(restatement(current, previous))
    return "checked", [f.to_dict() for f in findings]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=2025)
    ap.add_argument("--sample", type=int, default=300)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    corps = client.listed_corps()
    rng = random.Random(args.seed)
    sample = rng.sample(corps, min(args.sample, len(corps)))

    with ThreadPoolExecutor(args.workers) as pool:
        results = list(pool.map(lambda c: check_corp(c, args.year), sample))

    RESULTS.mkdir(exist_ok=True)
    tag = f"{args.year}_n{args.sample}_s{args.seed}"
    findings = [f for _, fs in results for f in fs]
    with (RESULTS / f"findings_{tag}.jsonl").open("w") as fh:
        for f in findings:
            fh.write(json.dumps(f, ensure_ascii=False) + "\n")

    by_rule: dict[str, Counter] = {}
    for f in findings:
        by_rule.setdefault(f["rule"], Counter())[f["status"]] += 1
    summary = {
        "year": args.year,
        "sampled": len(sample),
        "listed_total": len(corps),
        "checked": sum(1 for s, _ in results if s == "checked"),
        "no_filing": sum(1 for s, _ in results if s == "no_filing"),
        "fetch_error": sum(1 for s, _ in results if s == "fetch_error"),
        "by_rule": {r: dict(c) for r, c in sorted(by_rule.items())},
        "corps_with_mismatch": len({f["corp_code"] for f in findings if f["status"] == "mismatch"}),
    }
    (RESULTS / f"summary_{tag}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
