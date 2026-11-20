"""평가 하네스 + 게이트형 자기개선 루프의 결정론적 부분. LLM은 호출하지 않는다(표준 라이브러리만).

분석가/판정자/개선자 역할은 오케스트레이터가 Claude Code 서브에이전트로 실행하고, 이 모듈은 프롬프트 생성,
도구 호출 기록, 점수 계산, 스키마 검증, 게이트 판정, 원장 기록만 한다. 절차는 eval/PROTOCOL.md.

    python3 -m dart_recon.evalkit prompt --case ID --skill v0 --round 0
    python3 -m dart_recon.evalkit tool --round 0 --skill v0 --case ID dart_check '{"corp":"효성화학","year":2025}'
    python3 -m dart_recon.evalkit score --round 0 --skill v0
    python3 -m dart_recon.evalkit judge-prompt --round 0 --skill v0
    python3 -m dart_recon.evalkit judge-validate --round 0 --skill v0
    python3 -m dart_recon.evalkit improve-prompt --round 0 --skill v0
    python3 -m dart_recon.evalkit gate --round 0 --base v0 --cand v1
    python3 -m dart_recon.evalkit report
    python3 -m dart_recon.evalkit check-cases      # 케이스의 required_facts가 실제 도구 출력에 있는지(캐시 필요)
"""

from __future__ import annotations

import argparse
import bisect
import json
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
FAILURE_TYPES = ("wrong_cause", "missing_fact", "ungrounded_number", "wrong_basis", "overclaim", "false_alarm", "other")
TOOL_NAMES = ("dart_company", "dart_check", "dart_explain", "dart_history", "dart_rules")
_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


# ---- 경로 ----------------------------------------------------------------------------


def cases_path() -> Path:
    return ROOT / "eval" / "cases.jsonl"


def run_dir(rnd: int, skill: str) -> Path:
    return ROOT / "results" / "loop" / "runs" / f"r{rnd}" / skill


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
        elif isinstance(node, str):  # detail 같은 문장 속 숫자
            vals.update(int(x) for x in re.findall(r"\d{4,}", strip_commas(node)))

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


def run_tool(rnd: int, skill: str, case_id: str, name: str, args: dict) -> str:
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
    d = run_dir(rnd, skill)
    d.mkdir(parents=True, exist_ok=True)
    with (d / f"{case_id}.tools.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"tool": name, "args": args, "result": result, "is_error": is_error}, ensure_ascii=False) + "\n")
    return text


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


# ---- 채점 ----------------------------------------------------------------------------


def score_case(case: dict, answer: dict | None, tool_records: list[dict]) -> dict:
    base = {"split": case["split"], "protected": bool(case.get("protected"))}
    if answer is None or not isinstance(answer.get("answer"), str):
        return {**base, "missing_answer": True, "cause_match": 0, "facts_recall": 0.0, "missing_facts": list(case["required_facts"]),
                "basis_ok": 0, "hallucinated_amounts": [], "forbidden_hits": [], "score": 0.0}
    text = answer["answer"]
    cause_match = int(answer.get("cause") == case["expected_cause"])
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


def aggregate(cases: dict[str, dict]) -> dict:
    out = {}
    for name, pick in (("train", lambda c: c["split"] == "train"), ("heldout", lambda c: c["split"] == "heldout"), ("all", lambda c: True)):
        rows = [c for c in cases.values() if pick(c)]
        out[name] = {
            "n": len(rows),
            "mean_score": _mean([c["score"] for c in rows]),
            "cause_match": _mean([c["cause_match"] for c in rows]),
            "facts_recall": _mean([c["facts_recall"] for c in rows]),
            "basis_ok": _mean([c["basis_ok"] for c in rows]),
            "hallucinated_amounts": sum(len(c["hallucinated_amounts"]) for c in rows),
            "forbidden_hits": sum(len(c["forbidden_hits"]) for c in rows),
        }
    return out


def score_run(rnd: int, skill: str) -> dict:
    d = run_dir(rnd, skill)
    per = {}
    for case in load_cases():
        ans_file = d / f"{case['id']}.answer.json"
        answer = None
        if ans_file.exists():
            try:
                answer = json.loads(ans_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                answer = None
        per[case["id"]] = score_case(case, answer, read_jsonl(d / f"{case['id']}.tools.jsonl"))
    result = {"round": rnd, "skill": skill, "cases": per, "aggregates": aggregate(per)}
    d.mkdir(parents=True, exist_ok=True)
    scores = d / "scores.json"
    if scores.exists():  # 재채점 시 이전 judge 일치도 기록은 유지
        prev = json.loads(scores.read_text(encoding="utf-8"))
        if "agreement" in prev:
            result["agreement"] = prev["agreement"]
    scores.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def load_scores(rnd: int, skill: str) -> dict:
    p = run_dir(rnd, skill) / "scores.json"
    if not p.exists():
        raise SystemExit(f"scores.json 없음: {p} (먼저 score 실행)")
    return json.loads(p.read_text(encoding="utf-8"))


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


def judge_validate(rnd: int, skill: str) -> dict:
    d = run_dir(rnd, skill)
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
    scores = load_scores(rnd, skill)
    known = scores["cases"]
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
        det = known[jc["case_id"]]
        if det["missing_answer"]:
            continue
        j_halluc = any(c["label"] == "hallucinated" for c in jc["claims"])
        d_halluc = bool(det["hallucinated_amounts"])
        j_correct = jc["rubric"]["correctness"] >= 4
        rows.append({"case_id": jc["case_id"], "judge_hallucinated": j_halluc, "det_hallucinated": d_halluc,
                     "hallucination_agree": j_halluc == d_halluc, "judge_correct": j_correct,
                     "det_cause_match": bool(det["cause_match"]), "correctness_agree": j_correct == bool(det["cause_match"])})
    n = len(rows)
    agreement = {"n": n, "hallucination_agree": sum(r["hallucination_agree"] for r in rows),
                 "correctness_agree": sum(r["correctness_agree"] for r in rows), "cases": rows}
    scores["agreement"] = agreement
    (d / "scores.json").write_text(json.dumps(scores, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (d / "agreement.json").write_text(json.dumps(agreement, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"valid": True, "errors": [], "agreement": {k: agreement[k] for k in ("n", "hallucination_agree", "correctness_agree")}}


# ---- 프롬프트 ------------------------------------------------------------------------


def _taxonomy_text() -> str:
    return "\n".join(f"- `{k}`: {v}" for k, v in TAXONOMY.items())


def analyst_prompt(case_id: str, skill: str, rnd: int) -> str:
    case = case_by_id(case_id)
    skill_text = skill_path(skill).read_text(encoding="utf-8")
    d = f"results/loop/runs/r{rnd}/{skill}"
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
python3 -m dart_recon.evalkit tool --round {rnd} --skill {skill} --case {case_id} <도구이름> '<JSON 인자>'
```
예: `python3 -m dart_recon.evalkit tool --round {rnd} --skill {skill} --case {case_id} dart_check '{{"corp":"회사명","year":2025}}'`
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


def judge_prompt(rnd: int, skill: str) -> str:
    d = f"results/loop/runs/r{rnd}/{skill}"
    blocks = []
    for c in load_cases():
        ans = run_dir(rnd, skill) / f"{c['id']}.answer.json"
        ans_text = ans.read_text(encoding="utf-8").strip() if ans.exists() else "(답변 파일 없음)"
        blocks.append(f"""### {c['id']}
- 질문: {c['question']}
- 기대 원인: {c['expected_cause']} / 기대 근거 수준: {c['expected_basis']}
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
    """TRAIN 분할의 실패만 보여 준다. 홀드아웃 케이스는 id·질문·점수 모두 노출하지 않는다."""
    scores = load_scores(rnd, skill)
    d = run_dir(rnd, skill)
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
        ans = d / f"{cid}.answer.json"
        j = judge.get(cid, {})
        fails.append(f"""### {cid}
- 질문: {c['question']}
- 점수 {s['score']} (cause_match={s['cause_match']}, facts_recall={s['facts_recall']}, basis_ok={s['basis_ok']})
- 기대 원인: {c['expected_cause']} / 기대 근거 수준: {c['expected_basis']}
- 빠진 사실: {json.dumps(s['missing_facts'], ensure_ascii=False)}
- 근거 없는 금액: {json.dumps(s['hallucinated_amounts'], ensure_ascii=False)} / 금지 표현 적중: {json.dumps(s['forbidden_hits'], ensure_ascii=False)}
- 판정 실패 유형: {json.dumps(j.get('failure_types', []), ensure_ascii=False)} / 진단: {j.get('diagnosis', '(없음)')}
- 분석가 답변 파일: {ans.relative_to(ROOT) if ans.exists() else '(없음)'}""")
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
- 후보는 게이트를 통과해야 채택됩니다: G1 대상 케이스 평균 상승, G2 보호 케이스 점수 비하락, G3 근거 없는 금액 0건, G4 보이지 않는 홀드아웃 평균 비하락. 대부분의 후보는 기각됩니다.
- 이 프롬프트에 없는 케이스·정답 파일(eval/cases.jsonl 등)은 읽지 마세요.

## 현재 스킬 {skill}
{skill_path(skill).read_text(encoding='utf-8')}

## TRAIN 실패 (점수 < 1)
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


def gate_decide(base: dict, cand: dict, target_ids: list[str], train_ids: set[str]) -> dict:
    gates = {}
    bad = [t for t in target_ids if t not in train_ids]
    if not target_ids or bad:
        gates["G1"] = {"pass": False, "detail": f"target_case_ids가 비었거나 train 밖: {bad or '(empty)'}"}
    else:
        b = _mean([base["cases"][t]["score"] for t in target_ids])
        c = _mean([cand["cases"][t]["score"] for t in target_ids])
        gates["G1"] = {"pass": c > b, "detail": f"target mean base={b} cand={c}"}
    drops = [(cid, base["cases"][cid]["score"], s["score"]) for cid, s in cand["cases"].items()
             if s["protected"] and s["score"] < base["cases"][cid]["score"]]
    gates["G2"] = {"pass": not drops, "detail": f"protected drops: {drops}" if drops else "no protected drop"}
    h = cand["aggregates"]["all"]["hallucinated_amounts"]
    gates["G3"] = {"pass": h == 0, "detail": f"candidate hallucinated_amounts={h}"}
    bh, ch = base["aggregates"]["heldout"], cand["aggregates"]["heldout"]
    gates["G4"] = {"pass": ch["mean_score"] >= bh["mean_score"], "detail": f"heldout mean base={bh['mean_score']} cand={ch['mean_score']} (n={ch['n']})"}
    return gates


def append_ledger(record: dict) -> None:
    p = ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def run_gate(rnd: int, base_v: str, cand_v: str, activate=None) -> str:
    base, cand = load_scores(rnd, base_v), load_scores(rnd, cand_v)
    if set(base["cases"]) != set(cand["cases"]):
        raise SystemExit("base와 candidate의 케이스 집합이 다름")
    cj = ROOT / "results" / "loop" / "runs" / f"r{rnd}" / "candidate.json"
    if not cj.exists():
        raise SystemExit(f"candidate.json 없음: {cj}")
    meta = json.loads(cj.read_text(encoding="utf-8"))
    train_ids = {cid for cid, c in base["cases"].items() if c["split"] == "train"}
    gates = gate_decide(base, cand, list(meta.get("target_case_ids") or []), train_ids)
    failed = [GATE_NAMES[g] for g, r in gates.items() if not r["pass"]]
    adopted = not failed
    record = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "round": rnd, "base": base_v, "candidate": cand_v,
              "adopted": adopted, "failed_gates": failed, "gates": gates,
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
        rows.append({"round": s["round"], "skill": s["skill"], "train_mean": a["train"]["mean_score"], "train_n": a["train"]["n"],
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
    md = ["# 개선 루프 요약", "", "| round | skill | train mean (n) | heldout mean (n) | hallucinated | 결과 |", "|---|---|---|---|---|---|"]
    for r in rep["rows"]:
        md.append(f"| {r['round']} | {r['skill']} | {r['train_mean']} ({r['train_n']}) | {r['heldout_mean']} ({r['heldout_n']}) | {r['hallucinated']} | {r['status']} |")
    md += ["", f"후보 {rep['ledger_records']}개 중 채택 {rep['adopted']}, 기각 {rep['rejected']}.", "",
           "한계: 케이스 약 10개, 판정자 모델 1개, 모든 역할이 같은 모델 계열. 평균 차이는 케이스 1개 단위로 크게 움직이므로 통계적 유의성을 주장하지 않는다. "
           "같은 케이스를 여러 라운드 재사용하면 홀드아웃도 점차 오염된다.", ""]
    (loop / "summary.md").write_text("\n".join(md), encoding="utf-8")
    return rep


def check_cases() -> int:
    """각 케이스의 required_facts가 실제 도구 출력(캐시)에 있는지 확인. 캐시가 필요하다."""
    from . import mcp_server

    os.environ["DART_OFFLINE"] = "1"
    bad = 0
    for c in load_cases():
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

    p = rs(sub.add_parser("prompt")); p.add_argument("--case", required=True)
    p = rs(sub.add_parser("tool")); p.add_argument("--case", required=True); p.add_argument("name"); p.add_argument("args", nargs="?", default="{}")
    rs(sub.add_parser("score"))
    rs(sub.add_parser("judge-prompt"))
    rs(sub.add_parser("judge-validate"))
    rs(sub.add_parser("improve-prompt"))
    p = sub.add_parser("gate"); p.add_argument("--round", type=int, required=True); p.add_argument("--base", required=True); p.add_argument("--cand", required=True)
    sub.add_parser("report")
    sub.add_parser("check-cases")
    a = ap.parse_args(argv)

    if a.cmd == "prompt":
        print(analyst_prompt(a.case, _check_id(a.skill, "skill"), a.round))
    elif a.cmd == "tool":
        try:
            args = json.loads(a.args)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"인자가 JSON이 아님: {exc}")
        if not isinstance(args, dict):
            raise SystemExit("인자는 JSON 객체여야 함")
        print(run_tool(a.round, a.skill, a.case, a.name, args))
    elif a.cmd == "score":
        res = score_run(a.round, _check_id(a.skill, "skill"))
        for cid, c in res["cases"].items():
            print(f"{cid:28} {c['split']:8} score={c['score']} cause={c['cause_match']} recall={c['facts_recall']} basis={c['basis_ok']} "
                  f"halluc={len(c['hallucinated_amounts'])} forbidden={len(c['forbidden_hits'])}{' MISSING' if c['missing_answer'] else ''}")
        print(json.dumps(res["aggregates"], ensure_ascii=False))
    elif a.cmd == "judge-prompt":
        print(judge_prompt(a.round, _check_id(a.skill, "skill")))
    elif a.cmd == "judge-validate":
        res = judge_validate(a.round, _check_id(a.skill, "skill"))
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
