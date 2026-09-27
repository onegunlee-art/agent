# AI Company OS V0.4 — 챗봇 생산 라인 v1

V0.4는 세 번의 V0.2 생산 사이클에서 반복 검증된 절차만
`lines/chatbot/line_manifest.json`에 등록한다. 자동 생성된 적대 질문과 새 절차는
승인된 스킬이 아니라 DRAFT 또는 `candidates/` 상태로 시작한다.

## 생산 흐름

1. `CUSTOMER_INPUT_SCHEMA.json` 형식으로 공개 FAQ, 금지 표식, 거절·가격·부정
   질문을 받는다.
2. `draft_adversarial_evaluation`이 공개 사실, 자료 밖 질문, 가격 미상, 비밀정보,
   타 고객 혼입, 부정 질문 사례를 DRAFT로 만든다. 자동 생성은 승인이 아니다.
3. 빌더가 실패 수용 테스트와 고객 폴더를 커밋한다.
4. 실행자는 최소 sparse 범위에서 제품 파일 한 개와 테스트 node 한 개만 다룬다.
5. 집중 테스트와 기존 전체 평가를 모두 통과한 결과만 납품 후보가 된다.

## 스킬 승격 관문

`company skill evaluate`는 후보 manifest가 선언한 기존 평가 경로를 전부 실행하고
candidate tree 해시와 결과 파일 해시를 Event에 기록한다. PASS 뒤 CEO가
후보 해시·평가 Event에 정확히 결속된 승인문을 파일로 남기고 `company skill
approve`로 승인 Event를 추가해야 `company skill promote`가 원자적으로
`lines/chatbot/skills/`에 복사할 수 있다. 후보 바이트가 바뀌면 이전 평가와 승인은
무효다.

## Backlog

- B15: 현황판의 승인 POST는 현재 단일 신뢰 사용자·localhost 전제다. V0.6 원격
  지시 전에 1회용 토큰 또는 사람 확인 단계를 추가한다.
- 실행 시점 diff 자동 캡처는 독립 사례 세 번을 채울 때까지 candidates 상태를
  유지한다.
