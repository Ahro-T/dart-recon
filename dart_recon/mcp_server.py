"""stdio MCP 서버(줄 단위 JSON-RPC 2.0). 규칙 엔진을 도구 5개로 노출한다. 표준 라이브러리만 사용.

    python3 -m dart_recon.mcp_server

캐시(data/raw, DART_RECON_DATA로 교체 가능)만 읽는다. 네트워크 요청 없음(DART_OFFLINE=1 강제).
숫자는 규칙 엔진·캐시의 원본 값 그대로 돌려준다.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

from . import client, pit, triage
from .rules import ACCOUNTS, RULE_IDS, Statements, reconciling_items, restatement, run_single

RULES_DIR = Path(__file__).resolve().parent.parent / "rules"
PROTOCOL_VERSION = "2025-06-18"
STATUS_ORDER = ("mismatch", "reclassified", "rounding", "missing", "ok")
CODE_BY_ID = {v: k for k, v in RULE_IDS.items()}
CAUSES = {
    "rounding_typo": "원 단위 공시에서 차이가 1만 원 이하(반올림·오타급)",
    "held_for_sale_cash": "현금흐름표 기말현금이 재무상태표 현금보다 크고 같은 기간 재무상태표에 매각예정 자산이 있음",
    "column_shift": "'순증감' 줄의 값이 같은 공시의 기초/기말 현금 값과 같음(줄·열 밀림 또는 계정 태깅 오류)",
    "restatement_discontinued": "매출만 바뀌고 순이익·자산·자본은 그대로이며 전기 열에 중단영업 손익이 있음",
    "restatement": "그 외 전기 숫자 변경(재작성·재분류·연결범위 변경 등)",
    "unclassified": "위 가설에 해당하지 않음",
}
# 규칙별로 근거가 되는 계정 key (rules.ACCOUNTS)
EVIDENCE_KEYS = {
    "bs_equation": ("assets", "liabilities", "equity"),
    "cf_end_vs_bs_cash": ("cf_end", "bs_cash"),
    "cf_bridge": ("cf_begin", "cf_end", "cf_operating", "cf_investing", "cf_financing"),
    "cf_net_change_label": ("cf_change", "cf_begin", "cf_end", "cf_operating", "cf_investing", "cf_financing"),
    "cf_prior_end_is_current_begin": ("cf_begin", "cf_end"),
}


class ToolError(Exception):
    """사용자에게 보일 도구 오류(isError 결과). 잘못된 인자는 RpcError."""


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code, self.message = code, message


# ---- 캐시 조회 -----------------------------------------------------------------------


def _fs_dir() -> Path:
    return client.CACHE / "fs"


def find_corps(query: str) -> list[dict]:
    q = query.strip()
    corps = client.listed_corps()
    exact = [c for c in corps if q in (c["corp_code"], c["stock_code"], c["corp_name"])]
    if exact:
        return exact
    return [c for c in corps if q and q in c["corp_name"]]


def resolve_corp(query: str) -> dict:
    found = find_corps(query)
    if not found:
        raise ToolError(f"회사를 찾지 못함: {query!r}")
    if len(found) > 1:
        names = ", ".join(f"{c['corp_name']}({c['corp_code']})" for c in found[:10])
        raise ToolError(f"여러 회사가 일치함, 이름 전체나 corp_code로 다시 지정: {names}")
    return found[0]


def cached_filings(corp_code: str) -> dict[int, list[str]]:
    """캐시에 실제 공시(status 000, 줄 있음)가 있는 연도 -> fs_div 목록."""
    out: dict[int, list[str]] = {}
    for path in sorted(_fs_dir().glob(f"{corp_code}_*_{client.ANNUAL}_*.json")):
        m = re.fullmatch(rf"{corp_code}_(\d{{4}})_{client.ANNUAL}_(CFS|OFS)\.json", path.name)
        if not m:
            continue
        payload = json.loads(path.read_text())
        if payload.get("status") == "000" and payload.get("list"):
            out.setdefault(int(m.group(1)), []).append(m.group(2))
    return out


def load_statements(corp: dict, year: int, fs_div: str | None) -> Statements | None:
    """캐시된 공시만. fs_div 미지정이면 연결(CFS) 우선, 없으면 별도(OFS)."""
    for div in ((fs_div,) if fs_div else ("CFS", "OFS")):
        try:
            payload = client.financials(corp["corp_code"], year, div)
        except RuntimeError:  # 캐시에 없음(offline)
            continue
        if payload.get("status") == "000" and payload.get("list"):
            return Statements(payload, corp["corp_name"], year, div)
    return None


def _need_statements(corp: dict, year: int, fs_div: str | None) -> Statements:
    s = load_statements(corp, year, fs_div)
    if s is None:
        have = cached_filings(corp["corp_code"])
        raise ToolError(f"캐시에 {corp['corp_name']} {year}년 {fs_div or '연결/별도'} 공시 없음. 캐시된 공시: "
                        + (json.dumps({str(y): d for y, d in have.items()}, ensure_ascii=False) or "{}"))
    return s


def run_checks(corp: dict, year: int, fs_div: str | None) -> tuple[Statements, Statements | None, list[dict]]:
    cur = _need_statements(corp, year, fs_div)
    findings = run_single(cur)
    prev = load_statements(corp, year - 1, cur.fs_div)
    if prev is not None:
        findings.extend(restatement(cur, prev))
    return cur, prev, [f.to_dict() for f in findings]


def _public(f: dict) -> dict:
    out = {"rule": RULE_IDS[f["rule"]], "code": f["rule"]}
    out.update({k: f[k] for k in ("period", "status", "lhs", "rhs", "diff", "unit", "detail", "rcept_no")})
    return out


def _sorted_findings(findings: list[dict]) -> list[dict]:
    return sorted(findings, key=lambda f: STATUS_ORDER.index(f["status"]))  # 안정 정렬: 같은 상태는 규칙 순서 유지


# ---- 도구 ----------------------------------------------------------------------------


def dart_company(args: dict) -> dict:
    found = find_corps(args["query"])
    return {
        "query": args["query"],
        "matches": [
            {**c, "cached_filings": {str(y): d for y, d in sorted(cached_filings(c["corp_code"]).items())}}
            for c in found[:10]
        ],
        "total_matches": len(found),
    }


def dart_check(args: dict) -> dict:
    corp = resolve_corp(args["corp"])
    cur, prev, findings = run_checks(corp, args["year"], args.get("fs_div"))
    return {
        "corp_name": corp["corp_name"], "corp_code": corp["corp_code"], "year": cur.year, "fs_div": cur.fs_div,
        "rcept_no": cur.rcept_no, "restatement_checked": prev is not None,
        "summary": dict(Counter(f["status"] for f in findings)),
        "findings": [_public(f) for f in _sorted_findings(findings)],
    }


def _evidence(label: str, s: Statements, keys) -> list[dict]:
    out = []
    for key in keys:
        r = s.row(key)
        if r is not None:
            out.append({"filing": label, "rcept_no": s.rcept_no, "key": key, "account_nm": r["account_nm"],
                        "account_id": r["account_id"], "sj_div": r["sj_div"],
                        "thstrm_amount": r.get("thstrm_amount"), "frmtrm_amount": r.get("frmtrm_amount")})
    return out


def _extra_rows(label: str, s: Statements, f: dict) -> list[dict]:
    """매각예정 현금, 조정 항목 등 규칙이 부가로 쓰는 줄."""
    names = {n for n, _ in reconciling_items(s, f["period"])} if f["rule"] == "cf_bridge" else set()
    out = []
    for r in s.rows:
        held = f["rule"] == "cf_end_vs_bs_cash" and "매각예정" in r["account_nm"]
        if held or (r["sj_div"] == "CF" and r["account_nm"] in names):
            out.append({"filing": label, "rcept_no": s.rcept_no, "key": None, "account_nm": r["account_nm"],
                        "account_id": r["account_id"], "sj_div": r["sj_div"],
                        "thstrm_amount": r.get("thstrm_amount"), "frmtrm_amount": r.get("frmtrm_amount")})
    return out


def dart_explain(args: dict) -> dict:
    corp = resolve_corp(args["corp"])
    cur, prev, findings = run_checks(corp, args["year"], args.get("fs_div"))
    bad = [f for f in findings if f["status"] not in ("ok",)]
    triaged = triage.triage(findings)["rows"]  # mismatch/rounding만 분류한다
    items = []
    for f in _sorted_findings(bad):
        cause = next((t for t in triaged if (t["rule"], t["period"], t["status"], t["diff"], t["detail"])
                      == (f["rule"], f["period"], f["status"], f["diff"], f["detail"])), None)
        ev: list[dict] = []
        if f["rule"] == "restatement":
            key = f["detail"].split(":")[0]
            ev += _evidence(f"{cur.year}년 보고서", cur, [key])
            if prev is not None:
                ev += _evidence(f"{prev.year}년 보고서", prev, [key])
        else:
            ev += _evidence(f"{cur.year}년 보고서", cur, EVIDENCE_KEYS[f["rule"]])
            ev += _extra_rows(f"{cur.year}년 보고서", cur, f)
        item = {"finding": _public(f), "evidence": ev}
        if cause:
            item["cause_candidate"] = {"category": cause["category"], "meaning": CAUSES[cause["category"]],
                                       "basis": "API 수치 기반 가설. 공시 원문 확인 전"}
        else:
            item["cause_candidate"] = None  # missing·reclassified는 triage 대상이 아님
        items.append(item)
    return {"corp_name": corp["corp_name"], "corp_code": corp["corp_code"], "year": cur.year, "fs_div": cur.fs_div,
            "non_ok_count": len(items), "items": items}


def dart_history(args: dict) -> dict:
    corp = resolve_corp(args["corp"])
    account, fs_div = args["account"], args.get("fs_div")
    have = cached_filings(corp["corp_code"])
    if not have:
        raise ToolError(f"캐시에 {corp['corp_name']} 공시 없음")
    if fs_div is None:
        fs_div = "CFS" if any("CFS" in d for d in have.values()) else "OFS"
    # series[account_key][(fiscal_year, reported_in)] = (value, rcept_no, unit)
    series: dict[str, dict[tuple[int, int], tuple]] = {}
    for year in sorted(y for y, d in have.items() if fs_div in d):
        s = load_statements(corp, year, fs_div)
        if s is None:
            continue
        if account in ACCOUNTS:
            for period, fy in (("thstrm", year), ("frmtrm", year - 1)):
                v = s.get(account, period)
                if v is not None:
                    series.setdefault(account, {})[(fy, year)] = (v, s.rcept_no, s.unit)
        else:
            rows, _ = pit.pit_rows(client.financials(corp["corp_code"], year, fs_div), corp["corp_name"], year, fs_div)
            for (_, _, _, key, fy, v, rep, rcept, unit) in rows:
                if key.split("|")[1] == account:
                    series.setdefault(key, {})[(fy, rep)] = (v, rcept, unit)
    if not series:
        raise ToolError(f"계정을 찾지 못함: {account!r}. rules.ACCOUNTS 키({', '.join(ACCOUNTS)}) 또는 account_id")
    out = []
    for key, pts in sorted(series.items()):
        years = []
        for fy in sorted({fy for fy, _ in pts}):
            reps = sorted(rep for f, rep in pts if f == fy)
            first, last = pts[(fy, reps[0])], pts[(fy, reps[-1])]
            years.append({"fiscal_year": fy, "first_reported_in": reps[0], "first_value": first[0],
                          "first_rcept_no": first[1], "latest_reported_in": reps[-1], "latest_value": last[0],
                          "latest_rcept_no": last[1], "unit": last[2],
                          "restated": first[0] != last[0],
                          "diff": last[0] - first[0] if first[0] != last[0] else 0})
        out.append({"account_key": key, "years": years})
    return {"corp_name": corp["corp_name"], "corp_code": corp["corp_code"], "fs_div": fs_div,
            "account": account, "series": out}


def _rule_title(path: Path) -> str:
    return path.read_text().splitlines()[0].lstrip("# ").strip()


def dart_rules(args: dict) -> dict:
    rid = args.get("rule_id")
    if rid:
        rid = RULE_IDS.get(rid, rid)  # 코드명도 허용
        path = RULES_DIR / f"{rid}.md"
        if rid not in CODE_BY_ID or not path.exists():
            raise ToolError(f"규칙을 찾지 못함: {args['rule_id']!r}. 가능한 ID: {', '.join(CODE_BY_ID)}")
        return {"rule": rid, "code": CODE_BY_ID[rid], "markdown": path.read_text()}
    return {"rules": [{"rule": rid, "code": code, "title": _rule_title(RULES_DIR / f"{rid}.md")}
                      for code, rid in RULE_IDS.items()]}


# ---- MCP -----------------------------------------------------------------------------

_CORP = {"type": "string", "description": "회사명, corp_code(8자리) 또는 종목코드(6자리)"}
_YEAR = {"type": "integer", "description": "사업연도(사업보고서 기준)"}
_FS = {"type": "string", "enum": ["CFS", "OFS"], "description": "CFS 연결 / OFS 별도. 생략하면 연결 우선"}
TOOLS = {
    "dart_company": (dart_company, "상장사 검색(이름·corp_code·종목코드)과 캐시에 있는 공시(연도, CFS/OFS) 확인.",
                     {"query": {"type": "string", "description": "회사명, corp_code 또는 종목코드"}}, ["query"]),
    "dart_check": (dart_check, "한 회사의 사업보고서에 모든 정합성 규칙(KDQ)을 적용한다. 전년도 공시가 캐시에 있으면 "
                   "전기 숫자 재작성 검사도 한다. 불일치 우선 정렬, 상태별 건수 포함.",
                   {"corp": _CORP, "year": _YEAR, "fs_div": _FS}, ["corp", "year"]),
    "dart_explain": (dart_explain, "dart_check의 불일치·반올림·누락 건에 대해 원인 후보(API 수치 기반 가설)와 "
                     "근거 계정 줄(account_nm, account_id, sj_div, 당기/전기 금액)을 돌려준다.",
                     {"corp": _CORP, "year": _YEAR, "fs_div": _FS}, ["corp", "year"]),
    "dart_history": (dart_history, "회계연도별 최초 보고값과 최신 보고값(캐시된 사업보고서 기준) 비교.",
                     {"corp": _CORP,
                      "account": {"type": "string", "description": "rules.ACCOUNTS 키(예: revenue, assets) 또는 account_id"},
                      "fs_div": _FS}, ["corp", "account"]),
    "dart_rules": (dart_rules, "규칙 목록(ID, 코드, 제목) 또는 rule_id를 주면 규칙 문서 전문.",
                   {"rule_id": {"type": "string", "description": "예: KDQ-0006 또는 restatement. 생략하면 목록"}}, []),
}


def tool_list() -> list[dict]:
    return [{"name": name, "description": desc,
             "inputSchema": {"type": "object", "properties": props, "required": req}}
            for name, (_, desc, props, req) in TOOLS.items()]


def _validate(name: str, args) -> dict:
    _, _, props, req = TOOLS[name]
    if not isinstance(args, dict):
        raise RpcError(-32602, "arguments must be an object")
    for k in req:
        if k not in args:
            raise RpcError(-32602, f"missing required argument: {k}")
    for k, v in args.items():
        spec = props.get(k)
        if spec is None:
            raise RpcError(-32602, f"unknown argument: {k}")
        ok = {"string": isinstance(v, str), "integer": isinstance(v, int) and not isinstance(v, bool)}[spec["type"]]
        if not ok:
            raise RpcError(-32602, f"argument {k} must be {spec['type']}")
        if "enum" in spec and v not in spec["enum"]:
            raise RpcError(-32602, f"argument {k} must be one of {spec['enum']}")
    return args


def call_tool(params) -> dict:
    if not isinstance(params, dict) or params.get("name") not in TOOLS:
        raise RpcError(-32602, f"unknown tool: {params.get('name') if isinstance(params, dict) else params!r}")
    name = params["name"]
    args = _validate(name, params.get("arguments") or {})
    try:
        result = TOOLS[name][0](args)
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}
    except Exception as exc:  # 도구 실패는 프로토콜 오류가 아니라 isError 결과
        return {"content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}], "isError": True}


def handle(msg) -> dict | None:
    if not isinstance(msg, dict) or "method" not in msg:
        return {"jsonrpc": "2.0", "id": msg.get("id") if isinstance(msg, dict) else None,
                "error": {"code": -32600, "message": "invalid request"}}
    method, mid = msg["method"], msg.get("id")
    if "id" not in msg:  # 알림: 응답 없음
        return None
    try:
        if method == "initialize":
            result = {"protocolVersion": (msg.get("params") or {}).get("protocolVersion") or PROTOCOL_VERSION,
                      "capabilities": {"tools": {}}, "serverInfo": {"name": "dart-recon", "version": "0.1.0"}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": tool_list()}
        elif method == "tools/call":
            result = call_tool(msg.get("params"))
        else:
            raise RpcError(-32601, f"method not found: {method}")
    except RpcError as e:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": e.code, "message": e.message}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main() -> None:
    os.environ["DART_OFFLINE"] = "1"  # 캐시만 읽는다
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            resp = handle(json.loads(line))
        except json.JSONDecodeError:
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
