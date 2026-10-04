# AI Agent Factory 사용 안내

현재 범위는 로컬에서 제품을 정의하고 승인 묶음을 만드는 V0.10 alpha입니다.
승인된 제품 코드를 작성·검수·GitHub 전달하는 V1.0 생산 단계는 제품별 사람 승인을
받은 뒤에만 시작합니다. GitHub push는 서버 호스팅이나 웹서비스 배포가 아닙니다.

## 어디서 대화하고 어디서 확인하나

- 회의·수정 지시: `C:\dev\ai-company-os`를 연 Codex 작업 채팅
- 실제 CTO·CPO·CMO 실행: 로컬 `company council` 명령과 구독 로그인 CLI
- 진행·승인 대기: `http://127.0.0.1:8780/` 로컬 현황판
- 제품 사용 확인: 제품별 로컬 실행 명령 또는 `127.0.0.1` 미리보기
- 정본: 저장소 밖 SQLite 원장과 해시 결속된 `var/` Evidence
- 코드 전달: 승인 묶음에 적힌 별도 제품 GitHub 저장소

새 Codex 채팅의 첫 입력은 다음과 같습니다.

```text
$council-room 임원 회의를 열어 줘. 이런 제품을 만들고 싶어: <아이디어>.
먼저 CTO·CPO·CMO가 각각 질문과 제안을 하고, 내 답변을 원장에 기록한 영수증을 보여 줘.
```

자동 스킬 발견이 되지 않으면 `.agents/skills/council-room/SKILL.md` 전체를 읽고
그 지침대로 시작하라고 요청합니다. 스킬을 불러오기만 한 일반 채팅은 원장에 자동
기록되지 않습니다. 매 메시지 뒤에 session ID, turn ID, CEO 메시지 해시, 세 역할
상태가 포함된 영수증이 있어야 기록된 것입니다.

## 정확한 CLI 흐름

```powershell
company idea create "제품 아이디어" --idempotency-key <key>
company council open <idea-id> --idempotency-key <key>
company council turn <session-id> --message-file <utf8-file> --idempotency-key <key>
company council status <session-id>
company council retry <session-id> --turn <n> --role <cto|cpo|cmo> --idempotency-key <new-key>
company council decide <session-id> --decision-file <utf8-json> --idempotency-key <key>
company council close <session-id> --idempotency-key <key>

company product draft <session-id> --definition-file <json> --idempotency-key <key>
company product bundle <bundle-id>
company product approval-text <bundle-id>
company product approve <bundle-id> --expected-sha256 <sha256> `
  --approval-file <exact-ceo-sentence.txt> --idempotency-key <key>
company product plan <product-id> <bundle-id>
```

`turn`은 세 역할을 각각 별도 프로세스로 실행합니다. 한 역할만 실패하면 성공 답변은
보존하고 `retry`로 그 역할만 다시 실행합니다. `QUOTA_WAIT`와 `AUTH_REQUIRED`는
로그인이나 한도 상태를 사람이 해결하기 전에 반복 호출하지 않습니다.

임원 응답에 미해결 결정이 하나라도 있으면 `close`는 거부됩니다. CEO가
`schema_version`, `session_id`, `decisions`를 담은 JSON을 `council decide`로
기록해야 합니다. 각 결정에는 `decision_id`, `decision`, `rationale`가 필요합니다.
결정 원문은 Evidence와 Event에 해시 결속되지만 외부 사실의 검증 증거로 승격되지는
않습니다.

## 사용자 시나리오 승인

`product draft`는 product brief, development schema, user scenarios,
implementation plan, approval bundle 다섯 파일을 새 버전 경로에 만듭니다. 계획은 두 개
이상의 WorkOrder와 비순환 의존 순서, 허용 파일, 새 테스트, 완료 조건을 가져야 합니다.

CEO는 쉬운 한국어 사용자 시나리오와 GitHub 대상·병합·push 범위를 먼저 읽습니다.
`approval-text`가 출력한 정확한 문장을 승인 파일에 넣은 경우에만 `product plan`이
실행 계획을 반환합니다. 산출물 한 바이트가 바뀌거나 제품·버전이 다르면 승인은
재사용되지 않습니다. 서버 배포는 항상 별도 범위입니다.

## 재시작·로그인·쿼터 복구

PC 재시작 뒤에는 새 회의를 만들지 말고 다음으로 이어갑니다.

```powershell
company council status <session-id>
company council retry <session-id> --turn <n> --role <unfinished-role> `
  --idempotency-key <new-key>
```

Codex 로그인은 현재 Codex/ChatGPT 앱의 기존 구독 로그인을, CPO는 Claude Code의 기존
로그인을 사용합니다. API 키를 만들거나 원장에 넣지 않습니다. 설치 확인:

```powershell
codex --version
& "$env:USERPROFILE\.codex\.sandbox-bin\codex.exe" --version
& "$env:USERPROFILE\.local\bin\claude.exe" --version
```

PATH의 Codex가 오래됐으면 Company OS는 현재 Work 설치의
`%USERPROFILE%\.codex\.sandbox-bin\codex.exe`를 우선합니다. 모델이 더 새 버전을
요구하면 Codex/ChatGPT 또는 Cursor 확장을 업데이트한 뒤 버전을 다시 확인합니다.
Claude Code 자체 업데이트는 다음 명령을 사용합니다.

```powershell
& "$env:USERPROFILE\.local\bin\claude.exe" update
```

로그인 필요 상태가 나오면 각 CLI를 대화형으로 열어 로그인한 뒤, 기존 턴의 실패 역할만
새 idempotency key로 재시도합니다. 쿼터 대기는 표시된 실제 리셋 시각이 있을 때까지
기다립니다. 알 수 없는 리셋 시각을 추정하지 않습니다.

## AI Company OS 업데이트

릴리스 태그와 `origin/main`이 검증된 뒤 다음 순서로 업데이트합니다. 실행 중인 회의와
제품 원장은 저장소 밖에 있으므로 소스 업데이트로 덮어쓰지 않습니다.

```powershell
cd C:\dev\ai-company-os
git status --short
git fetch origin --tags
git switch main
git pull --ff-only origin main
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -B -m pytest -q
company --root C:\dev\ai-company-os status
```

작업 트리가 깨끗하지 않으면 pull 전에 임의 삭제·reset을 하지 말고 변경 주체를 확인합니다.
업데이트 후 기존 session ID로 `council status`를 실행해 복원을 확인합니다.

## 버전 관문

- V0.9: CTO·CPO·CMO 다중 턴 회의, 역할별 구독 CLI, 재시작·부분 재시도
- V0.10: 설계·사용자 시나리오·다중 WorkOrder·해시 결속 CEO 승인
- V1.0 기능 목표: 승인된 제품의 실제 코드 작성, 시험, 별도 Claude 세션 검수,
  범위 내 수정, 승인된 GitHub ref 전달

실고객 3건 납품, 매출, 무인 운영, 클라우드 호스팅은 기능 버전과 다른 사업 관문입니다.
