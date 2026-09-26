# AI Company OS V0.3 로드맵

## 현재 관문

V0.2 동결 커밋 `6b90ad7`은 누적 독립 검수 PASS를 받았고, 판정서
SHA-256은
`2775b8188bde36da9f4330858dd1bb18b97c44378aec968b0e8ba4b3f18752ab`이다.
CEO 승인 뒤 로컬 `main`의 병합 커밋 `077d8e2`로 반영됐다. 원격 push는
승인되지 않았다. V0.3 개발은 V0.2 동결 커밋에서 분기한
`codex/v0.3-model-run-leases`에서 진행하며 과거 V0.2 Evidence 바이트를
변경하지 않는다.

## R2 — 실제 모델 실행 lease 통합

첫 bounded change의 완료 기준은 다음과 같다.

- `company work model-run`이 모델 프로세스 시작 전에 WorkOrder를 atomically
  claim한다.
- 모델 프로세스는 SQLite 쓰기 트랜잭션 밖에서 실행된다.
- 생산 Run과 `MODEL_EXECUTION` Evidence에 `execution_id`와 fence token이
  기록되고 `diagnostic_only=false`이다.
- 만료 claim 회수 뒤 늦은 결과는 Run/Evidence를 만들지 못한다.
- 실행자 예외는 claim에 결속된 실패 Run으로 남고 새 idempotency key로
  재시도할 수 있다.
- 기존 V0.2의 6개 `diagnostic_only=true` Run은 수정하지 않는다.

## 다음 관문

R2 검수 뒤에만 병렬 실행 수, 고객별 격리, 비용 지표를 확장한다. 비용 지표는
채택 Run 토큰이 아니라 요청 한 건에 사용된 실패·초과·채택 Run 전체 토큰과
시간을 기본 원가로 삼는다. 외부 발송·게시·결제는 계속 실행자 권한 밖에 둔다.

결정형 FAQ의 부정 표현 오매칭(B10)은 R2와 섞지 않는다. 실제 고객 라인을
시작하기 전에 “케이크 말고 빵 있어요?” 같은 부정 질문을 평가 사례에 넣고,
단순 부분 문자열 검색을 통과시키지 않는 별도 WorkOrder로 다룬다.
