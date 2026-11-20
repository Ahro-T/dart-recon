"""평가 하네스 + 게이트형 자기개선 루프의 결정론적 부분. LLM은 호출하지 않는다(표준 라이브러리만).

분석가/판정자/개선자 역할은 오케스트레이터가 Claude Code 서브에이전트로 실행하고, 이 모듈은 프롬프트 생성,
도구 호출 기록, 점수 계산, 스키마 검증, 게이트 판정, 원장 기록만 한다. 절차는 eval/PROTOCOL.md.

    python3 -m dart_recon.evalkit prompt --case ID --skill v0 --round 0 --rep 1
    python3 -m dart_recon.evalkit tool --round 0 --skill v0 --rep 1 --case ID dart_check '{"corp":"효성화학","year":2025}'
    python3 -m dart_recon.evalkit score --round 0 --skill v0 [--splits train,heldout]   # k* 디렉터리 전부 채점
    python3 -m dart_recon.evalkit noise --round 0 --skill v0
    python3 -m dart_recon.evalkit judge-prompt --round 0 --skill v0 [--rep 1]
    python3 -m dart_recon.evalkit judge-validate --round 0 --skill v0 [--rep 1]
    python3 -m dart_recon.evalkit improve-prompt --round 0 --skill v0
    python3 -m dart_recon.evalkit gate --round 0 --base v0 --cand v1
    python3 -m dart_recon.evalkit report
    python3 -m dart_recon.evalkit check-cases      # 케이스의 required_facts가 실제 도구 출력에 있는지(캐시 필요)
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 테스트에서 임시 디렉터리로 교체

TAXONOMY = {
    "none": "정합 — 검사한 규칙에서 불일치가 없다(오탐 함정 포함).",
    "restatement_disclosed": "회사가 주석으로 공시한 재작성(전기오류수정 등)으로 전기 숫자가 달라짐.",
    "discontinued_reclass": "중단영업 재분류로 전기 매출 등이 계속영업에서 빠짐.",
    "held_for_sale_cash": "현금흐름표 기말현금에 매각예정자산집단의 현금이 포함되어 재무상태표 현금과 차이.",
    "mistagged_account": "회사가 계정에 표준코드를 잘못 붙여 생긴 겉보기 불일치(숫자는 정합).",
    "column_shift_small": "열 밀림 등 표 구조 때문에 한 칸이 어긋남.",
    "rounding_or_typo": "반올림 또는 한두 칸 오타 수준의 소액 차이.",
    "unexplained": "불일치가 있으나 공시·도구 출력만으로 원인을 확인할 수 없음.",
}
BASES = ("original_checked", "api_only", "unknown")
SPLITS = ("train", "heldout", "test")
DEFAULT_SPLITS = ("train", "heldout")  # test는 루프 밖에서 마지막에 한 번만
TIERS = ("verified", "derived")
FAILURE_TYPES = ("wrong_cause", "missing_fact", "ungrounded_number", "wrong_basis", "overclaim", "false_alarm", "other")
TOOL_NAMES = ("dart_company", "dart_check", "dart_explain", "dart_history", "dart_rules")
_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


# ---- 경로 ----------------------------------------------------------------------------


def cases_path() -> Path:
    return ROOT / "eval" / "cases.jsonl"


def run_dir(rnd: int, skill: str) -> Path:
    return ROOT / "results" / "loop" / "runs" / f"r{rnd}" / skill


def rep_dir(rnd: int, skill: str, rep: int) -> Path:
    if rep < 1:
        raise SystemExit(f"--rep은 1 이상: {rep}")
    return run_dir(rnd, skill) / f"k{rep}"


def rep_dirs(rnd: int, skill: str) -> list[tuple[int, Path]]:
    d = run_dir(rnd, skill)
    found = [(int(p.name[1:]), p) for p in d.glob("k*") if p.is_dir() and re.fullmatch(r"k\d+", p.name)]
    return sorted(found)


def ledger_path() -> Path:
    return ROOT / "results" / "loop" / "ledger.jsonl"


def skill_path(version: str) -> Path:
    """채택된 스킬 SKILL.<v>.md, 없으면 후보 candidates/<v>.md."""
    base = ROOT / "skills" / "dart-recon"
    for p in (base / f"SKILL.{version}.md", base / "candidates" / f"{version}.md"):
        if p.exists():
            return p
    raise SystemExit(f"스킬 파일 없음: {version}")


def _check_id(value: str, what: str) -> str:
    if not _ID.match(value):
        raise SystemExit(f"잘못된 {what}: {value!r}")
    return value


def load_cases() -> list[dict]:
    out = []
    for line in cases_path().read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def case_by_id(cid: str) -> dict:
    for c in load_cases():
        if c["id"] == cid:
            return c
    raise SystemExit(f"없는 케이스: {cid}")


# ---- 금액 추출·정규화 ----------------------------------------------------------------

UNITS = {"조": 10**12, "억": 10**8, "천만": 10**7, "백만": 10**6, "만": 10**4, "천": 10**3}
_TOKEN = re.compile(r"(?P<run>(?:\d+(?:\.\d+)?\s*(?:조|억|천만|백만|만|천)\s*)+)|(?P<plain>\d+(?:\.\d+)?)")
_PAIR = re.compile(r"(\d+(?:\.\d+)?)\s*(조|억|천만|백만|만|천)")


def strip_commas(text: str) -> str:
    """숫자 사이 천 단위 쉼표 제거."""
    return re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", text)


def extract_amounts(text: str) -> list[dict]:
    """답변에서 금액 후보를 뽑는다: {value(원), tol(허용 오차), text}.
    4자리 이상 정수와 조/억/천만/백만/만/천 단위 표기를 본다. 규칙 ID·날짜·연도·소수·퍼센트는 제외."""
    t = re.sub(r"KDQ-\d+", " ", text)
    t = re.sub(r"\d{4}-\d{1,2}-\d{1,2}", " ", t)
    t = strip_commas(t)
    out = []
    for m in _TOKEN.finditer(t):
        if m.group("run"):
            pairs = _PAIR.findall(m.group("run"))
            if len(pairs) == 1 and pairs[0][1] == "만" and re.fullmatch(r"\d{5,}", pairs[0][0]):
                # '75000000만 있고'의 '만'은 단위가 아니라 조사('~만'). 다섯 자리 이상 원 단위 정수에 만을 붙여 쓰지 않는다.
                out.append({"value": int(pairs[0][0]), "tol": 0, "text": pairs[0][0]})
                continue
            value = sum(float(n) * UNITS[u] for n, u in pairs)
            n, u = pairs[-1]
            decimals = len(n.split(".")[1]) if "." in n else 0
            out.append({"value": value, "tol": 0.5 * 10 ** (-decimals) * UNITS[u], "text": m.group("run").strip()})
            continue
        s = m.group("plain")
        if "." in s or len(s) < 4:
            continue
        if t[m.end():m.end() + 1] == "%":
            continue
        v = int(s)
        if 1900 <= v <= 2100 and not s.startswith("0"):
            continue  # 연도
        out.append({"value": v, "tol": 0, "text": s})
    return out


def _signed(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and re.fullmatch(r"-?\d+", v.strip()):
        return int(v)
    return None


def grounded_values(tool_records: list[dict]) -> list[int]:
    """도구 결과의 모든 정수(절댓값, 문자열 속 4자리 이상 숫자 포함)와, 같은 객체 안 1000 이상 정수끼리의 차이. 정렬된 리스트."""
    vals: set[int] = set()

    def walk(node):
        if isinstance(node, dict):
            nums = [n for n in (_signed(v) for v in node.values()) if n is not None]
            vals.update(abs(n) for n in nums)
            big = [n for n in nums if abs(n) >= 1000]
            for i in range(len(big)):
                for j in range(i + 1, len(big)):
                    vals.add(abs(big[i] - big[j]))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, str):  # detail·가설 설명 같은 문장 속 숫자와 단위 표기('1만 원 이하')
            vals.update(int(x) for x in re.findall(r"\d{4,}", strip_commas(node)))
            vals.update(int(a["value"]) for a in extract_amounts(node) if float(a["value"]).is_integer())

    for rec in tool_records:
        walk(rec.get("result"))
    return sorted(vals)


def is_grounded(amount: dict, grounded: list[int]) -> bool:
    lo = amount["value"] - amount["tol"]
    hi = amount["value"] + amount["tol"]
    i = bisect.bisect_left(grounded, lo - 1e-6)
    return i < len(grounded) and grounded[i] <= hi + 1e-6


def hallucinated_amounts(answer: str, tool_records: list[dict]) -> list[str]:
    grounded = grounded_values(tool_records)
    return [a["text"] for a in extract_amounts(answer) if not is_grounded(a, grounded)]


def facts_recall(answer: str, facts: list[str]) -> tuple[float, list[str]]:
    """필수 사실 재현율. 숫자 사실은 독립된 수 또는 단위 표기(반올림 허용)로, 그 밖은 공백 무시 부분 문자열로 찾는다."""
    if not facts:
        return 1.0, []
    norm = strip_commas(answer)
    squashed = re.sub(r"\s+", "", norm).lower()
    amounts = extract_amounts(answer)
    missing = []
    for f in facts:
        nf = re.sub(r"\s+", "", strip_commas(f)).lower()
        if nf.isdigit():
            hit = re.search(rf"(?<!\d){nf}(?!\d)", norm) is not None or any(
                abs(a["value"] - int(nf)) <= a["tol"] and a["tol"] > 0 for a in amounts)
        else:
            hit = nf in squashed
        if not hit:
            missing.append(f)
    return (len(facts) - len(missing)) / len(facts), missing


# ---- 도구 호출 기록 ------------------------------------------------------------------


def run_tool(rnd: int, skill: str, case_id: str, name: str, args: dict, rep: int) -> str:
    """MCP 도구 핸들러를 직접 호출하고 결과를 tools.jsonl에 덧붙인다. 출력 문자열을 돌려준다."""
    from . import mcp_server

    _check_id(skill, "skill")
    _check_id(case_id, "case")
    if name not in TOOL_NAMES:
        raise SystemExit(f"없는 도구: {name} (가능: {', '.join(TOOL_NAMES)})")
    os.environ["DART_OFFLINE"] = "1"
    try:
        resp = mcp_server.call_tool({"name": name, "arguments": args})
    except mcp_server.RpcError as exc:
        resp = {"content": [{"type": "text", "text": f"RpcError: {exc.message}"}], "isError": True}
    text = resp["content"][0]["text"]
    is_error = bool(resp.get("isError"))
    result = text if is_error else json.loads(text)
    d = rep_dir(rnd, skill, rep)
    d.mkdir(parents=True, exist_ok=True)
    with (d / f"{case_id}.tools.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"tool": name, "args": args, "result": result, "is_error": is_error}, ensure_ascii=False) + "\n")
    return text


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


# ---- 채점 ----------------------------------------------------------------------------


def accepted_causes(case: dict) -> list[str]:
    return list(case.get("accepted_causes") or [case["expected_cause"]])


def score_case(case: dict, answer: dict | None, tool_records: list[dict]) -> dict:
    base = {"split": case["split"], "protected": bool(case.get("protected"))}
    if answer is None or not isinstance(answer.get("answer"), str):
        return {**base, "missing_answer": True, "cause_match": 0, "facts_recall": 0.0, "missing_facts": list(case["required_facts"]),
                "basis_ok": 0, "hallucinated_amounts": [], "forbidden_hits": [], "score": 0.0}
    text = answer["answer"]
    cause_match = int(answer.get("cause") in accepted_causes(case))
    recall, missing = facts_recall(text, case["required_facts"])
    basis_ok = int(answer.get("basis") == case["expected_basis"] and answer.get("basis") != "unknown")
    halluc = hallucinated_amounts(text, tool_records)
    forbidden = [p for p in case.get("forbidden", []) if re.search(p, text)]
    score = (cause_match + recall + basis_ok) / 3
    if halluc or forbidden:
        score = 0.0
    return {**base, "missing_answer": False, "cause_match": cause_match, "facts_recall": round(recall, 4), "missing_facts": missing,
            "basis_ok": basis_ok, "hallucinated_amounts": halluc, "forbidden_hits": forbidden, "score": round(score, 4)}


def _mean(xs: list[float]) -> float:
    return round(sum(xs) / len(xs), 4) if xs else 0.0


def _sd(xs: list[float]) -> float | None:
    """표본표준편차(n-1). 값이 2개 미만이면 None."""
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def rep_scores(c: dict) -> list[float]:
    """케이스의 rep별 점수(rep 번호 순). 레거시 scores.json(reps 없음)은 k=1로 본다."""
    if "reps" not in c:
        return [c["score"]]
    return [c["reps"][k]["score"] for k in sorted(c["reps"], key=int)]


def aggregate(cases: dict[str, dict]) -> dict:
    """rep 평균 점수 기준 집계. se = sqrt(sum(var_c / k_c)) / n (모든 케이스 k>=2일 때만, 아니면 None)."""
    out = {}
    picks = [("train", lambda c: c["split"] == "train"), ("heldout", lambda c: c["split"] == "heldout")]
    if any(c["split"] == "test" for c in cases.values()):
        picks.append(("test", lambda c: c["split"] == "test"))
    picks.append(("all", lambda c: True))
    for name, pick in picks:
        rows = [c for c in cases.values() if pick(c)]
        per = [rep_scores(c) for c in rows]
        se = None
        if rows and all(len(x) >= 2 for x in per):
            se = round(math.sqrt(sum(_sd(x) ** 2 / len(x) for x in per)) / len(rows), 4)
        out[name] = {
            "n": len(rows),
            "mean_score": _mean([c["score"] for c in rows]),
            "cause_match": _mean([c["cause_match"] for c in rows]),
            "facts_recall": _mean([c["facts_recall"] for c in rows]),
            "basis_ok": _mean([c["basis_ok"] for c in rows]),
            "hallucinated_amounts": sum(len(c["hallucinated_amounts"]) for c in rows),
            "forbidden_hits": sum(len(c["forbidden_hits"]) for c in rows),
            "se": se,
        }
    return out


def _read_answer(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def combine_reps(reps: dict[int, dict]) -> dict:
    """rep별 score_case 결과를 케이스 하나로 합친다. 점수·비율은 rep 평균, 환각·금지어는 전 rep 합."""
    first = reps[min(reps)]
    scores = [reps[j]["score"] for j in sorted(reps)]
    sd = _sd(scores)
    return {"split": first["split"], "protected": first["protected"], "k": len(reps),
            "reps": {str(j): reps[j] for j in sorted(reps)},
            "score": _mean(scores), "sd": None if sd is None else round(sd, 4),
            "missing_answer": all(r["missing_answer"] for r in reps.values()),
            "cause_match": _mean([r["cause_match"] for r in reps.values()]),
            "facts_recall": _mean([r["facts_recall"] for r in reps.values()]),
            "basis_ok": _mean([r["basis_ok"] for r in reps.values()]),
            "hallucinated_amounts": [h for j in sorted(reps) for h in reps[j]["hallucinated_amounts"]],
            "forbidden_hits": [h for j in sorted(reps) for h in reps[j]["forbidden_hits"]]}


def score_run(rnd: int, skill: str, splits: tuple[str, ...] = DEFAULT_SPLITS) -> dict:
    """k* 디렉터리를 모두 채점한다. 요청한 분할만 다시 채점하고, 이미 채점된 다른 분할은 scores.json에서 보존한다."""
    bad = [s for s in splits if s not in SPLITS]
    if bad or not splits:
        raise SystemExit(f"--splits는 {','.join(SPLITS)} 중에서: {bad or '(empty)'}")
    d = run_dir(rnd, skill)
    reps = rep_dirs(rnd, skill)
    if not reps:
        raise SystemExit(f"k* 디렉터리 없음: {d} (레거시 run은 재채점하지 않는다)")
    scores = d / "scores.json"
    prev = json.loads(scores.read_text(encoding="utf-8")) if scores.exists() else {}
    kept = {cid: c for cid, c in prev.get("cases", {}).items() if c["split"] not in splits and "reps" in c}
    per = dict(kept)
    for case in load_cases():
        if case["split"] not in splits:
            continue
        per[case["id"]] = combine_reps({
            j: score_case(case, _read_answer(p / f"{case['id']}.answer.json"), read_jsonl(p / f"{case['id']}.tools.jsonl")) for j, p in reps})
    order = {c["id"]: i for i, c in enumerate(load_cases())}
    per = {cid: per[cid] for cid in sorted(per, key=lambda x: order.get(x, len(order)))}
    result = {"round": rnd, "skill": skill, "k": len(reps), "splits": [s for s in SPLITS if s in splits or any(c["split"] == s for c in kept.values())],
              "cases": per, "aggregates": aggregate(per)}
    if "agreement" in prev:  # 재채점 시 이전 judge 일치도 기록은 유지
        result["agreement"] = prev["agreement"]
    scores.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def load_scores(rnd: int, skill: str) -> dict:
    p = run_dir(rnd, skill) / "scores.json"
    if not p.exists():
        raise SystemExit(f"scores.json 없음: {p} (먼저 score 실행)")
    return json.loads(p.read_text(encoding="utf-8"))


def load_rep_scores(rnd: int, skill: str) -> dict:
    """반복 실행 형식(reps 포함)의 scores.json. 레거시 형식이면 거부한다."""
    s = load_scores(rnd, skill)
    if any("reps" not in c for c in s["cases"].values()):
        raise SystemExit(f"레거시 scores.json(k 없음): r{rnd}/{skill}")
    return s


# ---- 스키마 검증(JSON Schema 부분집합) -----------------------------------------------


def validate(node, schema: dict, path: str = "$") -> list[str]:
    """type, enum, required, properties, additionalProperties:false, items, minimum, maximum, minLength, minItems만 지원."""
    errs = []
    t = schema.get("type")
    checks = {"object": lambda v: isinstance(v, dict), "array": lambda v: isinstance(v, list), "string": lambda v: isinstance(v, str),
              "integer": lambda v: isinstance(v, int) and not isinstance(v, bool)}
    if t and not checks[t](node):
        return [f"{path}: {t} 이어야 함"]
    if "enum" in schema and node not in schema["enum"]:
        errs.append(f"{path}: {schema['enum']} 중 하나여야 함 (값 {node!r})")
    if t == "integer":
        if "minimum" in schema and node < schema["minimum"]:
            errs.append(f"{path}: {schema['minimum']} 이상이어야 함")
        if "maximum" in schema and node > schema["maximum"]:
            errs.append(f"{path}: {schema['maximum']} 이하여야 함")
    if t == "string" and len(node) < schema.get("minLength", 0):
        errs.append(f"{path}: 너무 짧음")
    if t == "object":
        props = schema.get("properties", {})
        for k in schema.get("required", []):
            if k not in node:
                errs.append(f"{path}: 필수 키 없음 {k}")
        for k, v in node.items():
            if k in props:
                errs += validate(v, props[k], f"{path}.{k}")
            elif schema.get("additionalProperties") is False:
                errs.append(f"{path}: 허용되지 않는 키 {k}")
    if t == "array":
        if len(node) < schema.get("minItems", 0):
            errs.append(f"{path}: 항목이 {schema['minItems']}개 이상이어야 함")
        for i, v in enumerate(node):
            if "items" in schema:
                errs += validate(v, schema["items"], f"{path}[{i}]")
    return errs


def judge_schema() -> dict:
    return json.loads((ROOT / "eval" / "judge_schema.json").read_text(encoding="utf-8"))


def judge_validate(rnd: int, skill: str, rep: int = 1) -> dict:
    d = rep_dir(rnd, skill, rep)
    jp = d / "judge.json"
    if not jp.exists():
        raise SystemExit(f"judge.json 없음: {jp}")
    try:
        judge = json.loads(jp.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"valid": False, "errors": [f"JSON 파싱 실패: {exc}"]}
    errors = validate(judge, judge_schema())
    if errors:
        return {"valid": False, "errors": errors}
    scores = load_rep_scores(rnd, skill)
    known = scores["cases"]
    if any(str(rep) not in c["reps"] for c in known.values()):
        raise SystemExit(f"scores.json에 rep {rep} 없음 (score를 다시 실행)")
    seen = [c["case_id"] for c in judge["cases"]]
    for cid in seen:
        if cid not in known:
            errors.append(f"모르는 case_id: {cid}")
    if len(seen) != len(set(seen)):
        errors.append("case_id 중복")
    for cid in known:
        if cid not in seen:
            errors.append(f"judge에 빠진 케이스: {cid}")
    if errors:
        return {"valid": False, "errors": errors}
    rows = []
    for jc in judge["cases"]:
        det = known[jc["case_id"]]["reps"][str(rep)]
        if det["missing_answer"]:
            continue
        j_halluc = any(c["label"] == "hallucinated" for c in jc["claims"])
        d_halluc = bool(det["hallucinated_amounts"])
        j_correct = jc["rubric"]["correctness"] >= 4
        rows.append({"case_id": jc["case_id"], "judge_hallucinated": j_halluc, "det_hallucinated": d_halluc,
                     "hallucination_agree": j_halluc == d_halluc, "judge_correct": j_correct,
                     "det_cause_match": bool(det["cause_match"]), "correctness_agree": j_correct == bool(det["cause_match"])})
    n = len(rows)
    agreement = {"rep": rep, "n": n, "hallucination_agree": sum(r["hallucination_agree"] for r in rows),
                 "correctness_agree": sum(r["correctness_agree"] for r in rows), "cases": rows}
    scores["agreement"] = agreement
    (run_dir(rnd, skill) / "scores.json").write_text(json.dumps(scores, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (d / "agreement.json").write_text(json.dumps(agreement, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"valid": True, "errors": [], "agreement": {k: agreement[k] for k in ("n", "hallucination_agree", "correctness_agree")}}


# ---- 프롬프트 ------------------------------------------------------------------------


def _taxonomy_text() -> str:
    return "\n".join(f"- `{k}`: {v}" for k, v in TAXONOMY.items())


def analyst_prompt(case_id: str, skill: str, rnd: int, rep: int) -> str:
    case = case_by_id(case_id)
    skill_text = skill_path(skill).read_text(encoding="utf-8")
    d = f"results/loop/runs/r{rnd}/{skill}/{rep_dir(rnd, skill, rep).name}"
    return f"""당신은 dart-recon 분석가입니다. 아래 [스킬]의 지침을 따라 [질문]에 답하세요.

[스킬 {skill}]
{skill_text}
[/스킬]

[질문]
{case['question']}
[/질문]

## 도구 사용 (반드시 이 방식만)
도구는 다음 명령으로만 호출합니다. 호출 결과는 자동으로 기록되며 채점의 근거가 됩니다.
```
python3 -m dart_recon.evalkit tool --round {rnd} --skill {skill} --rep {rep} --case {case_id} <도구이름> '<JSON 인자>'
```
예: `python3 -m dart_recon.evalkit tool --round {rnd} --skill {skill} --rep {rep} --case {case_id} dart_check '{{"corp":"회사명","year":2025}}'`
도구 이름: {', '.join(TOOL_NAMES)}. 다른 파일(eval/, results/, data/)은 읽지 마세요.
답변의 모든 숫자는 도구 출력에 나온 값이어야 합니다. 도구에 없는 숫자를 만들지 마세요.

## 원인 분류 (cause)
{_taxonomy_text()}

## 근거 수준 (basis)
- `original_checked`: 도구 출력이 원문 공시 확인을 명시한 경우에만.
- `api_only`: OpenDART API 수치(도구 출력)만 본 경우.
- `unknown`: 판단할 수 없을 때.

## 출력
다음 JSON 파일을 작성하세요: `{d}/{case_id}.answer.json`
```
{{"case_id": "{case_id}", "answer": "<한국어 설명>", "cause": "<원인 분류 라벨>", "basis": "original_checked|api_only|unknown"}}
```
파일을 쓴 뒤 작업을 마칩니다.
"""


def judge_prompt(rnd: int, skill: str, rep: int = 1) -> str:
    """판정 대상은 scores.json에 채점된 케이스(score 먼저 실행)이고, 비용 때문에 한 rep만 본다."""
    d = f"results/loop/runs/r{rnd}/{skill}/{rep_dir(rnd, skill, rep).name}"
    scored = load_rep_scores(rnd, skill)["cases"]
    blocks = []
    for c in (c for c in load_cases() if c["id"] in scored):
        ans = rep_dir(rnd, skill, rep) / f"{c['id']}.answer.json"
        ans_text = ans.read_text(encoding="utf-8").strip() if ans.exists() else "(답변 파일 없음)"
        blocks.append(f"""### {c['id']}
- 질문: {c['question']}
- 기대 원인: {c['expected_cause']}{(' (허용: ' + ', '.join(accepted_causes(c)) + ')') if c.get('accepted_causes') else ''} / 기대 근거 수준: {c['expected_basis']}
- 필수 사실: {json.dumps(c['required_facts'], ensure_ascii=False)}
- 답변 파일 ({d}/{c['id']}.answer.json):
{ans_text}
- 도구 로그(근거): {d}/{c['id']}.tools.jsonl""")
    return f"""당신은 dart-recon 평가의 판정자입니다. 읽기 전용으로 채점하고, 파일은 `{d}/judge.json` 하나만 씁니다.

각 케이스의 답변을 도구 로그와 대조하세요. 도구 로그(tools.jsonl)는 직접 읽어서 확인합니다.
- claims: 답변의 사실 주장을 하나씩 뽑아 {{text, label, evidence_ref}}. label은 `grounded`(도구 로그에서 확인됨), `unverified`(확인 불가), `hallucinated`(도구 로그와 모순되거나 없는 숫자). evidence_ref는 근거가 되는 tools.jsonl 줄 번호(예: "line 2") 또는 "none".
- rubric: correctness, grounding, calibration 각 1~5 정수. correctness=결론(원인)이 기대와 맞는가, grounding=주장이 도구 출력에 근거하는가, calibration=근거 수준(원문 확인 vs API 수치)과 확신 정도가 적절한가.
- failure_types: 해당하는 것만 {json.dumps(list(FAILURE_TYPES))} 중에서. 문제 없으면 빈 배열.
- diagnosis: 한두 문장.

출력 형식은 eval/judge_schema.json을 따릅니다:
{{"round": {rnd}, "skill": "{skill}", "cases": [{{"case_id": "...", "claims": [{{"text": "...", "label": "grounded", "evidence_ref": "line 1"}}], "rubric": {{"correctness": 1, "grounding": 1, "calibration": 1}}, "failure_types": [], "diagnosis": "..."}}]}}
아래 모든 케이스를 빠짐없이 포함하세요.

## 케이스
{chr(10).join(blocks)}
"""


def next_version(skill: str) -> str:
    """아직 쓰이지 않은 다음 버전. 기각된 후보 번호도 재사용하지 않는다."""
    m = re.fullmatch(r"v(\d+)", skill)
    if not m:
        raise SystemExit(f"버전 형식은 vN: {skill}")
    base = ROOT / "skills" / "dart-recon"
    used = [int(m.group(1))]
    for p in list(base.glob("SKILL.v*.md")) + list((base / "candidates").glob("v*.md")):
        n = re.fullmatch(r"(?:SKILL\.)?v(\d+)\.md", p.name)
        if n:
            used.append(int(n.group(1)))
    return f"v{max(used) + 1}"


def improve_prompt(rnd: int, skill: str) -> str:
    """TRAIN 분할의 실패(rep 평균 < 1)만 보여 준다. 홀드아웃·test 케이스는 id·질문·점수 모두 노출하지 않는다."""
    scores = load_rep_scores(rnd, skill)
    d = rep_dir(rnd, skill, 1)  # 판정자는 rep 1만 본다
    judge = {}
    if (d / "judge.json").exists():
        try:
            judge = {c["case_id"]: c for c in json.loads((d / "judge.json").read_text(encoding="utf-8")).get("cases", [])}
        except (json.JSONDecodeError, KeyError, TypeError):
            judge = {}
    cases = {c["id"]: c for c in load_cases() if c["split"] == "train"}
    fails, protected = [], []
    for cid, c in cases.items():
        s = scores["cases"].get(cid)
        if s is None:
            continue
        if c.get("protected"):
            protected.append(f"- {cid}: 현재 점수 {s['score']} (이 케이스의 점수를 낮추면 후보는 기각된다)")
        if s["score"] >= 1.0:
            continue
        worst = min(s["reps"], key=lambda k: (s["reps"][k]["score"], int(k)))  # 가장 낮은 rep(동점이면 앞 번호)
        w = s["reps"][worst]
        ans = rep_dir(rnd, skill, int(worst)) / f"{cid}.answer.json"
        j = judge.get(cid, {})
        accepted = f" (허용: {', '.join(accepted_causes(c))})" if c.get("accepted_causes") else ""
        fails.append(f"""### {cid}
- 질문: {c['question']}
- 평균 점수 {s['score']} (rep별: {', '.join(f"k{k}={s['reps'][k]['score']}" for k in sorted(s['reps'], key=int))})
- 가장 낮은 rep k{worst}: cause_match={w['cause_match']}, facts_recall={w['facts_recall']}, basis_ok={w['basis_ok']}
- 기대 원인: {c['expected_cause']}{accepted} / 기대 근거 수준: {c['expected_basis']}
- 빠진 사실(k{worst}): {json.dumps(w['missing_facts'], ensure_ascii=False)}
- 근거 없는 금액(k{worst}): {json.dumps(w['hallucinated_amounts'], ensure_ascii=False)} / 금지 표현 적중: {json.dumps(w['forbidden_hits'], ensure_ascii=False)}
- 판정(rep 1 기준) 실패 유형: {json.dumps(j.get('failure_types', []), ensure_ascii=False)} / 진단: {j.get('diagnosis', '(없음)')}
- 분석가 답변 파일(k{worst}): {ans.relative_to(ROOT) if ans.exists() else '(없음)'}""")
    lessons_dir = ROOT / "lessons"
    lessons = []
    if lessons_dir.exists():
        for p in sorted(lessons_dir.glob("*.md")):
            if p.name != "README.md":
                lessons.append(f"#### {p.name}\n{p.read_text(encoding='utf-8')}")
    cand = next_version(skill)
    return f"""당신은 dart-recon 스킬 개선자입니다. 현재 스킬 {skill}의 **범위가 좁은 후보 패치 하나**를 만듭니다.

## 규칙
- 아래 TRAIN 실패에 나타난 원인 유형 하나(또는 연관된 둘)만 겨냥합니다. 스킬 전체를 새로 쓰지 마세요.
- 케이스별 정답·회사명·금액을 스킬에 박아 넣지 마세요(일반 규칙만). 숫자 지어내기를 유도하는 문구도 금지.
- 후보는 게이트를 통과해야 채택됩니다: G1 대상 케이스 평균 차이가 잡음(1-SE)보다 큰 상승, G2 보호 케이스가 잡음 띠 밖으로 하락하지 않음, G3 근거 없는 금액 0건(전 rep), G4 보이지 않는 홀드아웃 평균이 잡음 띠 밖으로 하락하지 않음. 같은 스킬도 반복마다 점수가 흔들리므로 잡음 안의 차이는 개선으로 인정되지 않고, 대부분의 후보는 기각됩니다.
- 이 프롬프트에 없는 케이스·정답 파일(eval/cases.jsonl 등)은 읽지 마세요.

## 현재 스킬 {skill}
{skill_path(skill).read_text(encoding='utf-8')}

## TRAIN 실패 (rep 평균 점수 < 1)
{chr(10).join(fails) if fails else '(실패 없음 — 개선 대상이 없으면 후보를 만들지 마세요)'}

## 보호 케이스 (train)
{chr(10).join(protected) if protected else '(없음)'}

## 이전 기각 교훈 (lessons/)
{chr(10).join(lessons) if lessons else '(없음)'}

## 출력 (이 두 파일만 작성)
1. `skills/dart-recon/candidates/{cand}.md` — 후보 스킬 전문(frontmatter 포함).
2. `results/loop/runs/r{rnd}/candidate.json`:
```
{{"target_failures": ["<겨냥한 실패 유형/원인>"], "target_case_ids": ["<TRAIN 케이스 id>"], "change_summary": "<무엇을 왜 바꿨는지 2~3문장>", "scope": "<바꾼 섹션/문단>"}}
```
target_case_ids는 위 TRAIN 실패 케이스 id만 쓸 수 있습니다.
"""


# ---- 게이트 · 원장 -------------------------------------------------------------------

GATE_NAMES = {"G1": "G1_targeted", "G2": "G2_protected", "G3": "G3_hallucination", "G4": "G4_heldout"}


EPS = 1e-9


def _paired(base: dict, cand: dict, ids: list[str]) -> dict:
    """같은 케이스끼리 짝지은 비교. d_c = mean(cand reps) - mean(base reps), se_c = sqrt(var_b/k_b + var_c/k_c).
    집합 S: D = mean(d_c), SE = sqrt(sum se_c^2) / |S|."""
    per, ks = {}, []
    for cid in ids:
        b, c = rep_scores(base["cases"][cid]), rep_scores(cand["cases"][cid])
        if len(b) < 2 or len(c) < 2:
            raise SystemExit(f"반복 횟수 k<2 (base k={len(b)}, cand k={len(c)}, 케이스 {cid}): 분산을 모르므로 게이트를 판정하지 않는다")
        d = round(sum(c) / len(c) - sum(b) / len(b), 10)
        se = math.sqrt(_sd(b) ** 2 / len(b) + _sd(c) ** 2 / len(c))
        per[cid] = (d, se)
        ks += [len(b), len(c)]
    n = len(ids)
    D = sum(d for d, _ in per.values()) / n if n else 0.0
    SE = math.sqrt(sum(se ** 2 for _, se in per.values())) / n if n else 0.0
    return {"per": per, "D": round(D, 4), "SE": round(SE, 4), "_D": D, "_SE": SE, "n": n, "k": min(ks) if ks else 0}


def _stat(p: dict) -> dict:
    return {"D": p["D"], "SE": p["SE"], "n": p["n"], "k": p["k"]}


def gate_decide(base: dict, cand: dict, target_ids: list[str], train_ids: set[str]) -> dict:
    """게이트는 train·heldout 케이스만 본다(test는 루프 밖). 1-SE 규칙은 유의성 검정이 아니라 잡음 띠 밖의 차이만 인정하는 경험 규칙."""
    gated = [cid for cid, c in cand["cases"].items() if c["split"] in DEFAULT_SPLITS]
    gates = {}
    bad = [t for t in target_ids if t not in train_ids]
    if not target_ids or bad:
        gates["G1"] = {"pass": False, "detail": f"target_case_ids가 비었거나 train 밖: {bad or '(empty)'}", "D": None, "SE": None, "n": 0, "k": None}
    else:
        p = _paired(base, cand, target_ids)
        gates["G1"] = {"pass": p["_D"] > p["_SE"] + EPS, "detail": f"target D={p['D']} SE={p['SE']} (n={p['n']}, k={p['k']}); D>SE 필요", **_stat(p)}
    prot = [cid for cid in gated if cand["cases"][cid]["protected"]]
    p = _paired(base, cand, prot)
    drops = []
    for cid, (d, se) in p["per"].items():
        if d < (0.0 if se < EPS else -se) - EPS:
            drops.append((cid, round(d, 4), round(se, 4)))
    gates["G2"] = {"pass": not drops, "detail": f"protected drops beyond noise (cid, d, se): {drops}" if drops else "no protected drop beyond noise", **_stat(p)}
    h = sum(len(cand["cases"][cid]["hallucinated_amounts"]) for cid in gated)
    ks = [len(rep_scores(cand["cases"][cid])) for cid in gated]
    gates["G3"] = {"pass": h == 0, "detail": f"candidate hallucinated_amounts={h} (all reps)", "D": None, "SE": None, "n": len(gated), "k": min(ks) if ks else 0}
    held = [cid for cid in gated if cand["cases"][cid]["split"] == "heldout"]
    p = _paired(base, cand, held)
    gates["G4"] = {"pass": p["_D"] >= -p["_SE"] - EPS, "detail": f"heldout D={p['D']} SE={p['SE']} (n={p['n']}, k={p['k']}); D>=-SE 필요", **_stat(p)}
    return gates


def append_ledger(record: dict) -> None:
    p = ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def run_gate(rnd: int, base_v: str, cand_v: str, activate=None) -> str:
    base, cand = load_rep_scores(rnd, base_v), load_rep_scores(rnd, cand_v)
    if set(base["cases"]) != set(cand["cases"]):
        raise SystemExit("base와 candidate의 케이스 집합이 다름")
    if base.get("splits") != cand.get("splits"):
        raise SystemExit(f"base와 candidate의 splits가 다름: {base.get('splits')} vs {cand.get('splits')}")
    if not all(s in base["splits"] for s in DEFAULT_SPLITS):
        raise SystemExit("게이트는 train과 heldout이 모두 채점된 scores.json이 필요하다")
    for who, sc in (("base", base), ("candidate", cand)):
        low = [cid for cid, c in sc["cases"].items() if c["split"] in DEFAULT_SPLITS and len(rep_scores(c)) < 2]
        if low:
            raise SystemExit(f"{who}의 반복 횟수 k<2 (예: {low[0]}): 분산을 모르므로 게이트를 판정하지 않는다")
    cj = ROOT / "results" / "loop" / "runs" / f"r{rnd}" / "candidate.json"
    if not cj.exists():
        raise SystemExit(f"candidate.json 없음: {cj}")
    meta = json.loads(cj.read_text(encoding="utf-8"))
    train_ids = {cid for cid, c in base["cases"].items() if c["split"] == "train"}
    gates = gate_decide(base, cand, list(meta.get("target_case_ids") or []), train_ids)
    failed = [GATE_NAMES[g] for g, r in gates.items() if not r["pass"]]
    adopted = not failed
    record = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "round": rnd, "base": base_v, "candidate": cand_v,
              "adopted": adopted, "failed_gates": failed, "gates": gates, "splits": cand["splits"], "k": {"base": base["k"], "candidate": cand["k"]},
              "target_case_ids": meta.get("target_case_ids"), "target_failures": meta.get("target_failures"),
              "change_summary": meta.get("change_summary"), "scope": meta.get("scope"),
              "base_aggregates": base["aggregates"], "candidate_aggregates": cand["aggregates"]}
    if adopted:
        dst = ROOT / "skills" / "dart-recon" / f"SKILL.{cand_v}.md"
        if dst.exists():
            raise SystemExit(f"이미 존재: {dst.name}")
        dst.write_text(skill_path(cand_v).read_text(encoding="utf-8"), encoding="utf-8")
        if activate is None:
            from . import skill as skill_mod
            activate = skill_mod.use
        activate(cand_v)
    else:
        lessons = ROOT / "lessons"
        lessons.mkdir(parents=True, exist_ok=True)
        lines = [f"# r{rnd} {cand_v} 기각 (base {base_v})", "", f"실패한 게이트: {', '.join(failed)}", ""]
        lines += [f"- {GATE_NAMES[g]}: {r['detail']}" for g, r in gates.items() if not r["pass"]]
        lines += ["", f"변경 요약: {meta.get('change_summary')}", f"범위: {meta.get('scope')}", f"겨냥한 실패: {meta.get('target_failures')}", ""]
        (lessons / f"r{rnd}-{cand_v}.md").write_text("\n".join(lines), encoding="utf-8")
    append_ledger(record)
    return f"round={rnd} candidates=1 adopted={int(adopted)} failed_gates={','.join(failed) if failed else 'none'}"


# ---- 잡음 요약 -----------------------------------------------------------------------


def noise_summary(rnd: int, skill: str, top: int = 5) -> dict:
    """같은 스킬의 rep 간 변동: 분할별 rep별 평균·rep 평균들의 표본표준편차, 케이스별 sd 상위, 집계 SE. noise.json에 저장."""
    scores = load_rep_scores(rnd, skill)
    k = scores["k"]
    if k < 2:
        raise SystemExit(f"k={k}: rep가 2개 이상이어야 잡음을 요약할 수 있다")
    out = {"round": rnd, "skill": skill, "k": k, "splits": {}, "top_case_sd": []}
    for sp in scores["splits"]:
        rows = [c for c in scores["cases"].values() if c["split"] == sp]
        by_rep = {j: _mean([c["reps"][j]["score"] for c in rows]) for j in sorted(rows[0]["reps"], key=int)}
        sd = _sd(list(by_rep.values()))
        out["splits"][sp] = {"n": len(rows), "rep_means": by_rep, "mean": scores["aggregates"][sp]["mean_score"],
                             "rep_mean_sd": None if sd is None else round(sd, 4), "se": scores["aggregates"][sp]["se"]}
    ranked = sorted(((cid, c) for cid, c in scores["cases"].items() if c["sd"] is not None), key=lambda x: (-x[1]["sd"], x[0]))
    out["top_case_sd"] = [{"case_id": cid, "split": c["split"], "sd": c["sd"], "reps": [r["score"] for r in c["reps"].values()]} for cid, c in ranked[:top]]
    (run_dir(rnd, skill) / "noise.json").write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out


# ---- 보고 ----------------------------------------------------------------------------


def build_report() -> dict:
    loop = ROOT / "results" / "loop"
    ledger = read_jsonl(ledger_path())
    status = {(r["round"], r["candidate"]): ("adopted" if r["adopted"] else "rejected: " + ",".join(r["failed_gates"])) for r in ledger}
    rows = []
    runs = loop / "runs"
    for sp in sorted(runs.glob("r*/*/scores.json"), key=lambda p: (int(p.parent.parent.name[1:]), int(p.parent.name[1:]))):
        s = json.loads(sp.read_text(encoding="utf-8"))
        a = s["aggregates"]
        legacy = any("reps" not in c for c in s["cases"].values())
        rows.append({"round": s["round"], "skill": s["skill"], "k": s.get("k", 1), "cases": a["all"]["n"], "legacy": legacy,
                     "train_mean": a["train"]["mean_score"], "train_n": a["train"]["n"],
                     "heldout_mean": a["heldout"]["mean_score"], "heldout_n": a["heldout"]["n"],
                     "hallucinated": a["all"]["hallucinated_amounts"], "forbidden_hits": a["all"]["forbidden_hits"],
                     "status": status.get((s["round"], s["skill"]), "baseline")})
    return {"rows": rows, "ledger_records": len(ledger), "adopted": sum(r["adopted"] for r in ledger),
            "rejected": sum(not r["adopted"] for r in ledger)}


def write_report() -> dict:
    rep = build_report()
    loop = ROOT / "results" / "loop"
    loop.mkdir(parents=True, exist_ok=True)
    (loop / "summary.json").write_text(json.dumps(rep, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md = ["# 개선 루프 요약", "", "| round | skill | k | 케이스 | legacy | train mean (n) | heldout mean (n) | hallucinated | 결과 |", "|---|---|---|---|---|---|---|---|---|"]
    for r in rep["rows"]:
        md.append(f"| {r['round']} | {r['skill']} | {r['k']} | {r['cases']} | {'legacy' if r['legacy'] else ''} | {r['train_mean']} ({r['train_n']}) | "
                  f"{r['heldout_mean']} ({r['heldout_n']}) | {r['hallucinated']} | {r['status']} |")
    md += ["", f"후보 {rep['ledger_records']}개 중 채택 {rep['adopted']}, 기각 {rep['rejected']}.", "",
           "legacy 행은 k 디렉터리 없이 1회만 실행한 11케이스 결과(구 게이트)다. 한계: 판정자 모델 1개, 모든 역할이 같은 모델 계열. "
           "게이트의 1-SE 규칙은 유의성 검정이 아니라 잡음 띠 밖의 차이만 인정하는 경험 규칙이다. 같은 케이스를 여러 라운드 재사용하면 홀드아웃도 점차 오염된다.", ""]
    (loop / "summary.md").write_text("\n".join(md), encoding="utf-8")
    return rep


CASE_KEYS = {"id", "question", "corp", "year", "expected_cause", "expected_basis", "required_facts", "forbidden", "split", "protected", "tier"}


def case_errors(c: dict) -> list[str]:
    """케이스 스키마 점검(캐시 불필요). accepted_causes는 선택 필드."""
    errs = []
    extra, missing = set(c) - CASE_KEYS - {"accepted_causes"}, CASE_KEYS - set(c)
    if extra or missing:
        errs.append(f"키 불일치 extra={sorted(extra)} missing={sorted(missing)}")
        return errs
    if not isinstance(c["id"], str) or not _ID.match(c["id"]):
        errs.append("id 형식")
    if c["expected_cause"] not in TAXONOMY:
        errs.append(f"expected_cause {c['expected_cause']!r}")
    acc = c.get("accepted_causes")
    if acc is not None and (not isinstance(acc, list) or c["expected_cause"] not in acc or any(a not in TAXONOMY for a in acc)):
        errs.append(f"accepted_causes는 expected_cause를 포함한 분류 라벨 목록이어야 함: {acc!r}")
    if c["expected_basis"] not in ("original_checked", "api_only"):
        errs.append(f"expected_basis {c['expected_basis']!r}")
    if c["split"] not in SPLITS:
        errs.append(f"split {c['split']!r}")
    if c["tier"] not in TIERS:
        errs.append(f"tier {c['tier']!r}")
    if not (isinstance(c["required_facts"], list) and c["required_facts"] and all(isinstance(f, str) and f for f in c["required_facts"])):
        errs.append("required_facts는 비어 있지 않은 문자열 목록")
    for pat in c["forbidden"] if isinstance(c["forbidden"], list) else [None]:
        try:
            re.compile(pat)
        except (re.error, TypeError):
            errs.append(f"forbidden 정규식 오류: {pat!r}")
    if not isinstance(c["protected"], bool) or (c["protected"] and c["split"] != "train"):
        errs.append("protected는 bool이며 train에서만 가능")
    return errs


def check_cases() -> int:
    """케이스 스키마와, 각 케이스의 required_facts가 실제 도구 출력(캐시)에 있는지 확인. 캐시가 필요하다."""
    from . import mcp_server

    os.environ["DART_OFFLINE"] = "1"
    bad = 0
    cases = load_cases()
    dup = {i for i in (c["id"] for c in cases) if [x["id"] for x in cases].count(i) > 1}
    if dup:
        print(f"id 중복: {sorted(dup)}")
        bad += len(dup)
    for c in cases:
        errs = case_errors(c)
        if errs:
            print(f"{c['id']}: SCHEMA {errs}")
            bad += 1
            continue
        text = ""
        try:
            for name, args in (("dart_check", {"corp": c["corp"], "year": c["year"]}), ("dart_explain", {"corp": c["corp"], "year": c["year"]})):
                text += mcp_server.call_tool({"name": name, "arguments": args})["content"][0]["text"]
        except Exception as exc:
            print(f"{c['id']}: 도구 실패 {exc}")
            bad += 1
            continue
        missing = [f for f in c["required_facts"] if f not in text]
        print(f"{c['id']}: {'OK' if not missing else 'MISSING ' + str(missing)}")
        bad += bool(missing)
    return bad


# ---- CLI -----------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="dart_recon.evalkit", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def rs(p, **kw):
        p.add_argument("--round", type=int, required=True)
        p.add_argument("--skill", required=True)
        return p

    p = rs(sub.add_parser("prompt")); p.add_argument("--case", required=True); p.add_argument("--rep", type=int, required=True)
    p = rs(sub.add_parser("tool")); p.add_argument("--case", required=True); p.add_argument("--rep", type=int, required=True)
    p.add_argument("name"); p.add_argument("args", nargs="?", default="{}")
    p = rs(sub.add_parser("score")); p.add_argument("--splits", default=",".join(DEFAULT_SPLITS), help="쉼표로 구분한 분할(기본 train,heldout; 예: test)")
    rs(sub.add_parser("noise"))
    for name in ("judge-prompt", "judge-validate"):
        p = rs(sub.add_parser(name)); p.add_argument("--rep", type=int, default=1)
    rs(sub.add_parser("improve-prompt"))
    p = sub.add_parser("gate"); p.add_argument("--round", type=int, required=True); p.add_argument("--base", required=True); p.add_argument("--cand", required=True)
    sub.add_parser("report")
    sub.add_parser("check-cases")
    a = ap.parse_args(argv)

    if a.cmd == "prompt":
        print(analyst_prompt(a.case, _check_id(a.skill, "skill"), a.round, a.rep))
    elif a.cmd == "tool":
        try:
            args = json.loads(a.args)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"인자가 JSON이 아님: {exc}")
        if not isinstance(args, dict):
            raise SystemExit("인자는 JSON 객체여야 함")
        print(run_tool(a.round, a.skill, a.case, a.name, args, a.rep))
    elif a.cmd == "score":
        res = score_run(a.round, _check_id(a.skill, "skill"), tuple(x for x in a.splits.split(",") if x))
        for cid, c in res["cases"].items():
            print(f"{cid:28} {c['split']:8} score={c['score']} sd={c['sd']} k={c['k']} cause={c['cause_match']} recall={c['facts_recall']} basis={c['basis_ok']} "
                  f"halluc={len(c['hallucinated_amounts'])} forbidden={len(c['forbidden_hits'])}{' MISSING' if c['missing_answer'] else ''}")
        print(json.dumps(res["aggregates"], ensure_ascii=False))
    elif a.cmd == "noise":
        print(json.dumps(noise_summary(a.round, _check_id(a.skill, "skill")), ensure_ascii=False, indent=2))
    elif a.cmd == "judge-prompt":
        print(judge_prompt(a.round, _check_id(a.skill, "skill"), a.rep))
    elif a.cmd == "judge-validate":
        res = judge_validate(a.round, _check_id(a.skill, "skill"), a.rep)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        if not res["valid"]:
            raise SystemExit(1)
    elif a.cmd == "improve-prompt":
        print(improve_prompt(a.round, _check_id(a.skill, "skill")))
    elif a.cmd == "gate":
        print(run_gate(a.round, _check_id(a.base, "base"), _check_id(a.cand, "cand")))
    elif a.cmd == "report":
        rep = write_report()
        print(f"rows={len(rep['rows'])} adopted={rep['adopted']} rejected={rep['rejected']}")
    elif a.cmd == "check-cases":
        raise SystemExit(1 if check_cases() else 0)


if __name__ == "__main__":
    main(sys.argv[1:])
