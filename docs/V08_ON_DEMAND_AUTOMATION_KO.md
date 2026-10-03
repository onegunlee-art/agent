# V0.8 수동 실행 자동 수정 흐름

작업 지시는 Codex 작업 채팅에 한다. 사용자는 “이 수정 작업을 대기열에 넣고 끝까지 실행해”라고
요청하고, Codex가 승인된 제품 범위로 실행 계획을 등록한다. 결과는 브라우저 현황판의
수동 실행 대기열·전체 Run·검수 이력에서 확인한다. 현황판의 승인 버튼은 사람만 누른다.

## 이번 운영 결정

- 일일 토큰·CLI 호출 상한은 기본 `null`로 해제한다. 사용량은 계속 기록한다.
- 야간 예약, 자동 시작, 백그라운드 데몬은 만들지 않는다.
- 기존 WorkOrder의 개별 시간·토큰·호출 한도와 수정 최대 3회는 종료 조건이다.
- 구독 잔여 한도와 USD를 알 수 없으면 `null`이다. 로컬 토큰을 구독 주간 한도/7이나
  실제 지출로 바꾸어 표현하지 않는다. 토큰 초과는 실행 후 결과 거부이며 선불 지출 차단이 아니다.

## Claude 의견의 적용 범위

V0.7의 제품/커널 분리와 결정적 시험 후 검수는 유지한다. 실제 재현된 공백 change ID,
SUPERSEDED 재개 재생, FP_LITE 통과를 닫았다. FP_LITE는 과거 기록 진단용으로만 남는다.
과거 계약·Evidence·승인·Run은 재작성하지 않는다. 새 작업은 STANDARD/FULL을 사용한다.

V0.8은 CHANGES_REQUIRED → Codex 수정 → 커널 시험/TEST_RESULT/manifest → 재검수까지
연결한다. 수정 증거는 처음 검수한 **제품 저장소**에 결속한다. 매 실행마다 WorkOrder lease와
fence가 적용되며 외부 실행은 SQLite 쓰기 트랜잭션 밖에서 이루어진다.
`tick`은 대기열 한 단계이고, `queue run`은 최대 7단계의 전경 실행이다.

“저녁 지시 → 아침 PR”은 야간 모드를 켰을 때의 후속 목표다. 이번 실행은 로컬 제품 브랜치와
Evidence까지 만들며 고객 제품의 자동 push/PR/main 병합을 켜지 않는다. OS 릴리스의 병합과
push는 이번 사용자가 승인한 개발 작업으로 수행한다. 역할 3종의 자동 기획은 V0.9 범위다.
실고객 납품·지불 의향·납품 형태 확인은 V1.0 관문으로 남는다.

## 사용법

이미 VERIFIED 상태인 WorkOrder와 별도의 깨끗한 `wo/<id>` 제품 저장소를 사용한다.
`plan.json`은 운영 데이터로 저장소 밖에 둔다. 예:

```json
{
  "schema_version": 1,
  "work_order_id": "work_order_...",
  "repository": "C:/dev/company-products/synthetic-product",
  "allowed_files": ["writer.py"],
  "new_test_files": ["test_render_exact.py"],
  "test_command": ["C:/dev/ai-company-os/.venv/Scripts/python.exe", "-B", "-m", "pytest", "-q"],
  "codex_executable": "codex",
  "claude_executable": "claude",
  "max_repairs": 3
}
```

```powershell
cd C:\dev\ai-company-os
.\.venv\Scripts\company.exe --root C:\dev\company-runtime queue add --file C:\dev\company-runtime\plan.json --idempotency-key request-001
.\.venv\Scripts\company.exe --root C:\dev\company-runtime queue run
.\.venv\Scripts\company.exe --root C:\dev\company-runtime queue list
.\.venv\Scripts\company.exe --root C:\dev\company-runtime queue usage
```

`company tick`은 한 단계만 실행한다. `queue limits`에 숫자를 지정하면 선택적 일일 상한을
적용하고, 인자 없이 호출하면 다시 해제한다. 한도 사용 시 실행 전 최대 사용량을 예약하고
실제 영수증으로 정산한다. 사용량 미상 예약은 0으로 풀지 않는다.
공급자 QUOTA_WAIT/AUTH_REQUIRED는 자동 재시도하지 않는다. 로그인/쿼터 확인 뒤
`queue retry <job_id>`로 재시도를 명시한다. 공급자 리셋 시각을 응답에서 얻지 못하면 미상으로 둔다.

## 범위와 중단

실행자는 허용된 제품 파일만 수정한다. 기존 시험·평가·설정·AGENTS 파일은 보호한다.
계획의 `new_test_files`에 미리 지정한, 아직 존재하지 않는 회귀 시험 파일은 최대 3개
추가할 수 있다. 첫 채택 후에는 이 파일도 해시로 고정한다. 최종 검수는 시험의 충분성도 확인한다.
이는 신뢰 로컬 사용자의 workspace-write 실행 및 **실행 후 파일 검사**다. 파일별 OS 권한으로
모든 쓰기를 사전 차단한다는 주장은 하지 않는다. 범위를 벗어난 파일은 채택하지 않고 보존한다.
JUnit에 실제 통과 시험이 없거나 실패/skip이 있으면 수정 완료로 처리하지 않는다.
등록된 시험 목록이 변경별 시험 증거에 표시되며, 각 결함을 새 시험으로 완전히 입증했다는
주장과는 구분한다. 독립 검수자가 최종 코드와 이 시험 범위를 판단한다.

프로세스 중단/lease 만료/오래된 worker는 결과를 덮어쓸 수 없다. 파일 작업의 상태가
불명확하면 NEEDS_ATTENTION으로 남긴다. 자동으로 모델을 다시 호출하지 않는다.
보존된 Run·diff·manifest와 제품 브랜치를 확인한 뒤 작업을 복구한다.

## 업데이트

이 PC는 프로젝트의 편집 설치를 사용하므로 main 최신 코드가 실행 코드다. 업데이트 후
기존 로컬 서버를 종료하고 같은 root/DB로 다시 시작한 뒤 브라우저를 새로고침한다.
다른 PC는 작업 파일을 보존한 상태에서 `git pull --ff-only` 후 필요한 경우
`.venv\Scripts\python.exe -m pip install -e ".[dev]"`로 설치를 갱신한다.
원장 DB/고객 자료를 Git으로 동기화하지 않는다.

CLI 호출 방식 참고: [OpenAI Codex 비대화형 실행](https://developers.openai.com/codex/noninteractive/).
