# 평가 · 자기개선 루프 프로토콜

dart-recon 스킬(`skills/dart-recon/SKILL.<vN>.md`)을 케이스로 평가하고, 게이트를 통과한 후보만 채택하는 루프.
설계는 edgartools `edgar/ai/evaluation/cc_runner.py`(API 없이 서브에이전트가 프롬프트 실행)와 FinSkillOps(arXiv:2609.19680: 유형화된 실패 진단 → 범위가 좁은 스킬 패치 후보 → 게이트, 대부분의 후보는 기각)를 따른다.
이 저장소에는 LLM 호출 코드가 없다. 케이스, 프롬프트 생성기, 도구 호출 기록기, 결정론적 채점기, 스키마 검증기, 게이트, 원장만 있다. 역할은 오케스트레이터가 Claude Code 서브에이전트(`.claude/agents/`)로 실행한다.

## 기준선
`v0`는 의도적으로 약하게 쓴 스킬이다(원인 분류·근거 수준·숫자 인용 지침 없음). 개선 폭을 보이기 위한 출발점이며 완성본이 아니다.

## 케이스 (`eval/cases.jsonl`)
필드: `id, question, corp, year, expected_cause, expected_basis, required_facts, forbidden, split, protected, tier`, 선택 필드 `accepted_causes`.
`required_facts`는 도구 출력에 그대로 있는 토큰(규칙 ID, 원 단위 금액, 접수번호)이며 `evalkit check-cases`가 스키마와 함께 확인한다.
`expected_basis`는 전부 `api_only`다. 분석가는 API 수치 도구만 쓰므로 공시 원문을 직접 확인할 수 없다. 사람이 원문으로 확인한 사례(효성화학, SK디스커버리)라도 분석가가 「원문 확인」이라고 쓰면 과장으로 본다.
`accepted_causes`가 있으면 답변의 `cause`가 그 목록에 들면 cause_match=1, 없으면 `expected_cause`와 같아야 한다.
분석가와 개선자는 이 파일을 읽지 않는다. 다만 `--case ID`와 경로에 id가 드러나므로 id에 정답 단서를 넣지 않는다(derived는 `d-<corp_code>`, verified는 `v-<corp_code>`).

### tier
| tier | 뜻 |
|---|---|
| `verified` | 사람이 공시 원문·도구 출력으로 확인한 11개. 질문·정답·분할을 바꾸지 않는다. train 7(보호 3 포함) / heldout 4 |
| `derived` | `dart_recon/casegen.py`가 규칙 엔진·triage 출력에서 결정적으로 만든 케이스(`python3 -m dart_recon.casegen --seed 7 --out eval/cases.jsonl`, 같은 seed·같은 캐시면 같은 파일). 현재 35개 |

derived 라벨 규칙(현재 캐시의 `dart_check`/`dart_explain` 출력 기준, 회사 단위):
- 모든 규칙이 ok(전년도 공시가 있어 재작성 검사 포함) → `none`.
- triage 범주가 `rounding_typo`뿐 → `rounding_or_typo`.
- restatement 상태가 `reclassified`뿐 → `discontinued_reclass`.
- triage 범주가 `restatement`뿐 → `restatement_disclosed`, `accepted_causes`는 `[restatement_disclosed, unexplained]`.
- triage 범주가 `unclassified`뿐 → `unexplained`.
- 범주가 섞였거나, 누락(missing) 규칙이 있거나, 라벨이 없는 범주(held_for_sale_cash, column_shift, restatement_discontinued)인 회사, 이름이 겹쳐 조회가 모호한 회사, verified 회사는 제외. 범주별 상한(8/7/6/8/8)을 두어 범주 균형을 맞춘다.
- 질문은 정답을 드러내지 않는 일반 템플릿 3종 중 `corp_code`로 고른다(범주와 무관). `forbidden`은 공통 패턴 + `none`에는 불일치 단정 문형.
- 분할은 범주별 층화로 train / heldout / test 약 40/30/30. 보호(protected) 케이스는 verified에만 있다.

derived 라벨의 한계: 사람이 공시 원문을 확인하지 않았다. 도구(규칙 엔진·triage 가설)가 낸 결과를 정답으로 삼으므로 도구의 오류는 정답에도 들어 있다. `restatement` 범주는 API 수치만으로 공시된 재작성인지 확정할 수 없어 `unexplained`도 정답으로 인정한다. `none`은 "검사한 규칙에서 불일치 없음"일 뿐 공시가 옳다는 뜻이 아니다. 따라서 derived 점수는 "도구 출력을 정확히 옮기고 과장하지 않는가"의 척도이며, 원인 판단의 정확도는 verified로 본다.

## 원인 분류 (`expected_cause` / 답변의 `cause`)
| 라벨 | 뜻 |
|---|---|
| none | 정합 — 불일치 없음(오탐 함정 포함) |
| restatement_disclosed | 회사가 주석으로 공시한 재작성으로 전기 숫자가 달라짐 |
| discontinued_reclass | 중단영업 재분류로 전기 매출 등이 계속영업에서 빠짐 |
| held_for_sale_cash | 현금흐름표 기말현금에 매각예정자산집단 현금이 포함됨 |
| mistagged_account | 회사의 계정 표준코드 오태깅으로 생긴 겉보기 불일치 |
| column_shift_small | 열 밀림 등 표 구조로 한 칸이 어긋남 |
| rounding_or_typo | 반올림 또는 오타 수준의 소액 차이 |
| unexplained | 불일치가 있으나 원인을 확인할 수 없음 |

현재 케이스는 none, discontinued_reclass, restatement_disclosed, rounding_or_typo, unexplained만 쓴다. 나머지 라벨은 분류 체계의 여지(보호 케이스의 롯데지주·퀀타매트릭스는 규칙 보강 후 도구가 ok를 내므로 정답이 none).

## 역할
| 역할 | 에이전트 | 하는 일 |
|---|---|---|
| 분석가 | `dart-analyst` | `evalkit prompt` 출력을 받아 `evalkit tool`로만 도구를 호출하고 `<case>.answer.json`을 쓴다 |
| 판정자 | `dart-judge` | 답변을 도구 로그와 대조해 `judge.json`(`eval/judge_schema.json`)을 쓴다. 읽기 전용 |
| 개선자 | `dart-improver` | `evalkit improve-prompt` 출력(train 실패만)으로 후보 스킬 1개와 `candidate.json`을 쓴다 |

## 실행 구조 (반복 k회)
같은 스킬·같은 케이스를 k번 독립 실행한다(분석가 서브에이전트를 케이스×rep마다 새로 띄운다). `evalkit prompt`와 `evalkit tool`은 `--rep J`(1 이상)를 받아 `results/loop/runs/r<R>/<vN>/k<J>/`에 답변과 도구 로그를 쓴다. 후보 비교는 기준선과 후보 모두 같은 라운드에서 같은 k로 돌린 결과만 쓴다.
판정자(`judge-prompt`/`judge-validate`)는 비용 때문에 한 rep(기본 `--rep 1`)만 본다. `judge.json`·`agreement.json`은 그 rep 디렉터리에 둔다.
`evalkit noise --round R --skill vN`은 rep 간 변동(분할별 rep별 평균과 그 표본표준편차, 케이스별 sd 상위, SE)을 `runs/rR/vN/noise.json`에 저장한다. 문서에 "잡음 크기"를 쓸 때의 근거다.

## 채점 (결정론, `evalkit score`)
`score`는 존재하는 `k*` 디렉터리를 모두 채점해 케이스별 `reps`(rep별 점수와 세부), `score`(rep 평균), `sd`(표본표준편차, k<2면 null), `k`를 `scores.json`에 쓴다. 한 rep에 답변이 없으면 그 rep는 0점이다. 집계(n, mean_score, cause_match, facts_recall, basis_ok, forbidden_hits)는 rep 평균 기준, `hallucinated_amounts`는 전 rep 합, `se`는 아래와 같다.
기본은 train+heldout만 채점한다(`--splits`로 지정, 예: `--splits test`). 채점한 분할은 `scores.json`의 `splits`에 기록되고, 다른 분할로 다시 채점해도 이미 채점된 분할은 보존된다(test 집계는 채점했을 때만 생긴다). `k*` 디렉터리가 없는 run은 에러로 거부한다(레거시 결과 덮어쓰기 방지).

rep 하나의 케이스 점수 = mean(cause_match, facts_recall, basis_ok). 답변에 도구 로그에 없는 금액(4자리 이상 정수, 조/억/만 단위 표기는 표기 정밀도만큼 반올림 허용)이 하나라도 있거나 금지 표현(`forbidden` 정규식)이 걸리면 0점. `basis`가 `unknown`이면 basis_ok=0. 근거 금액 집합은 도구 결과의 모든 정수와 같은 객체 안 정수끼리의 차이. 답변 파일이 없으면 0점.
판정자 출력은 점수에 반영하지 않고 일치도만 본다(`judge-validate`: 판정자의 hallucinated 주장 vs 결정론적 hallucinated_amounts, correctness≥4 vs cause_match).

## 게이트 (`evalkit gate --round R --base vB --cand vC`)
짝지은 비교: 같은 라운드·같은 케이스 집합·같은 `splits`여야 하고, 게이트는 train과 heldout 케이스만 본다(test는 무시). 케이스 c마다
- `d_c = mean(cand reps) − mean(base reps)`
- `se_c = sqrt(var_b/k_b + var_c/k_c)` (표본분산)

집합 S에 대해 `D_S = mean(d_c)`, `SE_S = sqrt(Σ se_c²) / |S|`.

| 게이트 | 조건 |
|---|---|
| G1 targeted | `candidate.json`의 `target_case_ids`(train만 허용)에서 `D > SE` |
| G2 protected | 보호 케이스마다 `d_c ≥ −se_c`. `se_c = 0`이면 `d_c ≥ 0` |
| G3 hallucination | 후보의 근거 없는 금액이 train·heldout 전 rep에서 0건 |
| G4 heldout | heldout 전체에서 `D ≥ −SE` |

base나 cand의 k가 2 미만이면 분산을 모르므로 게이트를 판정하지 않고 종료한다. 네 개 모두 통과해야 채택: `SKILL.<vM>.md`로 복사하고 `skill use vM`. 기각이면 `lessons/r<round>-<vM>.md`에 실패한 게이트와 변경 요약을 쓴다. 매 판정은 `results/loop/ledger.jsonl`에 한 줄 추가하며 각 게이트의 D, SE, n, k를 남긴다.

1-SE 규칙의 의미와 한계: 유의성 검정이 아니라 "rep 간 변동(잡음 띠) 밖의 차이만 개선으로 인정한다"는 경험 규칙이다. 케이스 수가 적고 k가 작으면 분산 추정 자체가 불안정하고(k=3이면 자유도 2), 케이스·게이트를 여러 개 동시에 보므로 우연히 통과할 확률을 통제하지 못한다. SE는 케이스 간 독립을 가정한다. 통과는 "잡음보다 커 보인다"는 뜻이지 개선이 입증됐다는 뜻이 아니다.

개선자 프롬프트(`improve-prompt`)의 train 실패는 rep 평균 점수 < 1.0인 케이스다. 케이스마다 rep별 점수를 보여 주고 답변 파일은 가장 낮은 rep의 경로를 준다.

## 홀드아웃 격리
개선자 프롬프트에는 train 실패만 들어간다(홀드아웃·test의 id·질문·점수·답변 모두 제외). 게이트 로그와 `lessons/`에는 홀드아웃 평균만 남는다. 분석가·개선자는 `eval/cases.jsonl`을 읽지 않는다(에이전트 지침으로 강제할 뿐 파일 권한으로 막지는 못한다).

## test 분할 운용
`test`는 개선 루프(게이트·개선자·judge 일치도 점검)에 쓰지 않는다. 루프가 끝난 뒤 최종 스킬(과 기준선)에 대해 `score --splits test`로 한 번만 평가한다. test 결과를 보고 스킬을 다시 고치면 test도 오염되므로 그 경우 결과를 그대로 보고하고 test를 다시 만들어야 한다.

## 레거시 r0~r2
r0~r2는 이 구조 이전에 11개 케이스(verified)를 1회씩만 돌린 결과다. `k*` 디렉터리 없이 `runs/r<R>/<vN>/` 바로 아래에 답변·도구 로그·`scores.json`이 있고 게이트는 구 규칙(평균 비교, 잡음 무시)으로 판정됐다. 이동하거나 다시 채점하지 않는다. 같은 v0의 train 평균이 0.8333~0.8651(n=7), heldout 평균이 0.8611~0.9305(n=4)로 흔들렸고 후보와 기준선의 차이가 그 정도 크기였다는 것이 반복 실행을 도입한 이유다. `evalkit report`는 레거시 `scores.json`을 k=1, `legacy` 표시로 읽고, 원장의 구 게이트 기록(D/SE 없음)도 읽는다. 새 round는 r3 이후 번호를 쓴다.

## 산출물
`results/loop/runs/r<round>/<vN>/k<J>/{<case>.answer.json, <case>.tools.jsonl}`(J=1에만 `judge.json`, `agreement.json`), `results/loop/runs/r<round>/<vN>/{scores.json, noise.json}`, `results/loop/runs/r<round>/candidate.json`, `results/loop/ledger.jsonl`, `results/loop/summary.{json,md}`(`evalkit report`). 작은 JSON이라 커밋한다.

## 한계
- 케이스 46개(verified 11 + derived 35), derived 라벨은 도구 출력에서 도출한 것이다. k가 작아(예: 3) 분산 추정이 거칠고 같은 케이스·같은 라운드를 반복해 봐도 케이스 구성에서 오는 변동은 줄지 않는다.
- 판정자는 모델 1개이고 분석가·판정자·개선자가 같은 모델 계열이다. 공통된 편향은 잡지 못한다.
- 홀드아웃을 여러 라운드 재사용하면 게이트 통과 여부를 통해 정보가 샌다.
- required_facts·forbidden은 사람이 정한 것이라 맞는 표현을 놓치거나 정상 표현을 잡을 수 있다. 금액 근거 검사는 도구 출력에 나온 숫자와 같은 객체 안 차이만 인정한다(합계 등 직접 계산한 값은 hallucinated로 표시된다).

## 검증기 수정 기록
- 2라운드 게이트 전: `samjung-pulp-typo`의 금지 표현 `재작성|중단영업`이 "재작성 아님", "중단영업 등 … 항목이 없고"처럼 원인을 배제하는 문장까지 잡아, 원인을 맞힌 답도 0점이 됐다(r0~r2 v0 답변 3개, r0 v1, r1 v2). 금지 표현을 원인으로 단정하는 문형(`(정정|재작성|중단영업)\s*(으로\s*(보|판단|추정)|에\s*따른|이\s*원인|때문)`)으로 좁혔다. 원인 오분류는 `cause_match`가 따로 잡는다.
- 수정 후 r0~r2의 모든 `scores.json`을 같은 답변 파일로 다시 채점했고(답변은 그대로), r0·r1 게이트를 다시 돌렸다. 수정 전 원장은 `results/loop/ledger.pre-verifier-fix.jsonl`에 남겼다. 채택/기각 결정은 바뀌지 않았고, r1의 실패 게이트 목록만 `G1,G2,G3,G4` → `G2,G3,G4`로 바뀌었다.
- r1 개선자는 수정 전 문구의 r0 교훈을 보고 후보를 만들었다.
- r3 이전(케이스 확대 시): verified 11개의 id가 `celltrion-clean`, `sk-discovery-restate`처럼 정답을 드러내고 있어 `v-<corp_code>`로 바꿨다. 대응: lotte-holdings-hfs=v-00120562, quantamatrix-tag=v-01311286, hyosung-chem-disc=v-01316236, sk-discovery-restate=v-00131832, samjung-pulp-typo=v-00128227, cheongdam-unexplained=v-01534577, celltrion-clean=v-00413046, metacare-unexplained=v-00791209, samyang-holdings-unexplained=v-00126937, viewworks-clean=v-00609324, crown-haitai-clean=v-00153126. 레거시 r0~r2 파일은 옛 id 그대로 둔다.
- 같은 시점: verified `none` 케이스의 금지 표현 `(불일치|오류|문제)(가|는|를)?\s*(있습니다|발견|존재|확인됩니다)`가 "오류는 발견되지 않았습니다"도 잡았다. derived와 같은 단정 문형(`발견(됩니다|되었습니다|했습니다)|존재합니다`)으로 좁혔다. 레거시 r0~r2는 다시 채점하지 않았다.
- r3 게이트 후: 금액 근거 검사가 거짓 양성 두 종류를 냈다. (1) `dart_explain` 가설 설명의 "차이가 1만 원 이하"를 답변이 그대로 인용해도, 도구 문자열 속에서는 4자리 이상 숫자만 근거로 인정해 "1만"을 근거 없는 금액으로 잡았다. (2) "전기 75000000만 있고"의 조사 '만'을 단위로 읽어 7,500억 원으로 해석했다. r3 v0·v4 3회 반복 전체에서 잡힌 근거 없는 금액 10건(v0 7건, v4 3건)이 모두 이 두 경우였다. 도구 문자열 속 단위 표기도 근거로 인정하고, 다섯 자리 이상 정수 바로 뒤의 '만'은 조사로 읽도록 고쳤다(테스트 추가).
- 수정 전 r3 결과: v0 train 0.8583(se 0.0164, n=20)·heldout 0.8743(se 0.0331, n=14), v4 train 0.8991·heldout 0.9286, 게이트 실패 `G3_hallucination`. 수정 전 원장 줄은 `results/loop/ledger.r3-pre-verifier-fix.jsonl`에 남겼다. 같은 답변 파일로 v0·v4를 다시 채점하고 게이트를 다시 돌렸다(수정 후: v0 train 0.9213(se 0.0072)·heldout 0.9431(se 0.0093), v4 train 0.9324·heldout 0.9524, 게이트 실패 `G1_targeted`). 판정자(rep 1)는 수정 전에도 v0의 이 4건을 근거 없는 금액으로 보지 않았다(환각 일치 30/34 → 수정 후 34/34).
- v4 후보는 수정 전 점수를 보고 만들어졌다. 대상 케이스 2개(`d-00531865`, `v-00128227`)의 0점은 대부분 이 거짓 양성 때문이어서, 후보가 겨냥한 실패 일부는 실제 분석 오류가 아니었다.
