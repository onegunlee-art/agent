# 개인 에이전트 코딩 업무 환경

## 어디서 에이전트와 대화하는가

주 대화 창구는 **Cursor에서 `C:\dev\ai-company-os`를 연 Codex 작업 채팅**이다.
만들거나 고칠 결과 하나마다 새 채팅을 시작한다. ChatGPT 데스크톱이나 Cursor
AI를 동시에 생산자로 쓰지 않는다. 내부 실행자가 필요할 때만 Company OS가
같은 PC의 Codex CLI를 제한된 Git worktree에서 비대화형으로 호출한다.

Codex는 현재 작업 트리와 `AGENTS.md`를 읽고 코드를 수정·시험하는 생산자다.
`company` CLI는 대화형 에이전트가 아니라 승인 상태, Evidence, Event,
WorkOrder를 보존하는 회사 원장이다. Claude Opus는 생성된 ReviewRequest와
소스 ZIP을 받아 독립 검수하는 외부 수동 검토자다.

따라서 일상 사용에서 창구는 다음처럼 나뉜다.

| 목적 | 사용하는 곳 | 역할 |
|---|---|---|
| 코딩 지시, 질문, 수정 협업 | Cursor의 Codex 작업 채팅 | 생산·시험·보고 |
| 회사 상태, 중단·재개, 증거 등록 | 통합 터미널의 `company` CLI | 공식 로컬 원장 |
| 완성된 챗봇 직접 사용 | 브라우저 `127.0.0.1:8765` | 합성 미리보기 |
| 프로젝트 상태·승인·수정 요청 | 브라우저 `127.0.0.1:8780` | 로컬 현황판 |
| 독립 최종 검수 | Claude의 별도 새 대화 | 수동 교차 검수 |
| 모바일·메신저 지시 | 아직 없음 | OpenClaw 후보, 후속 단계 |

OpenAI 공식 문서도 로컬 프로젝트가 컴퓨터 폴더를 채팅에 연결하며, 결과가
다른 작업은 별도 채팅으로 분리하고 지속 규칙은 `AGENTS.md`에 두도록
안내한다. 참고: [Projects and chats](https://learn.chatgpt.com/docs/projects),
[AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md).

## 어떻게 이야기하는가

자연어로 말하면 된다. 아래 다섯 항목이면 충분하다.

```text
목표: 이번 채팅에서 끝낼 결과 한 가지
범위: 바꿔도 되는 코드나 문서
완료 기준: 반드시 통과해야 할 시험 또는 확인 방법
허용: 브랜치 생성, 로컬 파일 수정 등
금지: push, merge, 실제 고객 데이터 사용 등
```

저장소 구현 작업에는 `$company-os-build` 스킬을 직접 지정할 수 있다.

```text
$company-os-build를 사용해서 장시간 executor가 SQLite 쓰기를 막지 않게
수정해. 실패하는 수용 테스트부터 만들고, main에는 merge하지 마.
```

명시하지 않아도 작업이 이 저장소의 실행·원장·검수·handoff 변경과 맞으면
Codex가 저장소 스킬을 선택할 수 있다.

## 매일 사용하는 흐름

1. Cursor에서 `C:\dev\ai-company-os` 프로젝트와 Codex 작업 채팅을 연다.
2. 결과 하나마다 새 Codex 채팅을 만들고 목표와 완료 기준을 말한다.
3. Codex가 별도 브랜치에서 실패 테스트 → 구현 → 전체 테스트 순서로 작업한다.
4. 변경 내용과 테스트 결과를 채팅에서 검토한다.
5. 독립 검수가 필요한 WorkOrder는 `company work review`가 만든 파일을
   Claude 새 대화에 전달하고, 응답 JSON을 `company review ingest`로 넣는다.
6. CEO가 승인한 경우에만 PR, push, merge를 수행한다.

상태 확인은 저장소 통합 터미널에서 실행한다.

```powershell
cd C:\dev\ai-company-os
.\.venv\Scripts\company.exe --root C:\dev\ai-company-os status
.\.venv\Scripts\company.exe --root C:\dev\ai-company-os inbox
```

챗봇과 현황판은 서로 다른 터미널 두 개에서 실행한다.

```powershell
cd C:\dev\ai-company-os
.\.venv\Scripts\company.exe --root C:\dev\ai-company-os preview --port 8765
```

```powershell
cd C:\dev\ai-company-os
.\.venv\Scripts\company.exe --root C:\dev\ai-company-os dashboard --port 8780 `
  --backup-dir "$env:LOCALAPPDATA\ai-company-os\backups"
```

그 뒤 브라우저에서 다음 주소를 연다.

- 챗봇: `http://127.0.0.1:8765/`
- 업무 현황판: `http://127.0.0.1:8780/`

Codex CLI 실제 실행은 `company work model-run --help`에서 인자를 확인한다.
작업마다 새 `wo/<id>` 브랜치와 worktree를 사용하며, 수리 실행에서만
`--reuse-worktree`를 명시한다. ChatGPT 로그인 실행은 USD가 제공되지 않을 수
있으므로 원장은 `cost_status=UNAVAILABLE`을 그대로 보존하고, 대신 기본
1회 호출·250,000토큰·WorkOrder 시간 제한을 적용한다. 여기서 호출 1회는
Codex CLI 프로세스 1회이며 내부 모델 turn 수가 아니다. 토큰은 입력+출력을
합산하고 캐시·추론 subtotal은 중복 합산하지 않는다. 시간 상한은 실행 중
프로세스 트리를 종료하지만 토큰 상한은 CLI 종료 후 초과 결과를 거부한다.
즉 토큰 상한은 결과 채택 관문이지 이미 발생한 사용량의 사전 지출 차단이
아니다. 최대 turn·context 기반 사전 제한은 V0.7 backlog다.

`company work model-run`은 실행 직후 지시문과 그 해시, 수용 테스트 명령,
worktree 브랜치와 시작 HEAD, sparse checkout 패턴, 생성 diff, 변경 파일
SHA-256을 별도 `RUN_REPRODUCIBILITY` Evidence와 Event에 자동 기록한다.
V0.3부터 이 명령은 모델 프로세스를 시작하기 전에 WorkOrder를 짧은 SQLite
트랜잭션으로 claim한다. 모델 실행 중에는 쓰기 트랜잭션을 열어두지 않으며,
완료 Run에는 `execution_id`, `fence_token`, `production_execution=true`,
`diagnostic_only=false`가 기록된다. 만료된 claim은 `company reclaim-expired`로
회수할 수 있고, 회수 뒤 늦게 돌아온 결과는 `STALE_RESULT_REJECTED`만 남기고
Evidence로 채택되지 않는다. V0.2에서 만들어진 6개 모델 Run은 당시 사실대로
`diagnostic_only=true`인 과거 기록으로 보존하며 수정하지 않는다.
과거 실행을 나중에 보강한 경우에는 `RETROACTIVE_OBSERVATION`으로 표시하며,
실행 시점에 캡처한 증거처럼 표현하지 않는다. 이 경우 원본 Evidence를 수정하지
않고 근거를 담은 추가 전용 `EVIDENCE_BACKFILLED` Event를 남긴다.

기본 `pytest`는 외부 실행 파일을 부르지 않는 단위·수용 테스트만 실행한다.
설치된 Codex CLI를 실제로 시작하는 검사는 `integration` marker로 분리되어
기본 실행에서 제외된다. 운영자가 명시적으로 확인할 때만 아래처럼 실행한다.

```powershell
.\.venv\Scripts\python.exe -m pytest -m integration tests\integration
```

Git 체크아웃·tree·줄바꿈 재현 테스트에는 `slow` marker가 붙는다. 릴리스 관문과
기본 전체 테스트는 이를 포함한다. 개발 중 빠른 루프에서만 명시적으로 제외한다.

```powershell
.\.venv\Scripts\python.exe -m pytest -m "not slow and not integration"
```

현재 `preview`는 LLM이 매 질문의 문장을 생성하는 챗봇이 아니라 합성 자료를
결정적으로 검색하는 FAQ 미리보기다. 실제 모델 Codex는 이 프로그램과 데이터를
수정하는 코딩 실행자로 사용된다. 알려진 답변의 출처는 `출처 보기` 버튼을
눌렀을 때만 펼쳐지고 자료 밖 질문에는 버튼이 나오지 않아야 한다.

평가 승인은 `eval_cases.json`의 `_status`를 직접 바꾸지 않는다. CEO가 승인한
원시 파일 SHA-256과 정확한 승인 문장을 로컬 텍스트 파일에 보존한 뒤 다음 두
명령으로 SQLite 원장 승인과 공식 평가를 분리해 실행한다.

```powershell
.\.venv\Scripts\company.exe --root C:\dev\ai-company-os evaluation approve <work-order-id> `
  --cases C:\dev\ai-company-os\examples\synthetic-cafe-a\eval_cases.json `
  --expected-sha256 <승인한-sha256> --approval-file <승인문.txt> `
  --idempotency-key <고유-승인-key>

.\.venv\Scripts\company.exe --root C:\dev\ai-company-os evaluation run <work-order-id> `
  --run <채택-run-id> `
  --cases C:\dev\ai-company-os\examples\synthetic-cafe-a\eval_cases.json `
  --data C:\dev\ai-company-os\examples\synthetic-cafe-a\faq_data.json `
  --approval <approval-id> --idempotency-key <고유-공식평가-key>
```

공식 Evidence는 승인 ID, 평가 파일 해시, clean Git commit/tree 해시를 함께
기록한다. 승인 뒤 파일이 한 바이트라도 바뀌면 공식 평가는 실패한다.

현황판 조회는 `query_only` SQLite 연결만 사용한다. 승인·수정 요청 버튼은
현황판 프로세스가 원장을 직접 쓰지 않고 별도 `company dashboard-action` CLI를
실행한다. CLI는 현재 Run·Artifact·검수 결속을 담은
`CEO_WORK_ORDER_APPROVED` 또는 `CEO_WORK_ORDER_CHANGE_REQUESTED` Event 하나만
추가하며 WorkOrder·Decision·Approval 행을 덮어쓰거나 새로 만들지 않는다.
평가 사례의 공식 승인은 위의 해시 결속 평가 승인 명령으로 별도 수행한다.

현황판의 승인·수정 요청 버튼은 사람만 누른다. 에이전트는 실행 중인 현황판을
브라우저로 조작하지 않는다. Event-only 동작은 합성 테스트 원장에 대한 자동
테스트와 로컬 HTTP 호출로만 검증하고, 운영 원장에는 사람의 실제 입력만 남긴다.

현황판은 승인 대기와 CEO 판단 필요 항목을 맨 위에 놓고, 성공 재실행에 가려진
거부·만료 시도까지 실행 이력 전체를 보존한다. 채택 Run 원가와 요청 전체
원가를 토큰·시간·USD로 분리하며, `--backup-dir`에서 SHA-256 sidecar가 있는
가장 최근 원장 또는 recovery ZIP의 시각을 보여준다. 권장 백업 위치는 다음과
같다.

```powershell
.\.venv\Scripts\company.exe --root C:\dev\ai-company-os ledger recovery-backup `
  --dir "$env:LOCALAPPDATA\ai-company-os\backups"
```

SQLite만 복구하려면 `company ledger backup/verify/restore`를 사용한다. 원장과
연결된 Evidence·Artifact·handoff까지 재해 복구하려면 아래 묶음을 사용한다.

```powershell
.\.venv\Scripts\company.exe --root C:\dev\ai-company-os ledger recovery-backup --dir C:\safe-backups
.\.venv\Scripts\company.exe --root C:\dev\ai-company-os ledger recovery-verify --bundle <company-recovery.zip>
.\.venv\Scripts\company.exe --root C:\dev\ai-company-os ledger recovery-restore --bundle <company-recovery.zip> --to-db <new-ledger.sqlite3> --to-root <empty-company-root>
```

## 현재 구축 수준

현재 main 기준선은 V0.5 통합본이다. V0.6 작업 브랜치는 그 기반 위에
단방향 승인 대기 알림과 재현된 검증·소스 결속 결함 수정을 제공한다.
이 변경의 자체 테스트와 Claude 독립 검수는 구분하며, 독립 PASS 전에는
V0.6 정식 종료나 실고객 운영 검증 완료로 표현하지 않는다.
다음 종료 절차와 사용 경계는 [V0.6 종료 관문](V06_RELEASE_GATE_KO.md)에 정리했다.

OpenClaw, Deep Agents, Langfuse, Graphiti, Browser Use, OpenSandbox, Docling,
DSPy를 한꺼번에 설치하지 않는다. 반복해서 검증된 절차만 다음 버전의 생산
라인 또는 스킬 후보로 승격한다.
