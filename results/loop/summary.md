# 개선 루프 요약

| round | skill | k | 케이스 | legacy | train mean (n) | heldout mean (n) | hallucinated | 결과 |
|---|---|---|---|---|---|---|---|---|
| 0 | v0 | 1 | 11 | legacy | 0.8333 (7) | 0.8611 (4) | 0 | baseline |
| 0 | v1 | 1 | 11 | legacy | 0.8095 (7) | 1.0 (4) | 1 | rejected: G1_targeted,G3_hallucination |
| 1 | v0 | 1 | 11 | legacy | 0.8492 (7) | 0.8611 (4) | 0 | baseline |
| 1 | v2 | 1 | 11 | legacy | 0.9286 (7) | 0.7083 (4) | 1 | rejected: G2_protected,G3_hallucination,G4_heldout |
| 2 | v0 | 1 | 11 | legacy | 0.8651 (7) | 0.9305 (4) | 0 | baseline |
| 2 | v3 | 1 | 11 | legacy | 0.881 (7) | 0.8611 (4) | 0 | rejected: G2_protected,G4_heldout |
| 3 | v0 | 3 | 46 |  | 0.9213 (20) | 0.9431 (14) | 0 | baseline |
| 3 | v4 | 3 | 34 |  | 0.9324 (20) | 0.9524 (14) | 0 | rejected: G1_targeted |

후보 4개 중 채택 0, 기각 4.

legacy 행은 k 디렉터리 없이 1회만 실행한 11케이스 결과(구 게이트)다. 한계: 판정자 모델 1개, 모든 역할이 같은 모델 계열. 게이트의 1-SE 규칙은 유의성 검정이 아니라 잡음 띠 밖의 차이만 인정하는 경험 규칙이다. 같은 케이스를 여러 라운드 재사용하면 홀드아웃도 점차 오염된다.
