"""평가 케이스(derived 티어)를 규칙 엔진·triage 출력에서 결정적으로 만든다. LLM은 쓰지 않는다.

    python3 -m dart_recon.casegen --seed 7 --out eval/cases.jsonl

입력: results/findings_2025_n600_s7.jsonl(후보 회사 목록)과 캐시를 읽는 MCP 도구 핸들러(dart_check, dart_explain; DART_OFFLINE=1).
라벨은 도구의 현재 출력에서 도출한다(사람이 공시 원문으로 확인한 것이 아님).

  none                   dart_check의 모든 규칙이 ok(전년도 공시도 있어 재작성 검사까지 수행)
  rounding_or_typo       triage 범주가 rounding_typo뿐
  discontinued_reclass   restatement 상태가 reclassified뿐
  restatement_disclosed  triage 범주가 restatement뿐. API 수치만으로 공시 여부를 확정할 수 없어
                         accepted_causes = [restatement_disclosed, unexplained]
  unexplained            triage 범주가 unclassified뿐

범주가 섞였거나(예: restatement + unclassified), 누락(missing) 규칙이 있거나, 라벨이 없는 범주(held_for_sale_cash,
column_shift, restatement_discontinued)인 회사는 제외한다. 기존 `--out` 파일의 사람 확인 케이스는 tier=verified로
그대로 보존하고, derived는 매번 새로 만든다(같은 seed·같은 캐시면 같은 파일).
"""

from __future__ import annotations

import argparse
import json
import os
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LABEL_ORDER = ("none", "rounding_or_typo", "discontinued_reclass", "restatement_disclosed", "unexplained")
CAPS = {"none": 8, "rounding_or_typo": 7, "discontinued_reclass": 6, "restatement_disclosed": 8, "unexplained": 8}
CATEGORY_LABEL = {"rounding_typo": "rounding_or_typo", "restatement": "restatement_disclosed", "unclassified": "unexplained"}
MIN_FACT_DIFF = 1000  # 이보다 작은 차이는 다른 숫자와 구별되지 않아 필수 사실로 쓰지 않는다
SPLIT_FRACTIONS = (0.4, 0.3)  # train, heldout; 나머지는 test

# 정답을 유추할 수 없는 일반 질문. 범주와 무관하게 corp_code로만 고른다.
QUESTIONS = (
    "{corp} {year} 사업보고서 재무제표에 맞지 않는 숫자가 있나요? 있으면 이유도 알려 주세요.",
    "{corp} {year} 사업보고서의 재무제표 숫자는 서로 잘 맞나요? 이상한 점이 있으면 원인과 함께 알려 주세요.",
    "{corp} {year} 사업보고서에서 현금흐름표·재무상태표·전기 숫자가 어긋나는 곳이 있는지 확인하고, 있다면 왜 그런지 설명해 주세요.",
)
FORBIDDEN_COMMON = ["(분식|횡령|배임|사기|조작|부정\\s*회계)", "(매수|매도|투자)\\s*(를\\s*)?(추천|권유|하세요|하시길)|목표\\s*주가"]
# 기존 clean 케이스의 단정 패턴에서 "발견/존재"를 단정형으로 좁혔다: "불일치는 발견되지 않았습니다" 같은 정답 문장에 걸리지 않게.
FORBIDDEN_NONE = "(불일치|오류|문제)(가|는|를)?\\s*(있습니다|발견(됩니다|되었습니다|했습니다)|존재합니다|확인됩니다)"


def question_for(corp_name: str, corp_code: str, year: int) -> str:
    return QUESTIONS[int(corp_code) % len(QUESTIONS)].format(corp=corp_name, year=year)


def default_call(name: str, args: dict) -> dict:
    from . import mcp_server

    os.environ["DART_OFFLINE"] = "1"
    resp = mcp_server.call_tool({"name": name, "arguments": args})
    if resp.get("isError"):
        raise RuntimeError(resp["content"][0]["text"])
    return json.loads(resp["content"][0]["text"])


def label_corp(call, corp: dict, year: int) -> dict | None:
    """회사 하나의 라벨과 필수 사실. 라벨을 정할 수 없으면 None."""
    args = {"corp": corp["corp_code"], "year": year}
    try:
        check = call("dart_check", args)
        by_name = call("dart_check", {"corp": corp["corp_name"], "year": year})  # 질문의 회사명으로 같은 회사가 조회돼야 한다
    except Exception:
        return None
    if by_name.get("corp_code") != corp["corp_code"] or by_name.get("rcept_no") != check.get("rcept_no"):
        return None
    summary = check["summary"]
    if set(summary) == {"ok"}:
        if not check.get("restatement_checked"):
            return None
        facts, label = [check["rcept_no"], "KDQ-0006"], "none"
        text = json.dumps(check, ensure_ascii=False)
        explain_text = ""
    else:
        if "missing" in summary:
            return None
        try:
            explain = call("dart_explain", args)
        except Exception:
            return None
        markers = set()
        for item in explain["items"]:
            f = item["finding"]
            if f["status"] == "reclassified":
                markers.add("discontinued_reclass")
            elif item.get("cause_candidate"):
                markers.add(CATEGORY_LABEL.get(item["cause_candidate"]["category"], "excluded"))
            else:
                markers.add("excluded")
        if len(markers) != 1 or next(iter(markers)) == "excluded":
            return None
        label = next(iter(markers))
        items = explain["items"]
        diffs = []
        for item in items:
            d = str(abs(item["finding"]["diff"]))
            if abs(item["finding"]["diff"]) >= MIN_FACT_DIFF and d not in diffs:
                diffs.append(d)
        facts = [items[0]["finding"]["rule"]] + diffs[:2]
        text = json.dumps(check, ensure_ascii=False)
        explain_text = json.dumps(explain, ensure_ascii=False)
    if any(f not in text + explain_text for f in facts):
        return None
    return {"label": label, "facts": facts}


def split_counts(n: int) -> tuple[int, int, int]:
    n_train = int(n * SPLIT_FRACTIONS[0] + 0.5)
    n_held = int(n * SPLIT_FRACTIONS[1] + 0.5)
    return n_train, n_held, n - n_train - n_held


def build_case(corp: dict, info: dict, split: str, year: int) -> dict:
    label = info["label"]
    forbidden = list(FORBIDDEN_COMMON) + ([FORBIDDEN_NONE] if label == "none" else [])
    case = {"id": f"d-{corp['corp_code']}", "question": question_for(corp["corp_name"], corp["corp_code"], year),
            "corp": corp["corp_name"], "year": year,
            "expected_cause": "restatement_disclosed" if label == "restatement_disclosed" else label,
            "expected_basis": "api_only", "required_facts": info["facts"], "forbidden": forbidden,
            "split": split, "protected": False, "tier": "derived"}
    if label == "restatement_disclosed":
        case["accepted_causes"] = ["restatement_disclosed", "unexplained"]
    return case


def generate(seed: int, corps: list[dict], call, verified: list[dict], year: int = 2025, caps: dict | None = None) -> list[dict]:
    """verified 케이스는 그대로(tier만 부여) 앞에 두고, 이어서 derived. corps: [{corp_code, corp_name}]."""
    caps = caps or CAPS
    skip = {v["corp"] for v in verified}
    uniq = {}
    for c in corps:
        uniq[c["corp_code"]] = {"corp_code": c["corp_code"], "corp_name": c["corp_name"]}
    names = [c["corp_name"] for c in uniq.values()]
    pool: dict[str, list[tuple[dict, dict]]] = {label: [] for label in LABEL_ORDER}
    for code in sorted(uniq):
        corp = uniq[code]
        if corp["corp_name"] in skip or names.count(corp["corp_name"]) > 1:
            continue
        info = label_corp(call, corp, year)
        if info is not None:
            pool[info["label"]].append((corp, info))
    out = [{**v, "tier": "verified"} if "tier" not in v else dict(v) for v in verified]
    for label in LABEL_ORDER:
        picks = list(pool[label])
        random.Random(f"{seed}:{label}").shuffle(picks)
        picks = picks[:caps.get(label, 0)]
        n_train, n_held, _ = split_counts(len(picks))
        for i, (corp, info) in enumerate(picks):
            split = "train" if i < n_train else "heldout" if i < n_train + n_held else "test"
            out.append(build_case(corp, info, split, year))
    return out


def read_verified(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    return [r for r in rows if r.get("tier") != "derived"]


def read_corps(path: Path) -> list[dict]:
    seen, out = set(), []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r["corp_code"] not in seen:
            seen.add(r["corp_code"])
            out.append({"corp_code": r["corp_code"], "corp_name": r["corp_name"]})
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="dart_recon.casegen", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--year", type=int, default=2025)
    ap.add_argument("--findings", default=str(ROOT / "results" / "findings_2025_n600_s7.jsonl"))
    a = ap.parse_args(argv)
    out = Path(a.out)
    cases = generate(a.seed, read_corps(Path(a.findings)), default_call, read_verified(out), a.year)
    out.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in cases), encoding="utf-8")
    derived = [c for c in cases if c["tier"] == "derived"]
    print(f"verified={len(cases) - len(derived)} derived={len(derived)} -> {out}")


if __name__ == "__main__":
    main()
