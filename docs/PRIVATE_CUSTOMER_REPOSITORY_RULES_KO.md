# 고객별 비공개 저장소 규칙 (V0.5)

실제 고객 자료는 공개 AI Company OS 저장소에 넣지 않는다. 고객을 이름 대신
소문자 식별자로 등록하고, OneDrive·Dropbox·Google Drive·iCloud 밖의 비공개 레지스트리 아래에
`clients/<id>`별 **독립 Git 저장소**를 만든다. 저장소마다 자기 `.git` 이력이 있으며
레지스트리 자체나 다른 고객 저장소와 Git 객체를 공유하지 않는다. 고객 자료는
`material`, 실행 중 파일은 `workspace`에 둔다. 비밀 표식 원문은 해당 저장소의
`client.json`에만 있으며 SQLite Event에는 표식 집합의 SHA-256만 남는다.
토큰·비용 상한과 sparse 경로는 SQLite Event가 정본이다. 비공개 파일의 값이
Event와 다르면 실행·평가·백업·삭제를 모두 거부한다.

고객 저장소를 만들 때 공개 OS의 `lines/chatbot`, `src/company_os`, `tests`를 단순
복사해 최초 커밋한다. submodule이나 공개 저장소의 worktree를 사용하지 않는다.
따라서 실행자 worktree는 해당 고객 저장소에서만 생성되며 다른 고객의 Git 객체를
조회할 수 없다. 공개 템플릿이 바뀌면 명시적인 갱신 작업으로 다시 복사해야 한다.
추가 동기화 폴더 이름은 `AI_COMPANY_OS_EXTRA_SYNC_FOLDERS` 환경 변수에 세미콜론으로
등록한다. 템플릿 갱신은 현재 고객 tree와 공개 템플릿 SHA-256을 결속한 CEO 승인문을
요구하고, 승인 Event와 갱신 전후 tree OID를 원장에 남긴다.

고객 실행은 다음 sparse 경로 네 개만 허용한다.

1. `lines/chatbot`
2. `material`
3. `src/company_os`
4. `tests`

다른 고객 폴더나 더 넓은 경로가 하나라도 있으면 모델 실행 전 거부한다. WorkOrder의
토큰·USD 상한도 고객 정책 상한 이하여야 한다. 다른 고객의 비밀 표식과 고객 표식은
평가 DRAFT의 `critical_forbidden`에 자동 합쳐진다.

```powershell
company --root C:\dev\ai-company-os client init client-001 `
  --private-root C:\private\ai-company-clients `
  --markers-file C:\private\client-001-markers.json `
  --token-limit 120000 --cost-limit-usd 1.25 `
  --idempotency-key client-001-init
```

`markers-file`은 `private_markers`와 `customer_markers` 문자열 배열만 담는다. 고객
원문, 연락처, FAQ는 `clients/<id>/material` 아래에서만 다룬다. lessons, 공개
저장소, 일반 Evidence 요약에는 원문을 복사하지 않는다.

타 고객 표식이 자동 합쳐진 평가 DRAFT도 고객 폴더 안에서만 생성한다.

```powershell
company --root C:\dev\ai-company-os client evaluation-draft client-001 `
  --private-root C:\private\ai-company-clients `
  --intake C:\private\ai-company-clients\clients\client-001\material\intake.json `
  --output C:\private\ai-company-clients\clients\client-001\workspace\eval_cases.json
```

고객 백업은 독립 저장소의 `.git` 이력까지 고객별 ZIP과 SHA-256 sidecar에 넣으며,
검증 후 빈 private root에만 복원한다. 삭제 요청은 정확한 고객 식별자를 다시
입력해야 한다. 삭제 명령은 고객 저장소 디렉터리 전체(`.git` 포함)와 관리 백업을
제거한 뒤에만 인증서와 `CUSTOMER_DATA_DELETED` Event를 남긴다. 인증서는
`active`, `backup`, `git_history` 범위별 수량·바이트·제거 여부를 분리 기록하며
원문이나 원래 파일명은 기록하지 않는다.

```powershell
company --root C:\dev\ai-company-os client backup client-001 `
  --private-root C:\private\ai-company-clients `
  --dir C:\private\ai-company-clients\backups

company --root C:\dev\ai-company-os client delete client-001 `
  --private-root C:\private\ai-company-clients `
  --confirm-client-id client-001 --idempotency-key delete-client-001
```

삭제가 파일 제거 뒤 Event 확정 전에 중단되면 다음 명령으로 검사한다. 활성 저장소나
관리 백업이 남아 있으면 기존 claim을 해제해 원래 삭제 명령을 재실행할 수 있게 하고,
둘 다 없으면 삭제 인증서와 Event를 사실에 맞게 확정한다.

```powershell
company --root C:\dev\ai-company-os client recover-delete client-001 `
  --private-root C:\private\ai-company-clients `
  --deletion-idempotency-key delete-client-001

company --root C:\dev\ai-company-os client sync-templates client-001 `
  --private-root C:\private\ai-company-clients `
  --approval-file C:\private\client-001-template-sync-approval.txt `
  --idempotency-key sync-client-001-v2
```
