# V0.2 코딩 지시서 (Cursor용)

> 목적: 실제 모델이 합성 고객의 챗봇 수정 요청 1건을 수행하고, 코드·자동 시험·품질 평가·브라우저 확인·독립 검수·SQLite 기록을 모두 통과하는 **첫 생산 사이클**을 완성한다.
> 이 폴더(`v02-kit/`)를 저장소의 `reference/v02/`에 복사한 뒤, 아래 WorkOrder 블록을 순서대로 Cursor에 붙여넣는다.

## 0. 이 킷의 구성

| 파일 | 역할 | 대응 WorkOrder |
|---|---|---|
| `reference/execution_lease.py` | 청구·확정·만료 회수·fence token·stop 경쟁 차단 | A, B |
| `reference/rubric_verifier.py` | 항목별 채점 검증기 + 임계값 + Evidence 해시 | C |
| `reference/synthetic_faq_bot/` | 결정적 합성 FAQ 챗봇, 평가 사례 10건, 평가 실행기, 127.0.0.1 미리보기 | D |
| `reference/model_executor.py` | 코딩 에이전트 CLI를 워크트리 안에서 제한 실행하는 ExecutorPort 어댑터 | E |
| `reference/db_backup.py` | SQLite 온라인 백업·해시·복원 검증 | G |
| `tests/` | 위 모듈의 **동작을 규정하는** 수용 테스트 29개 (전부 통과 상태) | 전체 |

참조 코드는 그대로 붙이는 게 아니라 기존 `application.py`의 WorkOrder / Run / Event / Evidence / ReviewRequest에 **이식**한다. 이름과 표는 바꿔도 되지만, `tests/`가 규정하는 동작은 바꾸지 않는다. 테스트가 먼저 옮겨지고, 그다음 구현이 따라간다.

## 1. 코딩 원칙 (모든 WorkOrder 공통)

1. **실패하는 수용 테스트를 먼저** 쓴다. 테스트 없이 구현부터 시작하지 않는다.
2. **트랜잭션은 짧게, 실행은 트랜잭션 밖에서.** 쓰기 트랜잭션은 `BEGIN IMMEDIATE`로 열고 몇 ms 안에 닫는다. 모델 호출·테스트 실행·파일 작업 중에는 절대 트랜잭션이 열려 있지 않다 (`test_no_transaction_is_open_while_executor_runs`).
3. **fence token 없는 확정은 없다.** 결과를 원장에 쓰는 모든 경로는 현재 청구의 `execution_id`와 `fence_token`을 확인한다. 불일치면 원장은 손대지 않고 `STALE_RESULT_REJECTED` 이벤트만 남긴다.
4. **exact-text는 합성 fixture 전용.** 실제 모델 결과물은 항목별 채점(`rubric_verifier`)으로만 판정한다.
5. **실행자는 워크트리 밖을 모른다.** WorkOrder마다 `git worktree` + 브랜치. 실행자 cwd는 그 안. 외부 발송·게시·결제·패키지 설치·git commit은 실행자가 아니라 OS가 한다.
6. **외부 부작용은 자동 재시도하지 않는다.** `side_effect_class=EXTERNAL`이 만료·실패하면 `MANUAL_RECOVERY_REQUIRED`로 멈춘다.
7. **모든 상태 변화는 Event로, 모든 판정은 Evidence로.** 이벤트는 추가 전용. 검증 리포트에는 `evidence_sha256`이 붙는다.
8. **한 WorkOrder = 한 브랜치 = 한 PR.** `main` 머지는 CEO만. 에이전트는 push·merge를 하지 않는다.
9. **범위 밖 발견은 끼워 넣지 않는다.** 새 결함을 찾으면 `docs/ROADMAP_V0.2_KO.md`의 "관문" 절에 기록하고 다음 WorkOrder로 보낸다. 첫 실행을 위험하게 만드는 결함만 예외.

## 2. 순서와 병행

```
트랙 1 (실행 기반):   0 → A → B → E
트랙 2 (평가·산출물): C → D                  ← 트랙 1과 병행, 별도 브랜치, executor·원장 코드 수정 금지
합류:                  E 완료 후 → F(최소 브라우저 확인) → 첫 생산 사이클 실행 → G(DB 안전성)
```

- C·D는 실행 코드를 건드리지 않으므로 A·B와 동시에 진행한다. 브랜치를 분리하면 충돌하지 않는다.
- G는 합성 데이터만 쓰는 첫 사이클을 막지 않는다. 다만 **첫 실제 고객 전** 관문이다.
- 관문 동결: 첫 모델 실행 전 필수 = A, B, C, D, E. 그 외는 추가하지 않는다.

## 3. WorkOrder 지시문 (붙여넣기)

### WorkOrder 0 — 기준점 독립 검증

```
목표: d7747b5 기준 보고 내용과 실제 저장소 상태가 일치하는지 검증
범위: 읽기 전용. 코드 수정 없음
완료 기준:
- 전체 테스트를 직접 재실행하고 실제 통과 수를 보고한다 (보고서의 210 passed와 비교)
- prepare_review()가 verifier 정의 파일 해시를 재검사하지 않는다는 결함을 코드 줄 번호로 확인한다
- 정지 확인과 실행 청구 트랜잭션 사이의 경쟁 구간을 코드 줄 번호로 확인한다
- .gitignore에 DB 관련 패턴이 있는지, canonical DB 기본 경로가 어디인지 보고한다
- reference/v02/tests 를 현재 저장소에서 실행해 29개 통과를 확인한다
허용: 읽기, 테스트 실행
금지: 파일 수정, 커밋, push, merge
```

### WorkOrder A — 기존 정확성 결함 수정

```
목표: 검수 시 verifier 무결성, stop 경쟁 조건, 실패 Run 누락의 세 결함을 닫는다
범위: application.py의 prepare_review / 실행 청구 경로 / 실행 실패 경로, 관련 테스트
완료 기준 (실패 테스트를 먼저 작성):
- prepare_review()는 ReviewRequest 생성 직전에 verifier 정의 파일을 다시 해시하고,
  승인 시점 해시와 다르면 VERIFIER_TAMPERED 이벤트를 남기고 ReviewRequest를 만들지 않는다
- company stop 상태 확인이 실행 청구와 같은 BEGIN IMMEDIATE 트랜잭션 안에서 일어난다
  · stop이 먼저 커밋 → 청구 거부 (reference: test_stop_committed_first_rejects_claim)
  · 청구가 먼저 커밋 → 실행 계속 가능 (reference: test_claim_committed_first_can_finish_after_stop)
- ExistingArtifactExecutor의 산출물 누락이 정식 Run(outcome=MISSING_ARTIFACT)과 Event 양쪽에 남는다
  (reference: test_executor_exception_is_recorded_as_run_and_state_restored)
- 서로 다른 WorkOrder 두 건이 동시에 EXECUTING이 될 수 있음을 테스트로 증명한다
- 기존 전체 테스트 회귀 없음
허용: 브랜치 작업, 로컬 수정, 로컬 커밋
금지: push, merge, 스키마 대규모 변경, B의 lease 필드 선행 도입
참조: reference/v02/reference/execution_lease.py 의 claim()/immediate(), tests/test_execution_lease.py
```

### WorkOrder B — 실행 lease와 만료 회수

```
목표: 실행 청구에 lease·fence token·시간·비용 상한을 붙이고, 만료 청구를 안전하게 회수한다
범위: WorkOrder/Run 스키마 확장(마이그레이션 포함), 청구·확정·회수 코드, 관련 테스트
완료 기준 (reference/v02/tests/test_execution_lease.py 10개를 기존 스키마에 맞게 옮겨 전부 통과):
- WorkOrder에 execution_id, fence_token, lease_expires_at, time_limit_seconds, cost_limit_usd,
  side_effect_class(WORKSPACE_ONLY|EXTERNAL) 이 있다
- 청구마다 fence_token이 1 증가하고, 확정 시 execution_id·fence_token 불일치는
  StaleExecution으로 거부되며 원장은 변하지 않는다
- 같은 lease로 두 번 확정할 수 없다
- lease 만료 후 도착한 결과는 outcome=EXPIRED로 기록되고 산출물은 확정되지 않는다
- 프로세스 시작 시 reclaim_expired()가 만료 청구를 회수한다:
  WORKSPACE_ONLY → READY, EXTERNAL → MANUAL_RECOVERY_REQUIRED
- cost_usd > cost_limit_usd 이면 outcome=COST_LIMIT_EXCEEDED, status=FAILED
- 실행 중에는 트랜잭션이 열려 있지 않다 (conn.in_transaction == False 를 실행자 안에서 검증)
- company CLI에 `reclaim-expired` 명령이 있다
허용: 브랜치 작업, 로컬 수정, 로컬 커밋, 스키마 마이그레이션
금지: push, merge, 실제 모델 호출
참조: reference/v02/reference/execution_lease.py 전체
```

### WorkOrder C — 실제 검증기 (트랙 2)

```
목표: exact-text 검증기 옆에 항목별 채점 검증기를 추가하고 Evidence에 연결한다
범위: verifier 패키지, Evidence 저장 경로, 관련 테스트. executor·원장 청구 코드는 수정 금지
완료 기준 (reference/v02/tests/test_rubric_verifier.py 8개를 옮겨 전부 통과):
- 평가 사례 JSON 형식(must_include_any/all, must_not_include, expect_refusal,
  must_cite_source, expected_source_ids, judge_criteria, weight)을 지원한다
- critical_forbidden 문자열이 하나라도 노출되면 점수와 무관하게 FAIL
- 가중 점수 >= threshold 이고 critical 위반 0건일 때만 PASS
- judge_criteria가 있는데 판정자가 없으면 그 항목은 통과로 처리하지 않는다
- 리포트 JSON과 evidence_sha256이 Evidence로 저장되고, 같은 입력이면 해시가 같다
- exact-text 검증기는 합성 fixture 경로에만 남기고, 실제 모델 결과 경로에서는 호출되지 않는다
- 기존 verifier 게이트("검증기 없으면 실행 차단")는 두 검증기 모두에 적용된다
허용: 별도 브랜치 작업, 로컬 수정, 로컬 커밋
금지: push, merge, executor·청구·확정 코드 수정, 실제 모델 호출
참조: reference/v02/reference/rubric_verifier.py
```

### WorkOrder D — 평가용 합성 챗봇 (트랙 2)

```
목표: 실제 모델이 '수정'할 대상인 합성 고객 FAQ 챗봇과 평가 사례 10건을 저장소에 넣는다
범위: ventures/synthetic-cafe-a/ (또는 사업별 작업 폴더 규칙에 맞는 위치), 관련 테스트
완료 기준 (reference/v02/tests/test_synthetic_bot.py 5개 통과):
- 모델을 쓰지 않는 결정적 챗봇: 같은 질문 → 같은 답
- 자료 밖 질문은 추측하지 않고 정해진 거절문으로 답한다
- 모든 비거절 답변에 출처(source id)가 붙는다
- internal_note 등 비공개 필드는 어떤 질문에도 노출되지 않는다
- 서버는 127.0.0.1에만 바인딩하고 /health, /, /ask 를 제공한다
- eval_cases.json 은 _status=DRAFT 로 들어가며, run_eval.py --require-approved 는 DRAFT면 실패한다
- 기준 챗봇이 eval_cases.json 을 PASS 한다 (수정 요청 전 상태가 합격이어야 '수정'을 평가할 수 있다)
- 다른 고객 폴더(ventures/synthetic-cafe-b 빈 폴더)의 파일을 읽지 않음을 테스트로 증명한다
허용: 별도 브랜치 작업, 로컬 수정, 로컬 커밋
금지: push, merge, 실제 고객 데이터, 외부 포트 바인딩
참조: reference/v02/reference/synthetic_faq_bot/ 전체
CEO 작업(병행): eval_cases.json 의 질문·정답·금지 답·임계값을 확정하고 _status를 APPROVED로 바꾼다
```

### WorkOrder E — 실제 모델 실행자

```
목표: ExecutorPort 뒤에 실제 코딩 에이전트 CLI 실행자를 한 개만 연결한다
범위: executor 패키지, 워크트리 관리, 설정 파일, 관련 테스트
완료 기준 (reference/v02/tests/test_model_executor.py 6개를 옮겨 전부 통과 + 아래):
- WorkOrder마다 git worktree와 브랜치(wo/<id>)를 만들고 실행자 cwd로 쓴다
- CLI는 subprocess timeout=time_limit_seconds 로 실행되며 초과 시 label=TIMEOUT
- CLI 출력에서 비용을 파싱한다. 파싱 불가면 cost_unknown=True 이고 원장은 상한 초과로 취급한다
- 실행 후 변경 파일 목록을 Evidence에 기록한다. 변경 0건은 성공이 아니다(NO_CHANGES)
- 실행 후 완료 기준 테스트 명령을 돌려 실패하면 TESTS_FAILED
- 실행자 환경변수는 허용 목록만 전달한다. API 키는 .env/비밀번호 관리자에서만 주입한다
- 실행자에 넘기는 지시문에는 build_instructions()의 실행 규칙 블록이 항상 포함된다
- 실제 CLI 플래그(권한 모드, 허용 도구, 턴 제한, 샌드박스)는 설치 버전 문서로 확인해 설정 파일에 두고,
  확인한 문서 주소를 PR 설명에 적는다
- 실제 CLI로 "hello() 수정" 수준의 최소 작업 1건을 돌려 Run·Event·Evidence·비용이 기록됨을 보인다
허용: 브랜치 작업, 로컬 수정, 로컬 커밋, 실제 CLI 최소 실행 1회(비용 상한 0.5달러)
금지: push, merge, 외부 발송·게시·결제 도구 허용, 실제 고객 데이터
참조: reference/v02/reference/model_executor.py
```

### WorkOrder F — 최소 브라우저 확인

```
목표: 터미널 없이 첫 사이클을 확인할 수 있는 최소 화면 1개
범위: 읽기 전용 현황 페이지(127.0.0.1) + 승인/수정요청 두 버튼
완료 기준:
- WorkOrder 목록, 상태, 마지막 Run outcome·비용·소요시간, 챗봇 미리보기 링크, 검증 리포트 요약을 보여준다
- "승인"은 CEO 승인 Event를, "수정 요청"은 새 WorkOrder(원본 링크 포함)를 만든다. 원장 직접 수정 없음
- 화면은 SQLite를 읽기 전용으로 연다 (mode=ro)
- 그 이상(타임라인, 중단/재시작, 백업 상태)은 V0.3으로 보낸다
허용: 브랜치 작업, 로컬 수정, 로컬 커밋
금지: push, merge, 외부 포트 바인딩, 프론트엔드 프레임워크 도입
```

### WorkOrder G — DB 안전성 (첫 실제 고객 전 관문)

```
목표: canonical DB를 저장소 밖으로 옮기고 백업·복원을 시험한다
범위: DB 경로 설정, company CLI backup/verify/restore 명령, 관련 테스트
완료 기준 (reference/v02/tests/test_db_backup.py 2개 통과 + 아래):
- 기본 DB 경로가 저장소 밖(예: %LOCALAPPDATA%\ai-company-os\ledger.sqlite3)이며 설정으로 바꿀 수 있다
- 백업은 sqlite3 온라인 백업 API로 만들고 .sha256을 함께 남긴다
- verify = 해시 일치 + integrity_check + 표 행 수 일치
- restore는 verify 실패 시 중단한다
- 실행 중 DB 파일 자체를 OneDrive 등 동기화 폴더에 두는 설정은 거부한다
- 문서: 새 PC에서 백업으로 원장을 복원하는 절차
허용: 브랜치 작업, 로컬 수정, 로컬 커밋
금지: push, merge, 실행 중 DB 파일 직접 복사
참조: reference/v02/reference/db_backup.py
```

## 4. 첫 생산 사이클 (E·F 완료 후, CEO가 직접 실행)

1. `eval_cases.json` 을 APPROVED로 확정한다.
2. 합성 고객 수정 요청 1건을 작업지시서로 만든다. 예: "명절 휴무 안내를 '명절 당일과 다음 날'로 바꾸고, 신규 메뉴 '흑임자 라떼' FAQ를 추가하라." 평가 사례 2건을 여기에 맞춰 추가·확정한다.
3. CEO 착수 승인 → 워크트리 생성 → 실제 모델 실행(시간 20분, 비용 2달러 상한) → 자동 테스트 → 항목별 평가 → 127.0.0.1 미리보기에서 직접 질문 → Claude 독립 검수(diff·테스트 출력·평가 리포트 원문 첨부) → CEO 승인 → 머지.
4. 결과·비용·소요시간·수정 횟수를 `lessons/` 에 기록한다. 이것이 V0.2 종료 증거다.

## 5. V0.2 완료 판정 체크리스트

- [ ] WorkOrder 0~E 각각 별도 PR로 머지됨 (CEO 머지)
- [ ] 참조 테스트 29개가 저장소 스키마에 이식되어 전부 통과
- [ ] 실제 모델 실행 Run이 원장에 1건 이상 있고 cost_usd가 기록됨
- [ ] 항목별 평가 리포트가 Evidence로 저장되고 verdict=PASS
- [ ] 127.0.0.1 미리보기에서 수정된 챗봇이 동작함
- [ ] Claude 독립 검수 결과가 ReviewRequest에 결속됨
- [ ] 강제 종료 후 재시작 시 만료 청구가 회수됨을 수동으로 1회 확인
- [ ] 알려진 HIGH 결함 0건, 나머지는 관문별로 로드맵에 기록됨
