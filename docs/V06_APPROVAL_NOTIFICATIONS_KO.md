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

현황판 주소는 `http://127.0.0.1:<포트>/`만 허용한다. 고객 정보가 섞일 수 있는
경로·쿼리·fragment·사용자정보는 거부한다. 이메일은 표시명 없는 단일 주소만,
webhook은 단일 HTTPS 주소만 허용하며 HTTP redirect를 따라가지 않는다.
발송 중에는 SQLite 쓰기 트랜잭션을 보유하지 않는다. 로컬 파일 생성과 외부
발송은 서로 다른 경로로 중복 방지되므로, 나중에 채널을 설정해도 로컬 파일
생성이 외부 발송을 막지 않는다. 대상 주소 원문 대신 경로 해시만 Event에 남긴다.

외부 전송 직후 원장 기록 전에 프로세스가 종료되면 실제 수신 여부를 확정할 수
없다. 이 경우 남은 CLAIMED 기록은 자동 재발송하지 않고 건너뛴다. 운영자가
수신측 기록을 확인해야 하며, 외부 전송의 정확히 한 번 실행을 보장하지 않는다.
일반 전송 예외는 재시도 가능으로 기록하므로 수신측도 notification_id로 중복을
제거하는 것이 좋다. 새 경로 결속 방식은 기존 Event를 수정하지 않으며, 구버전
방식으로 이미 발송한 대기 항목은 업그레이드 후 한 번 더 알림될 수 있다.

휴대폰에서 이 localhost 링크를 눌러도 PC 현황판이 열리는 것은 아니다. V0.6은
알림을 받고 PC로 돌아와 결재하는 단계다. 예약 실행·원격 접속·원격 승인은 없다.

## 권한 경계

알림에서 돌아오는 입력 경로는 없다. 링크를 눌러도 로컬 현황판만 열리며, webhook
응답·이메일 답장·메신저 메시지를 승인으로 해석하는 코드가 없다. 원격 승인 API,
수신 webhook, 이메일 수신기 역시 없다. 승인과 수정 요청은 기존 로컬 CEO 절차에서만
기록한다. 향후 원격 입력을 추가하려면 별도 권한 모델과 완전 검수를 먼저 통과해야 한다.
