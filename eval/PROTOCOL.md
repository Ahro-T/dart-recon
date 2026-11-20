# 평가 · 자기개선 루프 프로토콜

dart-recon 스킬(`skills/dart-recon/SKILL.<vN>.md`)을 케이스로 평가하고, 게이트를 통과한 후보만 채택하는 루프.
설계는 edgartools `edgar/ai/evaluation/cc_runner.py`(API 없이 서브에이전트가 프롬프트 실행)와 FinSkillOps(arXiv:2609.19680: 유형화된 실패 진단 → 범위가 좁은 스킬 패치 후보 → 게이트, 대부분의 후보는 기각)를 따른다.
이 저장소에는 LLM 호출 코드가 없다. 케이스, 프롬프트 생성기, 도구 호출 기록기, 결정론적 채점기, 스키마 검증기, 게이트, 원장만 있다. 역할은 오케스트레이터가 Claude Code 서브에이전트(`.claude/agents/`)로 실행한다.

## 기준선
`v0`는 의도적으로 약하게 쓴 스킬이다(원인 분류·근거 수준·숫자 인용 지침 없음). 개선 폭을 보이기 위한 출발점이며 완성본이 아니다.

## 케이스 (`eval/cases.jsonl`)
실제 캐시 데이터로 MCP 도구 핸들러를 돌려 만들었다. 필드: `id, question, corp, year, expected_cause, expected_basis, required_facts, forbidden, split, protected`.
`required_facts`는 도구 출력에 그대로 있는 토큰(규칙 ID, 원 단위 금액, 접수번호)이며 `evalkit check-cases`로 확인한다.
`expected_basis`는 전부 `api_only`다. 분석가는 API 수치 도구만 쓰므로 공시 원문을 직접 확인할 수 없다. 사람이 원문으로 확인한 사례(효성화학, SK디스커버리)라도 분석가가 「원문 확인」이라고 쓰면 과장으로 본다.
분할: train 7(보호 3 포함) / heldout 4. 분석가와 개선자는 이 파일을 읽지 않는다.

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

## 채점 (결정론, `evalkit score`)
케이스 점수 = mean(cause_match, facts_recall, basis_ok). 답변에 도구 로그에 없는 금액(4자리 이상 정수, 조/억/만 단위 표기는 표기 정밀도만큼 반올림 허용)이 하나라도 있거나 금지 표현(`forbidden` 정규식)이 걸리면 0점. `basis`가 `unknown`이면 basis_ok=0. 근거 금액 집합은 도구 결과의 모든 정수와 같은 객체 안 정수끼리의 차이. 답변 파일이 없으면 0점.
판정자 출력은 점수에 반영하지 않고 일치도만 본다(`judge-validate`: 판정자의 hallucinated 주장 vs 결정론적 hallucinated_amounts, correctness≥4 vs cause_match).

## 게이트 (`evalkit gate`)
| 게이트 | 조건 |
|---|---|
| G1 targeted | `candidate.json`의 `target_case_ids`(train만 허용) 평균 점수가 base보다 엄격히 높음 |
| G2 protected | 보호 케이스 점수가 하나도 내려가지 않음 |
| G3 hallucination | 후보의 근거 없는 금액이 전체 케이스에서 0건 |
| G4 heldout | 홀드아웃 평균이 base보다 낮지 않음 |
네 개 모두 통과해야 채택: `SKILL.<vM>.md`로 복사하고 `skill use vM`. 기각이면 `lessons/r<round>-<vM>.md`에 실패한 게이트와 변경 요약을 쓴다. 매 판정은 `results/loop/ledger.jsonl`에 한 줄 추가.

## 홀드아웃 격리
개선자 프롬프트에는 train 실패만 들어간다(홀드아웃의 id·질문·점수·답변 모두 제외). 게이트 로그와 `lessons/`에는 홀드아웃 평균만 남는다. 분석가·개선자는 `eval/cases.jsonl`을 읽지 않는다(에이전트 지침으로 강제할 뿐 파일 권한으로 막지는 못한다).

## 산출물
`results/loop/runs/r<round>/<vN>/{<case>.answer.json, <case>.tools.jsonl, scores.json, judge.json, agreement.json}`, `results/loop/runs/r<round>/candidate.json`, `results/loop/ledger.jsonl`, `results/loop/summary.{json,md}`(`evalkit report`). 작은 JSON이라 커밋한다.

## 한계
- 케이스 11개(train 7 / heldout 4). 케이스 하나가 평균을 크게 움직이므로 점수 차이로 통계적 결론을 내릴 수 없다.
- 판정자는 모델 1개이고 분석가·판정자·개선자가 같은 모델 계열이다. 공통된 편향은 잡지 못한다.
- 홀드아웃을 여러 라운드 재사용하면 게이트 통과 여부를 통해 정보가 샌다.
- required_facts·forbidden은 사람이 정한 것이라 맞는 표현을 놓치거나 정상 표현을 잡을 수 있다. 금액 근거 검사는 도구 출력에 나온 숫자와 같은 객체 안 차이만 인정한다(합계 등 직접 계산한 값은 hallucinated로 표시된다).

## 검증기 수정 기록
- 2라운드 게이트 전: `samjung-pulp-typo`의 금지 표현 `재작성|중단영업`이 "재작성 아님", "중단영업 등 … 항목이 없고"처럼 원인을 배제하는 문장까지 잡아, 원인을 맞힌 답도 0점이 됐다(r0~r2 v0 답변 3개, r0 v1, r1 v2). 금지 표현을 원인으로 단정하는 문형(`(정정|재작성|중단영업)\s*(으로\s*(보|판단|추정)|에\s*따른|이\s*원인|때문)`)으로 좁혔다. 원인 오분류는 `cause_match`가 따로 잡는다.
- 수정 후 r0~r2의 모든 `scores.json`을 같은 답변 파일로 다시 채점했고(답변은 그대로), r0·r1 게이트를 다시 돌렸다. 수정 전 원장은 `results/loop/ledger.pre-verifier-fix.jsonl`에 남겼다. 채택/기각 결정은 바뀌지 않았고, r1의 실패 게이트 목록만 `G1,G2,G3,G4` → `G2,G3,G4`로 바뀌었다.
- r1 개선자는 수정 전 문구의 r0 교훈을 보고 후보를 만들었다.
