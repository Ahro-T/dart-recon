"""시점별 버전(point-in-time) 테이블: 같은 (회사, 재무제표구분, 계정, 회계연도) 값을 보고서마다 따로 저장한다.

- 최초값: 해당 연도 사업보고서의 당기(thstrm) 값, reported_in_year = 회계연도
- 나중값: 다음 연도 사업보고서의 전기(frmtrm) 값, reported_in_year = 회계연도 + 1
data/raw/fs 캐시만 읽는다(네트워크 요청 없음). DART_OFFLINE=1로 실행해야 한다.

    DART_OFFLINE=1 python3 -m dart_recon.pit --year 2025 --sample 600 --seed 7
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
from collections import Counter
from pathlib import Path

from . import client
from .rules import ACCOUNTS, RESTATEMENT_ACCOUNTS, Statements, _reporting_unit, amount, restatement

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "pit.sqlite"
RESULTS = ROOT / "results"
UNSTANDARD = "-표준계정코드 미사용-"

SCHEMA = """
CREATE TABLE IF NOT EXISTS pit (
    corp_code        TEXT NOT NULL,
    corp_name        TEXT NOT NULL,
    fs_div           TEXT NOT NULL,     -- CFS 연결 / OFS 별도
    account_key      TEXT NOT NULL,     -- sj_div|account_id(없으면 계정명)|account_detail
    fiscal_year      INTEGER NOT NULL,
    value            INTEGER NOT NULL,  -- 원 단위 그대로(공시 단위로 나누지 않음)
    reported_in_year INTEGER NOT NULL,  -- 이 값이 실린 사업보고서의 연도
    rcept_no         TEXT NOT NULL,
    unit             INTEGER NOT NULL,  -- 추정 공시 단위 1 / 1000 / 1000000
    PRIMARY KEY (corp_code, fs_div, account_key, fiscal_year, reported_in_year)
);
-- 연도별 최초값: 회계연도마다 reported_in_year가 가장 작은 행
CREATE VIEW IF NOT EXISTS as_first_reported AS
    SELECT p.* FROM pit p WHERE p.reported_in_year = (
        SELECT MIN(q.reported_in_year) FROM pit q
        WHERE q.corp_code = p.corp_code AND q.fs_div = p.fs_div
          AND q.account_key = p.account_key AND q.fiscal_year = p.fiscal_year);
-- 가장 나중 보고값: reported_in_year가 가장 큰 행
CREATE VIEW IF NOT EXISTS latest AS
    SELECT p.* FROM pit p WHERE p.reported_in_year = (
        SELECT MAX(q.reported_in_year) FROM pit q
        WHERE q.corp_code = p.corp_code AND q.fs_div = p.fs_div
          AND q.account_key = p.account_key AND q.fiscal_year = p.fiscal_year);
-- 최초값 != 최신값인 행: 재작성 후보
CREATE VIEW IF NOT EXISTS restatement_candidates AS
    SELECT f.corp_code, f.corp_name, f.fs_div, f.account_key, f.fiscal_year,
           f.value AS first_value, f.reported_in_year AS first_reported_in,
           l.value AS latest_value, l.reported_in_year AS latest_reported_in,
           l.value - f.value AS diff, l.unit AS unit
    FROM as_first_reported f JOIN latest l
      ON l.corp_code = f.corp_code AND l.fs_div = f.fs_div
     AND l.account_key = f.account_key AND l.fiscal_year = f.fiscal_year
    WHERE l.value != f.value;
"""


def connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def account_key(row: dict) -> str:
    aid = row.get("account_id") or UNSTANDARD
    ident = row["account_nm"] if aid == UNSTANDARD else aid
    return f"{row['sj_div']}|{ident}|{row.get('account_detail') or '-'}"


def pit_rows(payload: dict, corp_name: str, report_year: int, fs_div: str) -> tuple[list[tuple], int]:
    """한 보고서(corp, 보고연도, fs_div)에서 pit 행을 만든다. 같은 키가 한 보고서에 둘 이상이면
    어느 쪽이 어느 값인지 알 수 없으므로 건너뛰고 건너뛴 키 수를 함께 돌려준다."""
    rows = payload.get("list") or []
    if payload.get("status") != "000" or not rows:
        return [], 0
    counts = Counter(account_key(r) for r in rows)
    unit = _reporting_unit(rows)
    out = []
    for r in rows:
        key = account_key(r)
        if counts[key] > 1:
            continue
        for column, fiscal_year in (("thstrm_amount", report_year), ("frmtrm_amount", report_year - 1)):
            v = amount(r.get(column))
            if v is None:
                continue
            out.append((r["corp_code"], corp_name, fs_div, key, fiscal_year, v, report_year, r["rcept_no"], unit))
    return out, sum(1 for c in counts.values() if c > 1)


def load_payload(conn: sqlite3.Connection, payload: dict, corp_name: str, report_year: int, fs_div: str) -> tuple[int, int]:
    """(적재 행 수, 건너뛴 모호한 키 수). 같은 PK는 덮어쓰므로 다시 적재해도 중복이 생기지 않는다."""
    rows, skipped = pit_rows(payload, corp_name, report_year, fs_div)
    with conn:
        conn.executemany("INSERT OR REPLACE INTO pit VALUES (?,?,?,?,?,?,?,?,?)", rows)
    return len(rows), skipped


def read_cache(corp_code: str, year: int, fs_div: str) -> dict | None:
    """캐시에 있는 응답만 읽는다. 없으면 None(네트워크 요청 없음)."""
    path = client.CACHE / "fs" / f"{corp_code}_{year}_{client.ANNUAL}_{fs_div}.json"
    return json.loads(path.read_text()) if path.exists() else None


def load_cache(conn: sqlite3.Connection, corps: list[dict], years: list[int]) -> dict:
    stats = {"files_loaded": 0, "rows_loaded": 0, "ambiguous_keys_skipped": 0}
    for corp in corps:
        for year in years:
            for fs_div in ("CFS", "OFS"):
                payload = read_cache(corp["corp_code"], year, fs_div)
                if payload is None:
                    continue
                n, skipped = load_payload(conn, payload, corp["corp_name"], year, fs_div)
                stats["files_loaded"] += 1 if n else 0
                stats["rows_loaded"] += n
                stats["ambiguous_keys_skipped"] += skipped
    return stats


def rule_account_keys() -> dict[str, str]:
    """rules.RESTATEMENT_ACCOUNTS의 표준 account_id를 pit account_key로 옮긴 표(이름 대체 매칭 계정은 제외)."""
    out = {}
    for name in RESTATEMENT_ACCOUNTS:
        sj, ids, _ = ACCOUNTS[name]
        for sj_div in (("IS", "CIS") if sj == "IS" else (sj,)):
            for aid in ids:
                out[f"{sj_div}|{aid}|-"] = name
    return out


def pit_flagged(conn: sqlite3.Connection, year: int) -> dict[tuple[str, str], list[str]]:
    """(corp_code, fs_div) -> 최초값≠최신값인 account_key 목록(회계연도 year-1, 2개 보고서 비교)."""
    out: dict[tuple[str, str], list[str]] = {}
    for corp_code, fs_div, key in conn.execute(
        "SELECT corp_code, fs_div, account_key FROM restatement_candidates "
        "WHERE fiscal_year = ? AND first_reported_in = ? AND latest_reported_in = ?", (year - 1, year - 1, year)
    ):
        out.setdefault((corp_code, fs_div), []).append(key)
    return out


def crosscheck(conn: sqlite3.Connection, corps: list[dict], year: int) -> dict:
    """rules.restatement(mismatch/reclassified)가 가리키는 회사와 pit 차이 회사를 비교한다.

    비교 대상은 rules가 실제로 restatement를 실행한 회사(연도 N-1, N 보고서가 같은 fs_div로 캐시에 있고
    run.load와 같은 규칙으로 CFS 우선 선택). pit 쪽은 두 가지: 모든 계정, rules와 같은 7개 계정."""
    from .run import load  # 지연 import: run은 client 캐시만 사용(DART_OFFLINE=1)

    flagged = pit_flagged(conn, year)
    rule_keys = rule_account_keys()
    stats = Counter()
    samples = {"rules_only": [], "pit_only": []}
    for corp in corps:
        try:
            cur, prev = load(corp, year), load(corp, year - 1)
        except RuntimeError:
            continue
        if cur is None or prev is None or cur.fs_div != prev.fs_div:
            continue
        stats["compared_corps"] += 1
        findings = restatement(cur, prev)
        bad = [f for f in findings if f.status in ("mismatch", "reclassified")]
        keys = flagged.get((corp["corp_code"], cur.fs_div), [])
        narrow = [k for k in keys if k in rule_keys]
        for label, pit_keys in (("all_accounts", keys), ("rule_accounts", narrow)):
            rules_flag, pit_flag = bool(bad), bool(pit_keys)
            if rules_flag and pit_flag:
                stats[f"{label}_both_flagged"] += 1
            elif not rules_flag and not pit_flag:
                stats[f"{label}_both_clean"] += 1
            elif rules_flag:
                stats[f"{label}_rules_only"] += 1
                if label == "rule_accounts" and len(samples["rules_only"]) < 5:
                    samples["rules_only"].append({"corp": corp["corp_name"], "fs_div": cur.fs_div,
                                                  "rules": [f.detail.split(":")[0] for f in bad], "pit_keys_any": keys[:3]})
            else:
                stats[f"{label}_pit_only"] += 1
                if label == "all_accounts" and len(samples["pit_only"]) < 5:
                    samples["pit_only"].append({"corp": corp["corp_name"], "fs_div": cur.fs_div,
                                                "pit_keys": keys[:4], "pit_keys_in_rule_accounts": narrow})
    agree = {l: stats[f"{l}_both_flagged"] + stats[f"{l}_both_clean"] for l in ("all_accounts", "rule_accounts")}
    disagree = {l: stats[f"{l}_rules_only"] + stats[f"{l}_pit_only"] for l in ("all_accounts", "rule_accounts")}
    return {"compared_corps": stats["compared_corps"], "agree": agree, "disagree": disagree,
            "detail": dict(sorted(stats.items())), "samples": samples}


def summarize(conn: sqlite3.Connection) -> dict:
    one = lambda q: conn.execute(q).fetchone()[0]
    return {
        "pit_rows": one("SELECT COUNT(*) FROM pit"),
        "pit_corps": one("SELECT COUNT(DISTINCT corp_code) FROM pit"),
        "first_vs_latest_rows": one("SELECT COUNT(*) FROM as_first_reported f JOIN latest l USING "
                                    "(corp_code, fs_div, account_key, fiscal_year) "
                                    "WHERE f.reported_in_year != l.reported_in_year"),
        "restatement_candidate_rows": one("SELECT COUNT(*) FROM restatement_candidates"),
        "restatement_candidate_corps": one("SELECT COUNT(DISTINCT corp_code) FROM restatement_candidates"),
        "restatement_candidate_corp_fs": one("SELECT COUNT(*) FROM (SELECT DISTINCT corp_code, fs_div FROM restatement_candidates)"),
    }


def main() -> None:
    if os.environ.get("DART_OFFLINE") != "1":
        raise SystemExit("DART_OFFLINE=1 필요: pit는 캐시만 읽는다")
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=2025)
    ap.add_argument("--sample", type=int, default=600)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    corps = client.listed_corps()
    sample = random.Random(args.seed).sample(corps, min(args.sample, len(corps)))
    conn = connect()
    load_stats = load_cache(conn, sample, [args.year - 1, args.year])
    summary = {"year": args.year, "sampled": len(sample), "load": load_stats, **summarize(conn),
               "crosscheck_vs_rules_restatement": crosscheck(conn, sample, args.year)}
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"pit_{args.year}_n{args.sample}_s{args.seed}_summary.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
