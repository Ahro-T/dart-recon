"""Reconciliation rules over one company's DART financial statements.

Each rule returns Finding records. A finding is not an error by itself:
`status` is "ok" (exact), "rounding" (within reporting-unit rounding), "mismatch",
or "missing" (the rule's inputs are not in the filing).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

PERIODS = ("thstrm", "frmtrm")  # 당기, 전기


@dataclass
class Finding:
    rule: str
    corp_code: str
    corp_name: str
    year: int
    fs_div: str
    period: str
    status: str
    lhs: int | None
    rhs: int | None
    diff: int | None
    unit: int
    detail: str
    rcept_no: str

    def to_dict(self) -> dict:
        return asdict(self)


def amount(value: str | None) -> int | None:
    if value is None:
        return None
    s = value.replace(",", "").strip()
    if s in ("", "-"):
        return None
    try:
        return int(s)
    except ValueError:
        return int(float(s))


# Accounts identified by IFRS/DART id first, Korean name as fallback for filers that
# do not use standard account ids ("-표준계정코드 미사용-").
ACCOUNTS = {
    "assets": ("BS", ["ifrs-full_Assets"], ["자산총계"]),
    "liabilities": ("BS", ["ifrs-full_Liabilities"], ["부채총계"]),
    "equity": ("BS", ["ifrs-full_Equity"], ["자본총계"]),
    "bs_cash": ("BS", ["ifrs-full_CashAndCashEquivalents"], ["현금및현금성자산"]),
    "cf_begin": ("CF", ["dart_CashAndCashEquivalentsAtBeginningOfPeriodCf"], ["기초의현금및현금성자산", "기초현금및현금성자산"]),
    "cf_end": ("CF", ["dart_CashAndCashEquivalentsAtEndOfPeriodCf"], ["기말의현금및현금성자산", "기말현금및현금성자산"]),
    "cf_change": (
        "CF",
        [
            "ifrs-full_IncreaseDecreaseInCashAndCashEquivalents",
            "ifrs-full_IncreaseDecreaseInCashAndCashEquivalentsBeforeEffectOfExchangeRateChanges",
        ],
        ["현금및현금성자산의증가(감소)", "현금및현금성자산의순증가(감소)", "현금및현금성자산의순증감", "현금의증가(감소)"],
    ),
    "cf_fx": (
        "CF",
        ["ifrs-full_EffectOfExchangeRateChangesOnCashAndCashEquivalents"],
        ["외화환산으로인한현금변동", "외화환산으로인한현금의변동", "현금및현금성자산의환율변동효과"],
    ),
    "cf_operating": ("CF", ["ifrs-full_CashFlowsFromUsedInOperatingActivities"], ["영업활동현금흐름"]),
    "cf_investing": ("CF", ["ifrs-full_CashFlowsFromUsedInInvestingActivities"], ["투자활동현금흐름"]),
    "cf_financing": ("CF", ["ifrs-full_CashFlowsFromUsedInFinancingActivities"], ["재무활동현금흐름"]),
    "revenue": ("IS", ["ifrs-full_Revenue"], ["매출액", "수익(매출액)", "영업수익"]),
    "net_income": ("IS", ["ifrs-full_ProfitLoss"], ["당기순이익", "당기순이익(손실)"]),
}

# Accounts compared across consecutive annual reports to detect restated prior-year figures.
RESTATEMENT_ACCOUNTS = ("assets", "liabilities", "equity", "bs_cash", "revenue", "net_income", "cf_operating")


def _norm(name: str) -> str:
    return "".join(name.split())


class Statements:
    """Index of one filing (one corp, year, fs_div)."""

    def __init__(self, payload: dict, corp_name: str, year: int, fs_div: str):
        self.rows = payload.get("list", [])
        self.corp_name = corp_name
        self.year = year
        self.fs_div = fs_div
        self.corp_code = self.rows[0]["corp_code"] if self.rows else ""
        self.rcept_no = self.rows[0]["rcept_no"] if self.rows else ""
        self.unit = _reporting_unit(self.rows)

    def get(self, key: str, period: str) -> int | None:
        sj, ids, names = ACCOUNTS[key]
        sj_set = {"IS", "CIS"} if sj == "IS" else {sj}
        rows = [r for r in self.rows if r["sj_div"] in sj_set]
        for r in rows:
            if r["account_id"] in ids:
                return amount(r.get(f"{period}_amount"))
        wanted = {_norm(n) for n in names}
        for r in rows:
            if _norm(r["account_nm"]) in wanted:
                return amount(r.get(f"{period}_amount"))
        return None

    def finding(self, rule: str, period: str, lhs: int | None, rhs: int | None, detail: str) -> Finding:
        if lhs is None or rhs is None:
            return Finding(rule, self.corp_code, self.corp_name, self.year, self.fs_div, period,
                           "missing", lhs, rhs, None, self.unit, detail, self.rcept_no)
        diff = lhs - rhs
        return Finding(rule, self.corp_code, self.corp_name, self.year, self.fs_div, period,
                       classify(diff, self.unit), lhs, rhs, diff, self.unit, detail, self.rcept_no)


def _reporting_unit(rows: list[dict]) -> int:
    """Largest of 1, 1e3, 1e6 that divides every non-zero amount (원/천원/백만원 단위 추정)."""
    values = []
    for r in rows:
        for p in ("thstrm", "frmtrm"):
            v = amount(r.get(f"{p}_amount"))
            if v:
                values.append(v)
    for unit in (1_000_000, 1_000):
        if values and all(v % unit == 0 for v in values):
            return unit
    return 1


def classify(diff: int, unit: int) -> str:
    if diff == 0:
        return "ok"
    if abs(diff) <= 2 * unit:
        return "rounding"
    return "mismatch"


# ---- single-filing rules -------------------------------------------------------------


def bs_equation(s: Statements) -> list[Finding]:
    out = []
    for p in PERIODS:
        a, l, e = s.get("assets", p), s.get("liabilities", p), s.get("equity", p)
        rhs = None if l is None or e is None else l + e
        out.append(s.finding("bs_equation", p, a, rhs, "자산총계 = 부채총계 + 자본총계"))
    return out


def cf_end_vs_bs_cash(s: Statements) -> list[Finding]:
    return [
        s.finding("cf_end_vs_bs_cash", p, s.get("cf_end", p), s.get("bs_cash", p),
                  "현금흐름표 기말현금 = 재무상태표 현금및현금성자산")
        for p in PERIODS
    ]


# 영업·투자·재무 활동 밖에서 현금을 바꾸는 조정 항목. 회사마다 표시하는 항목과 이름이 다르다.
# 실제 불일치를 조사하다 밝혀진 항목을 여기 추가한다(예: 엔케이 2025 연결범위 변동 15.2억).
RECONCILING_IDS = (
    "ifrs-full_EffectOfExchangeRateChangesOnCashAndCashEquivalents",
    "dart_ChangesInConsolidatedCompanies",
)
RECONCILING_NAME_KEYS = ("환율", "외화환산", "연결범위", "연결대상범위", "매각예정")


def reconciling_items(s: Statements, period: str) -> list[tuple[str, int]]:
    items: dict[str, int] = {}
    for r in s.rows:
        if r["sj_div"] != "CF" or r.get("account_detail", "-") not in ("-", ""):
            continue
        name = _norm(r["account_nm"])
        if "증감" in name or "증가" in name:  # '환율변동효과 반영전 순증가' 같은 합계 줄 제외
            continue
        if r["account_id"] in RECONCILING_IDS or any(k in name for k in RECONCILING_NAME_KEYS):
            v = amount(r.get(f"{period}_amount"))
            if v:
                items[r["account_nm"]] = v
    return list(items.items())


def cf_bridge(s: Statements) -> list[Finding]:
    """기말 - 기초 = 영업 + 투자 + 재무 + (환율효과·연결범위 변동 등 조정 항목).

    조정 항목의 부분집합 중 차이를 가장 잘 설명하는 것을 고른다. 설명에 쓴 항목은 detail에 남긴다.
    """
    out = []
    for p in PERIODS:
        begin, end = s.get("cf_begin", p), s.get("cf_end", p)
        parts = [s.get(k, p) for k in ("cf_operating", "cf_investing", "cf_financing")]
        label = "기말 - 기초 = 영업 + 투자 + 재무 + 조정항목"
        if begin is None or end is None or None in parts:
            out.append(s.finding("cf_bridge", p, None if end is None or begin is None else end - begin, None, label))
            continue
        target, base = end - begin, sum(parts)
        items = reconciling_items(s, p)[:6]
        best_rhs, best_used = base, []
        for mask in range(1, 1 << len(items)):
            used = [items[i] for i in range(len(items)) if mask >> i & 1]
            rhs = base + sum(v for _, v in used)
            if abs(target - rhs) < abs(target - best_rhs):
                best_rhs, best_used = rhs, used
        detail = label + (" | 사용: " + ", ".join(f"{n}={v}" for n, v in best_used) if best_used else "")
        out.append(s.finding("cf_bridge", p, target, best_rhs, detail))
    return out


def cf_net_change_label(s: Statements) -> list[Finding]:
    """'현금 순증감'으로 공시한 숫자가 영업+투자+재무(조정 전) 또는 기말-기초(조정 후) 중 하나와 맞는가.

    어느 쪽에도 안 맞으면 표시된 순증감 숫자의 의미가 라벨과 다르다(예: '환율효과 반영전'인데 환산 항목 포함).
    """
    out = []
    for p in PERIODS:
        change = s.get("cf_change", p)
        begin, end = s.get("cf_begin", p), s.get("cf_end", p)
        parts = [s.get(k, p) for k in ("cf_operating", "cf_investing", "cf_financing")]
        label = "공시된 현금 순증감 = 영업+투자+재무 또는 기말-기초"
        if change is None or begin is None or end is None or None in parts:
            out.append(s.finding("cf_net_change_label", p, change, None, label))
            continue
        candidates = [sum(parts), end - begin]
        rhs = min(candidates, key=lambda c: abs(change - c))
        out.append(s.finding("cf_net_change_label", p, change, rhs, label))
    return out


def cf_prior_end_is_current_begin(s: Statements) -> list[Finding]:
    return [s.finding("cf_prior_end_is_current_begin", "thstrm", s.get("cf_begin", "thstrm"),
                      s.get("cf_end", "frmtrm"), "당기 기초현금 = 전기 기말현금")]


SINGLE_RULES = (bs_equation, cf_end_vs_bs_cash, cf_bridge, cf_net_change_label, cf_prior_end_is_current_begin)


def run_single(s: Statements) -> list[Finding]:
    out: list[Finding] = []
    for rule in SINGLE_RULES:
        out.extend(rule(s))
    return out


# ---- cross-filing rule ---------------------------------------------------------------


def restatement(current: Statements, previous: Statements) -> list[Finding]:
    """올해 보고서의 전기 숫자 vs 작년 보고서의 당기 숫자. 다르면 재작성(또는 재분류)."""
    out = []
    for key in RESTATEMENT_ACCOUNTS:
        now_prior = current.get(key, "frmtrm")
        then_current = previous.get(key, "thstrm")
        f = current.finding("restatement", "frmtrm", now_prior, then_current,
                            f"{key}: {current.year}년 보고서의 전기 = {previous.year}년 보고서의 당기")
        out.append(f)
    return out
