# V0.6 승인 대기 알림 최소판

이 기능은 승인 대기 또는 CEO 판단 필요 상태를 **밖으로 알리는 단방향 출구**다.
메시지에는 WorkOrder ID, 고정된 상태 요약, `127.0.0.1` 현황판 링크만 들어간다.
고객 이름, 작업 제목, 고객 자료, 비밀 표식, 실행 출력은 포함하지 않는다.

설정이 없으면 외부로 아무것도 보내지 않고 로컬 outbox JSON만 생성한다.

```powershell
.\.venv\Scripts\company.exe --root C:\dev\ai-company-os `
  --db "$env:LOCALAPPDATA\ai-company-os\ledger.sqlite3" `
  notification dispatch `
  --outbox "$env:LOCALAPPDATA\ai-company-os\notifications"
```

외부 채널은 설정 파일 하나에 webhook 또는 email 대상 하나만 등록한다. webhook은
HTTPS만 허용한다. email 비밀번호는 파일에 쓰지 않고 필요할 때
`AI_COMPANY_OS_EMAIL_USERNAME`, `AI_COMPANY_OS_EMAIL_PASSWORD` 환경 변수로 준다.

```json
{
  "schema_version": 1,
  "channel": "webhook",
  "target": "https://example.invalid/company-notify",
  "dashboard_url": "http://127.0.0.1:8780/"
}
```

```json
{
  "schema_version": 1,
  "channel": "email",
  "target": "ceo@example.invalid",
  "smtp_host": "smtp.example.invalid",
  "smtp_port": 587,
  "from_address": "company-os@example.invalid",
  "starttls": true,
  "dashboard_url": "http://127.0.0.1:8780/"
}
```

성공은 `NOTIFICATION_SENT`, 실패는 `NOTIFICATION_FAILED` Event로 원장에 남는다.
실패는 WorkOrder 실행이나 평가를 되돌리거나 막지 않으며 다음 명시적 dispatch에서
재시도할 수 있다. 백그라운드 daemon·scheduler는 없고 운영자가 필요할 때만 명령을
실행한다.

## 권한 경계

알림에서 돌아오는 입력 경로는 없다. 링크를 눌러도 로컬 현황판만 열리며, webhook
응답·이메일 답장·메신저 메시지를 승인으로 해석하는 코드가 없다. 원격 승인 API,
수신 webhook, 이메일 수신기 역시 없다. 승인과 수정 요청은 기존 로컬 CEO 절차에서만
기록한다. 향후 원격 입력을 추가하려면 별도 권한 모델과 완전 검수를 먼저 통과해야 한다.
