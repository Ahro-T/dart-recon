# dart-recon

DART 공시 재무제표의 정합성 규칙 엔진, 그 엔진을 LLM 분석가가 쓰게 하는 MCP 서버, 그리고 분석가용 스킬 문서를 케이스로 평가하고 게이트로 걸러 개선하는 루프를 담은 저장소.

- **규칙 엔진:** 재무상태표·현금흐름표·전년 보고서 사이에서 항상 맞아야 하는 관계 6개(KDQ-0001~0006)를 검사한다. 규칙마다 ID·문서·테스트가 있다(`rules/`).
- **MCP 서버:** 규칙 엔진 결과를 LLM이 도구 호출로 읽게 한다(`dart_recon/mcp_server.py`).
- **평가·개선 루프:** 스킬 문서(`skills/dart-recon/`)를 케이스 46개(train 20 / heldout 14 / test 12)로 케이스마다 3회 반복 채점하고, 후보 스킬이 잡음(1-SE)을 고려한 4개 게이트를 모두 통과할 때만 채택한다(`eval/`).

**설계 원칙:** 숫자는 규칙 엔진만 계산하고, LLM은 설명과 근거 선택만 한다. 답변에 도구 출력에 없는 금액이 있으면 채점기가 0점을 준다.

이 도구는 회사의 잘잘못을 판정하거나 부실을 예측하지 않는다. 투자 조언이 아니다. 표준 라이브러리만 쓴다.

## 구조

```
[고정 검증기 - 사람이 만든 코드]            [에이전트 - Claude Code 서브에이전트]
 규칙 엔진 (dart_recon/rules.py)             분석가  dart-analyst  : MCP 도구로 질문에 답변
 MCP 서버  (dart_recon/mcp_server.py)        판정자  dart-judge    : 답변을 도구 로그와 대조 (읽기 전용)
 결정론적 채점기 (evalkit score)             개선자  dart-improver : train 실패만 보고 후보 스킬 1개 작성
 4조건 게이트 (evalkit gate G1~G4)
              \                                      /
               v                                    v
        [원장·교훈]  results/loop/ledger.jsonl, results/loop/runs/, lessons/
```

저장소에는 LLM API 호출 코드와 API 키가 없다. 분석가·판정자·개선자는 `.claude/agents/`의 에이전트 정의로 정의되고, Claude Code 서브에이전트가 수행한다(edgartools `cc_runner` 방식 참고). 저장소가 제공하는 것은 케이스, 프롬프트 생성기, 도구 호출 기록기, 채점기, 스키마 검증기, 게이트, 원장이다. 프로토콜 전체는 `eval/PROTOCOL.md`.

## MCP 서버

표준 라이브러리만 쓰는 stdio MCP 서버. 오프라인·캐시 우선이라 이미 캐시된 공시만 읽고 네트워크를 쓰지 않는다. 도구 5개:

| 도구 | 하는 일 |
|---|---|
| `dart_company` | 상장사 검색(이름·corp_code·종목코드)과 캐시에 있는 공시(연도, CFS/OFS) 확인 |
| `dart_check` | 한 회사의 사업보고서에 모든 정합성 규칙(KDQ)을 적용 |
| `dart_explain` | 불일치·반올림·누락 건의 원인 후보(API 수치 기반 가설)와 근거 |
| `dart_history` | 회계연도별 최초 보고값과 최신 보고값 비교(캐시된 사업보고서 기준) |
| `dart_rules` | 규칙 목록 또는 `rule_id`를 주면 규칙 문서 전문 |

실행: `python3 -m dart_recon.mcp_server`. Claude Code에서는 저장소 루트의 `.mcp.json`(서버 이름 `dart-recon`)이 같은 명령을 등록한다.

## 데모 대화

출처: `results/loop/runs/r2/v3/sk-discovery-restate.answer.json`(답변)과 같은 폴더의 `sk-discovery-restate.tools.jsonl`(도구 호출 기록). 이 답변의 점수는 1.0(`scores.json`). 질문 문구는 `eval/cases.jsonl`.

**질문:** SK디스커버리 2025 사업보고서의 전기 자산·부채·자본이 2024년 보고서와 다른데 어떻게 된 건가요?

**도구 호출(기록 6건, 인자 그대로, 결과는 생략):**
1. `dart_check {corp: "SK디스커버리", year: 2025}` → KDQ-0006 불일치 4건(summary: ok 12, mismatch 4)
2. `dart_explain {corp: "SK디스커버리", year: 2025}` → 원인 후보
3. `dart_history {corp: "SK디스커버리", year: 2025}` → 오류 `missing required argument: account`
4. `dart_explain {corp: "SK디스커버리", year: 2025}` → (재호출)
5. `dart_history {corp: "SK디스커버리", year: 2025, account: "assets"}` → 오류 `unknown argument: year`
6. `dart_history {corp: "SK디스커버리", account: "assets"}` → 회계연도별 최초값·최신값

도구 인자를 두 번 틀렸고 서버가 오류로 돌려준 뒤 고쳐 호출했다. 기록에 그대로 남아 있다.

**답변(`cause: restatement_disclosed`, `basis: api_only`):**

> SK디스커버리 2025 사업보고서(연결, 접수번호 20260317000867)의 전기(2024) 수치가 2024년 보고서(20250318001277)의 당기 값과 달라졌고, 이는 전기 재작성(restatement)으로 결론 내립니다. dart_check 결과 KDQ-0006 불일치 4건: 자산 전기 15142281241732 vs 2024년 보고서 15077692130416 (차이 64589111316), 부채 8639257973247 vs 8624337888533 (차이 14920084714), 자본 차이 49669026602, 당기순이익 차이 15186001008. dart_history로도 자산 2024 회계연도 최초값 15077692130416이 최신값 15142281241732로 바뀌어 restated=true(차이 64589111316)로 확인됩니다. dart_explain의 원인 후보는 네 건 모두 '전기 숫자 변경(재작성·재분류·연결범위 변경 등)'이며 이것이 도구 출력이 뒷받침하는 유일한 후보입니다. 한계: 이는 OpenDART API 수치 기반 판단이며 공시 원문 주석(재작성 사유, 연결범위 변경 등 구체 원인)은 확인하지 못했습니다.

이 케이스는 사람이 원문 주석으로 재작성 공시를 확인한 사례(`results/verified_cases.md`)지만, 분석가는 API 수치 도구만 쓰므로 근거 수준을 `api_only`로 쓴 것이 정답이다.

## 평가

**케이스:** 46개(`eval/cases.jsonl`). 분석가와 개선자는 이 파일을 읽지 않는다(에이전트 지침으로만 강제, 파일 권한은 아님). 원인 라벨은 none, discontinued_reclass, restatement_disclosed, rounding_or_typo, unexplained를 쓴다.
- verified 11개: 사람이 공시를 보고 원인을 정한 케이스. 원인 판단 정확도를 본다. train 7(보호 케이스 3) / heldout 4.
- derived 35개: `python3 -m dart_recon.casegen --seed 7`이 규칙 엔진·triage 결과에서 결정론적으로 만든 케이스(사람 확인 없음). 도구 출력을 빠짐없이 옮기는지, 근거 없이 단정하지 않는지를 본다. train 13 / heldout 10 / test 12. 라벨 분포: none 8, restatement_disclosed 8, unexplained 8, rounding_or_typo 6, discontinued_reclass 5.
- 케이스 id는 정답을 드러내지 않게 `v-<corp_code>`, `d-<corp_code>`로 쓴다. test 12개는 루프 밖에서 마지막에 한 번만 채점한다.

**반복 실행:** 같은 스킬도 실행마다 답이 달라서, 케이스마다 k=3회 돌려(`results/loop/runs/r<R>/<vN>/k<J>/`) 케이스 점수는 rep 평균, 분산은 rep 간 표본분산으로 잡는다. `evalkit noise`가 rep별 평균과 SE를 `noise.json`에 쓴다.

**채점 두 층:**
1. 결정론적(`evalkit score`): 케이스 점수 = mean(cause_match, facts_recall, basis_ok). 도구 로그에 없는 금액이 답변에 하나라도 있거나(hallucinated amount) 금지 표현(`forbidden`)이 걸리면 0점. 답변 파일이 없어도 0점. 4자리 이상 정수는 도구 결과의 정수, 또는 같은 객체 안 정수끼리의 차이와 대조하고, 조/억/만 단위 표기는 표기 정밀도만큼 반올림을 허용한다.
2. LLM 판정자(`dart-judge`): `eval/judge_schema.json` 스키마로 `judge.json`을 쓰고 스키마 검증을 통과해야 한다. 점수에는 반영하지 않고 결정론적 채점과의 일치도만 본다(`agreement.json`): 판정자의 hallucinated 주장 vs 결정론적 hallucinated_amounts, 판정자 correctness≥4 vs `cause_match`.

**판정자-결정론 일치도(`results/loop/runs/r*/*/agreement.json`, r0~r2는 n=11, r3는 rep 1의 n=34):**

| 라운드 | 스킬 | hallucination 일치 | correctness 일치 |
|---|---|---|---|
| 3 | v0 | 34/34 | 33/34 |
| 3 | v4 | 34/34 | 34/34 |
| 0 | v0 | 11/11 | 10/11 |
| 0 | v1 | 10/11 | 10/11 |
| 1 | v0 | 11/11 | 10/11 |
| 1 | v2 | 10/11 | 11/11 |
| 2 | v0 | 11/11 | 10/11 |
| 2 | v3 | 11/11 | 11/11 |

일치하지 않은 케이스가 있는 run에서는 판정자와 결정론적 채점기 중 어느 쪽이 맞는지를 이 숫자만으로 알 수 없다. r3 v0는 검증기 수정 전 hallucination 일치가 30/34였고, 불일치 4건은 모두 채점기 쪽 거짓 양성이었다(아래 검증기 수정).

## 개선 루프 결과

게이트(`evalkit gate`, r3부터, 4개 모두 통과해야 채택). 같은 라운드에서 base와 후보를 같은 케이스로 k회씩 돌려 케이스별로 짝지어 비교한다: d_c = 후보 rep 평균 − base rep 평균, se_c = sqrt(var_b/k_b + var_c/k_c), D = 평균 d_c, SE = sqrt(Σ se_c²)/n.
- G1 목표 케이스(train): D > SE
- G2 보호 케이스: 모든 케이스에서 d_c ≥ −se_c
- G3 후보의 근거 없는 금액이 전 케이스·전 rep에서 0건
- G4 heldout: D ≥ −SE

1-SE 규칙은 유의성 검정이 아니라 잡음 띠 밖의 차이만 인정하는 경험 규칙이다. k<2이거나 분할이 다르면 게이트를 거부한다. 개선자 프롬프트에는 train 실패만 들어가고 heldout·test 내용은 들어가지 않는다. r0~r2는 1회 실행·11케이스·구 게이트(평균 비교)였고 legacy로 그대로 남긴다.

`results/loop/summary.md`:

| round | skill | train mean (n) | heldout mean (n) | hallucinated | 결과 |
|---|---|---|---|---|---|
| 0 | v0 | 0.8333 (7) | 0.8611 (4) | 0 | baseline |
| 0 | v1 | 0.8095 (7) | 1.0 (4) | 1 | rejected: G1_targeted,G3_hallucination |
| 1 | v0 | 0.8492 (7) | 0.8611 (4) | 0 | baseline |
| 1 | v2 | 0.9286 (7) | 0.7083 (4) | 1 | rejected: G2_protected,G3_hallucination,G4_heldout |
| 2 | v0 | 0.8651 (7) | 0.9305 (4) | 0 | baseline |
| 2 | v3 | 0.881 (7) | 0.8611 (4) | 0 | rejected: G2_protected,G4_heldout |
| 3 | v0 | 0.9213 (20) | 0.9431 (14) | 0 | baseline (k=3) |
| 3 | v4 | 0.9324 (20) | 0.9524 (14) | 0 | rejected: G1_targeted (k=3) |

원장(`results/loop/ledger.jsonl`) 라운드별 한 줄:

```
round=0 candidates=1 adopted=0 failed_gates=G1_targeted,G3_hallucination
round=1 candidates=1 adopted=0 failed_gates=G2_protected,G3_hallucination,G4_heldout
round=2 candidates=1 adopted=0 failed_gates=G2_protected,G4_heldout
round=3 candidates=1 adopted=0 failed_gates=G1_targeted
```

**r3 (46케이스, k=3):**
- v0 기준선 잡음(`results/loop/runs/r3/v0/noise.json`): train rep별 평균 0.9167 / 0.9361 / 0.9111, 평균 0.9213, SE 0.0072(n=20). heldout 0.9444 / 0.9206 / 0.9643, 평균 0.9431, SE 0.0093(n=14). 0점이 아닌 케이스 사이의 흔들림은 대부분 facts_recall(train 0.8472) 차이다.
- v4 후보(금액을 도구 값 그대로 인용하라는 규칙 추가, 대상 `d-00531865`, `v-00128227`): 대상 D=0.037, SE=0.0524(n=2)로 G1 탈락. 보호 케이스 하락 없음, 근거 없는 금액 0건, heldout D=0.0093, SE=0.0107. 전체 평균은 train +0.0111, heldout +0.0093 올랐지만 잡음 띠 안이다.
- test 분할(1회, v0, k=3, n=12, 모두 derived): 평균 0.9182, SE 0.0145, cause_match 0.9444, facts_recall 0.8102, 근거 없는 금액 0건.

**결론:** 4라운드, 후보 4개, 채택 0. 케이스를 46개로 늘리고 3회 반복하자 v0 train 평균의 SE가 0.0072가 되어, 11케이스 1회 실행 때 라운드 간 변동(train 0.0318, heldout 0.0694)보다 훨씬 작은 차이를 구분할 수 있게 됐다. 그 대신 r3 기준선이 train 0.92, heldout 0.94로 높아서, 남은 실패는 주로 필수 사실 누락(facts_recall)이고 후보가 개선할 여지가 작다. 통계적 유의성은 주장하지 않는다. 기각 후보의 교훈은 `lessons/r<round>-<vM>.md`에 남고 다음 라운드 개선자 프롬프트가 읽는다.

**legacy r0~r2:** 같은 v0 스킬의 기준선 점수가 라운드마다 달랐다: train 평균 최소 0.8333, 최대 0.8651(차이 0.0318, n=7), heldout 평균 최소 0.8611, 최대 0.9305(차이 0.0694, n=4). 후보와 base의 차이(train −0.0238~+0.0794, heldout −0.1528~+0.1389)가 이 변동과 비슷한 크기라 후보의 좋고 나쁨을 가릴 수 없었다. 이것이 r3에서 반복 실행·짝 비교·케이스 확대를 넣은 이유다.

**검증기 수정(`eval/PROTOCOL.md` 검증기 수정 기록):** 2라운드 게이트 전에, `samjung-pulp-typo`의 금지 표현이 "재작성 아님" 같은 원인 배제 문장까지 잡아 원인을 맞힌 답변 5개(r0~r2 v0 3개, r0 v1, r1 v2)가 0점이 되는 것을 발견해 금지 표현을 원인 단정 문형으로 좁혔다. 같은 답변 파일로 r0~r2 `scores.json`을 다시 채점했고 채택/기각 결정은 바뀌지 않았으며, 수정 전 원장은 `ledger.pre-verifier-fix.jsonl`에 남아 있다. r1 개선자는 수정 전 문구의 r0 교훈을 보고 후보를 만들었다.

r3 게이트 후에도 금액 근거 검사의 거짓 양성 두 종류를 발견해 고쳤다. (1) 답변이 `dart_explain`의 가설 설명 "차이가 1만 원 이하"를 그대로 인용해도 "1만"을 근거 없는 금액으로 잡았다. (2) "전기 75000000만 있고"의 조사 '만'을 단위로 읽었다. r3 v0·v4에서 잡힌 근거 없는 금액 10건(v0 7, v4 3)이 모두 이 경우였다. 같은 답변 파일로 다시 채점하자 v0 train 평균이 0.8583 → 0.9213, SE가 0.0164 → 0.0072로 바뀌었고, 게이트 결과는 `G3_hallucination` → `G1_targeted` 실패로 바뀌었다(채택 여부는 그대로 기각). 수정 전 원장 줄은 `results/loop/ledger.r3-pre-verifier-fix.jsonl`에 남겼다. v4 후보는 수정 전 점수를 보고 만들어져서, 대상 케이스의 0점 일부가 실제 분석 오류가 아니었다.

## 기준선 v0는 의도적으로 약하다

`skills/dart-recon/SKILL.v0.md`는 순서 3단계(회사·연도 파악, `dart_check`, 불일치 시 `dart_explain`, 설명)와 도구 목록만 있다. 근거 수준(원문 확인 vs API 수치)을 구분하라는 지침, 원인 분류 지침, 숫자 인용 지침이 없다. 개선 폭을 보이기 위한 출발점이며 완성본이 아니다. 루프는 이 v0에서 시작했고, 현재 채택된 스킬은 `skills/dart-recon/CURRENT`가 가리키는 버전이다(후보 v1~v4는 `skills/dart-recon/candidates/`에 있고 전부 기각).

## 규칙 엔진

| ID | 코드 | 검사 |
|---|---|---|
| [KDQ-0001](rules/KDQ-0001.md) | `bs_equation` | 자산총계 = 부채총계 + 자본총계 |
| [KDQ-0002](rules/KDQ-0002.md) | `cf_end_vs_bs_cash` | 현금흐름표 기말현금 = 재무상태표 현금및현금성자산 (+ 현금흐름표 주석의 '매각예정자산집단에 포함된 현금') |
| [KDQ-0003](rules/KDQ-0003.md) | `cf_bridge` | 기말 − 기초 = 영업 + 투자 + 재무 + 조정항목(환율효과·연결범위 변동 등, 부분집합 중 최적) |
| [KDQ-0004](rules/KDQ-0004.md) | `cf_net_change_label` | 공시된 '현금 순증감'이 (영업+투자+재무) 또는 (기말−기초) 중 하나와 일치 |
| [KDQ-0005](rules/KDQ-0005.md) | `cf_prior_end_is_current_begin` | 당기 기초현금 = 전기 기말현금 |
| [KDQ-0006](rules/KDQ-0006.md) | `restatement` | 올해 보고서의 전기 숫자 = 작년 보고서의 당기 숫자 (자산·부채·자본·현금·매출·순이익·영업CF) |

상태: `ok`, `rounding`(공시 단위 2단위 이내), `mismatch`, `missing`(입력 계정 없음), `reclassified`(restatement 중 중단영업 재분류로 추정되는 매출 감소). 공시 단위(원/천원/백만원)는 숫자의 나눠떨어짐으로 추정한다. 불일치는 오류 확정이 아니며 원인(반올림·오타, 재작성, 기준 차이, 규칙 오탐)을 `triage`로 분류하고 원문 공시와 대조한다.

### 표본 결과 (2025 사업보고서, 상장사 4,019개 중 600개 표본, seed 7)

검증된 회사 323개(공시 없음 150, 수집 실패 127). 출처: `results/summary_2025_n600_s7.json`, `results/triage_2025_n600_s7.json`. 아래 표의 "전"은 규칙 보강 전(`*_before.*`).

| 규칙 | 불일치 건수 전 → 후 | 불일치 회사 수 전 → 후 |
|---|---|---|
| cf_end_vs_bs_cash | 9 → 7 | 7 → 6 |
| cf_net_change_label | 14 → 12 | 10 → 9 |
| cf_bridge | 15 → 15 | 12 → 12 |
| cf_prior_end_is_current_begin | 3 → 3 | 3 → 3 |
| restatement | 120 → 113 (+7 reclassified) | 41 → 34 |
| **전체** | **161 → 150** | **60 → 52** |

규칙 보강 후 남은 비정상 152건(mismatch 150 + rounding 2)의 원인 분류: 재작성·기타 restatement 99, 원 단위 1만 원 이하 반올림·오타급 25, 미분류 27, 매각예정 현금 가설 1.

### 검증 사례 (`results/verified_cases.md`)
- **롯데지주:** 현금흐름표 기말현금이 재무상태표 현금보다 359,586,889원(전기 1,227,929,244원) 큼. 같은 표의 「당기말 매각예정자산집단에 포함된 현금및현금성자산」 줄이 두 금액과 정확히 일치. → `cf_end_vs_bs_cash`가 이 줄을 더해 비교(API 수치로 판정, 원문 미확인).
- **퀀타매트릭스:** '순증감'이 기말현금과 같아 열 밀림으로 의심했으나 회사가 순증감 표준코드를 '기말의 현금및현금성자산' 줄에 잘못 붙인 것. → 이름이 기초/기말인 줄은 순증감으로 읽지 않음(오탐 제거).
- **효성화학:** 전기 매출이 2024 보고서보다 3,859.7억원(13.6%) 적음. 순이익·자산·자본 불변, 2025 보고서 주석 15에 중단영업. → `reclassified`로 표시(7개사, 효성화학만 원문 확인). 중단영업 영업수익 합과 감소액의 금액 대응은 미확인.
- **SK디스커버리:** 전기 자산 +645.9억, 순이익 +151.9억. 주석에 전기오류수정 재작성이 공시되어 있고 조정표 금액이 일치. 규칙이 잡은 진짜 재작성이므로 `mismatch` 유지.
- **삼정펄프:** 규칙 4개가 정확히 1,000원 차이를 보고. 전기 열 기말현금 한 칸이 원인(독립 4건이 아닌 1건). 소액.
- **원인 미확인:** 청담글로벌(기초현금 vs 전기 기말현금 31억), 삼양홀딩스(현금 소액 차이), 메타케어(전기 자산·자본 +11.2억, 순이익 −7.8억).

### 시점별 저장 (`dart_recon/pit.py`)
같은 (회사, 재무제표구분, 계정, 회계연도) 값을 보고서마다 따로 저장한다(`data/pit.sqlite`, 테이블 `pit`). 뷰: `as_first_reported`, `latest`, `restatement_candidates`(최초≠최신). 캐시만 읽으며 `DART_OFFLINE=1`이 필요하다. `dart_history` 도구가 이 로직을 쓴다. 결과(`results/pit_2025_n600_s7_summary.json`): pit 행 217,271, 회사 326, 최초값≠최신값 행 2,067(회사 203).

## 실행 방법

```
# 키: 저장소 루트 .env의 DART_API_KEY(또는 환경변수). .env, data/는 커밋하지 않는다
python3 -m dart_recon.run --year 2025 --sample 600 --seed 7                 # data/raw에 캐시하며 수집
DART_OFFLINE=1 python3 -m dart_recon.run --year 2025 --sample 600 --seed 7  # 캐시만 사용
python3 -m dart_recon.triage --year 2025 --sample 600 --seed 7              # 원인 분류
DART_OFFLINE=1 python3 -m dart_recon.pit --year 2025 --sample 600 --seed 7  # 캐시 -> pit 테이블
python3 -m dart_recon.mcp_server                                            # MCP 서버
python3 -m unittest discover -s tests -t .                                  # 테스트
```

루프 명령(`evalkit`)의 서브커맨드와 순서는 `eval/PROTOCOL.md`에 있다: 케이스 확인(`check-cases`) → 프롬프트 생성(`prompt`) → 분석가가 `evalkit tool`로 도구를 호출해 답변 작성 → `score` → 판정자 → `judge-validate` → 개선자 프롬프트(`improve-prompt`, train 실패만) → 후보 → `gate` → 원장 기록 → `report`.

DART 서버는 연속 요청 시 연결을 끊는다. 클라이언트는 요청을 직렬화하고 최소 1초 간격(`DART_MIN_INTERVAL`)을 둔다. 연결 리셋·빈 응답이 연속 3회면 차단기가 수집 전체를 멈춘다.

## 한계와 다음 단계

- FRED(거시 지표) 미연동.
- RAG·임베딩 없음.
- PDF/DOCX 공시 원문 파싱 없음. 그래서 케이스의 `expected_basis`가 전부 `api_only`다.
- 수집 실패 127건(fetch_error)은 재수집하지 않았고 결과에 없다.
- LLM API 클라이언트 없음(의도). 에이전트 실행은 Claude Code 서브에이전트에 맡긴다.
- 투자 조언이 아니다.
- 평가 한계: 원인 판단을 사람이 확인한 verified 케이스는 11개뿐이고, derived 35개 라벨은 규칙 엔진 출력에서 나와 엔진이 틀리면 라벨도 틀린다. k=3이라 케이스별 분산 추정이 거칠다. 판정자는 rep 1만 본다. 판정자는 모델 1개이고 분석가·판정자·개선자가 같은 모델 계열이다. heldout을 여러 라운드 재사용하면 게이트 결과로 정보가 샌다. `required_facts`·`forbidden`은 사람이 정해 맞는 표현을 놓치거나 정상 표현을 잡을 수 있다.
- `restatement`는 올해와 작년 보고서가 같은 재무제표 구분(연결/별도)일 때만 비교한다. `reclassified`, `held_for_sale_cash` 등은 데이터 패턴에서 나온 가설이며 일부만 원문으로 확인했다.

## 기여
불일치를 찾았거나 판정이 틀렸으면 「불일치 제보」 이슈(`.github/ISSUE_TEMPLATE/data_quality.yml`)로 올리고 근거 구분(원문 확인 / API 수치만)을 적는다. PR은 `.github/PULL_REQUEST_TEMPLATE.md`의 관문을 통과해야 하며 규칙에는 ID·문서·테스트가 모두 있어야 한다(`tests/test_registry.py`). 작업 규칙: `AGENTS.md`, 이슈 처리·테스트 에이전트: `.claude/agents/`.

## 참고자료
- edgartools (github.com/dgunning/edgartools): MCP 도구 구성과 Claude Code 기반 평가(`cc_runner`)를 참고. 이슈 양식·에이전트 운영도 참고.
- FinSkillOps (arXiv 2609.19680): 유형화된 실패 진단 → 좁은 스킬 패치 후보 → 게이트 구조.
- FinVerBench (arXiv 2605.29586), MetaSkill-Evolve (arXiv 2607.05297), AgenticFinBench, FinAuditing: 평가·스킬 개선 설계 참고.
- XBRL US DQC: 규칙 문서 구조. 출처와 라이선스는 `docs/references.md`.

## 라이선스
MIT
