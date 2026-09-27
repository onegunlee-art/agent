# 고객별 비공개 저장소 규칙 (V0.5)

실제 고객 자료는 공개 AI Company OS 저장소에 넣지 않는다. 고객을 이름 대신
소문자 식별자로 등록하고, OneDrive 밖의 별도 비공개 저장소에서
`clients/<id>/material`과 `clients/<id>/workspace`만 사용한다. 비밀 표식 원문은
이 비공개 `client.json`에만 있으며 SQLite Event에는 표식 집합의 SHA-256만 남는다.
토큰·비용 상한과 sparse 경로는 SQLite Event가 정본이다. 비공개 파일의 값이
Event와 다르면 실행·평가·백업·삭제를 모두 거부한다.

고객 실행은 다음 sparse 경로 네 개만 허용한다.

1. `clients/<id>`
2. `lines/chatbot`
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

고객 백업은 고객별 ZIP과 SHA-256 sidecar로 만들며 검증 후 빈 private root에만
복원한다. 삭제 요청은 정확한 고객 식별자를 다시 입력해야 하며 활성 폴더와 해당
고객 백업을 삭제한 뒤, 원문이나 파일명 없이 수량·바이트·삭제 전 tree hash를 담은
인증서와 `CUSTOMER_DATA_DELETED` Event를 남긴다.

```powershell
company --root C:\dev\ai-company-os client backup client-001 `
  --private-root C:\private\ai-company-clients `
  --dir C:\private\ai-company-clients\backups

company --root C:\dev\ai-company-os client delete client-001 `
  --private-root C:\private\ai-company-clients `
  --confirm-client-id client-001 --idempotency-key delete-client-001
```
