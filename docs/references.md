# 참고한 프로젝트

| 프로젝트 | 라이선스 | 가져온 것 | 방식 |
|---|---|---|---|
| [XBRL US DQC 규칙](https://github.com/DataQualityCommittee/dqc_us_rules) | XBRL US 문서 라이선스(원문 수정 금지) | 규칙마다 고정 ID·문서·테스트를 두고, 규칙 문서에 검사·근거·해결하는 문제·변경 이력을 적는 구조 | **구조만 참고.** 문서 내용은 복사하지 않았다 |
| [edgartools](https://github.com/dgunning/edgartools) | MIT | 데이터 품질 이슈 양식, 이슈 처리·테스트 에이전트 역할 파일의 구조와 원칙 | 한국어로 줄여 고쳐 씀. 출처를 파일 머리에 적고 라이선스 사본을 `THIRD_PARTY_edgartools_LICENSE.txt`에 둠 |
| [sec-edgar-mcp](https://github.com/stefanoamorelli/sec-edgar-mcp) | AGPL-3.0 | 응답마다 원문 공시 링크, 평가를 CI에서 자동 실행 | 아이디어만. 코드는 가져오지 않는다(라이선스 다름) |
| [dart-fss](https://github.com/josw123/dart-fss) | MIT | DART 수집·XBRL·감사의견 API | 다음 층(감사의견 신호)에서 의존성 후보 |
| [OpenDartReader](https://github.com/FinanceData/OpenDartReader) | MIT | DART API 래퍼, AI용 CLI | 수집 대체 후보 |
| [Arelle](https://github.com/Arelle/Arelle) | Apache-2.0 | XBRL 검증기. SEC가 EDGAR 검증 플러그인을 직접 관리 | 참고. XBRL 원문 검증 층에서 후보 |
