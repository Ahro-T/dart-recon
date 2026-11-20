# 개선 루프 요약

| round | skill | train mean (n) | heldout mean (n) | hallucinated | 결과 |
|---|---|---|---|---|---|
| 0 | v0 | 0.8333 (7) | 0.8611 (4) | 0 | baseline |
| 0 | v1 | 0.8095 (7) | 1.0 (4) | 1 | rejected: G1_targeted,G3_hallucination |
| 1 | v0 | 0.8492 (7) | 0.8611 (4) | 0 | baseline |
| 1 | v2 | 0.9286 (7) | 0.7083 (4) | 1 | rejected: G2_protected,G3_hallucination,G4_heldout |
| 2 | v0 | 0.8651 (7) | 0.9305 (4) | 0 | baseline |
| 2 | v3 | 0.881 (7) | 0.8611 (4) | 0 | rejected: G2_protected,G4_heldout |

후보 3개 중 채택 0, 기각 3.

한계: 케이스 약 10개, 판정자 모델 1개, 모든 역할이 같은 모델 계열. 평균 차이는 케이스 1개 단위로 크게 움직이므로 통계적 유의성을 주장하지 않는다. 같은 케이스를 여러 라운드 재사용하면 홀드아웃도 점차 오염된다.
