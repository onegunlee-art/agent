# V0.7 — 제품 버전에 결속된 자동 검수

## 범위와 사용 화면

작업 지시는 기존 Cursor/Codex 창구에서 한다. `company work review --headless`는
검수 한 번만 실행한다. 현황판(`http://127.0.0.1:8780/`)은 자동 검수 이력,
제품 커밋, 토큰, 시간, 실행 ID를 표시한다. 승인/수정요청 버튼은 사람만 누른다.
검수 시작 버튼, 야간 예약, 자동 수정, 자동 병합은 이번 버전에 없다.

## 진입 명령

아래 값은 실제 WorkOrder와 제품 저장소에 맞춰 바꾼다. 준비된 PASS Run과 고정된
verifier가 필요하다. OS 저장소를 제품 저장소 대신 암묵적으로 사용하지 않는다.

```powershell
.venv/Scripts/python.exe -B -m company_os.cli --root C:/dev/company-runtime work review <WorkOrder-ID> --headless --repository C:/dev/product --claude-executable C:/Users/IBK/.local/bin/claude.exe --test-arg=C:/dev/ai-company-os/.venv/Scripts/python.exe --test-arg=-B --test-arg=-m --test-arg=pytest --test-arg=-q --idempotency-key=review-attempt-1
```

실제 CLI는 기본 테스트에서 실행하지 않는다. 합성 수용 데모는 아래처럼 실행한다.
`--execute`가 없으면 준비만 하고 구독을 사용하지 않는다.

```powershell
.venv/Scripts/python.exe -B examples/headless_review_demo.py --root C:/dev/ai-company-os-var/v07-demo
.venv/Scripts/python.exe -B examples/headless_review_demo.py --root C:/dev/ai-company-os-var/v07-demo --execute --claude-executable C:/Users/IBK/.local/bin/claude.exe
.venv/Scripts/python.exe -B -m company_os.cli --root C:/dev/ai-company-os-var/v07-demo dashboard --port 8781
```

이 데모의 제품과 임원 응답은 결정적 합성 fixture다. 실제 Claude 검수 여부와
제품을 실제 AI가 만들었는지는 별개이며, 데모를 새 생산 사이클로 세지 않는다.

## 정확성 및 기록

- 제품 repository/commit/tree/v2 해시와 OS kernel commit/해시를 구분한다.
- 검수 파일은 Git blob에서 직접 추출한다. 줄바꿈, export-ignore/export-subst로
  다른 코드가 전달되는 것을 피한다. 제품 소스는 clean/full checkout이어야 한다.
- SQLite 안에서 stop 확인과 lease/fence 획득을 묶고 실행은 트랜잭션 밖에서 한다.
  만료된 청구는 다음 명시적 시도에서 회수하고, 이전 실행 결과는 채택하지 않는다.
- 테스트는 커널의 별도 프로세스가 실행한다. Claude는 소스와 시험 출력을 검토한다.
  Claude가 직접 테스트를 실행했다는 주장은 하지 않는다.
- 요청, 명령, 프롬프트, 시험 출력, Claude 원문, receipt, manifest를 실행 시점에
  배타적으로 생성하고 SQLite Evidence/Event에 결속한다. 과거 Evidence는 고치지 않는다.
- 수동 ingest로 source=headless_claude를 입력할 수 없다. 현재 실행 ID와 정확한
  결과 해시가 원장에 결속된 경우에만 자동 결과를 받는다.
- 기본 600초(최대 1200초), 8턴(최대 30턴), 토큰 100,000(최대 250,000)이다.
  토큰 한도는 실행 후 채택 거부이며 선불 지출 차단이 아니다. 호출 단위는 CLI
  프로세스다. 구독의 실제 USD는 null이고 0으로 간주하지 않는다.
- QUOTA_WAIT/AUTH_REQUIRED/CLI_NOT_FOUND/CLI_FAILED/INVALID_RESPONSE/TIMEOUT/
  TESTS_FAILED/USAGE_UNKNOWN/USAGE_LIMIT_EXCEEDED는 검수 판정이 아니다.
  모두 WorkOrder를 완료하지 않는다. 재시도에는 새 idempotency key가 필요하다.
  정확한 공급자 리셋 시각을 모르면 지어내지 않으며 자동 재개하지 않는다.

## 권한과 한계

Claude Code 공식 네이티브 CLI와 본인 구독 로그인을 사용한다. API 키와 임의 API
주소는 서브프로세스로 전달하지 않는다. 토큰을 읽어 출력하거나 복사하지 않는다.
`--restricted --safe-mode`, Read/Glob/Grep만 사용하고 MCP와 쓰기/쉘 도구는 열지 않는다.
지원하지 않는 CLI 버전은 실패로 남긴다. `--bare`와 권한 우회 플래그는 사용하지 않는다.
공식 CLI의 managed policy는 별도 신뢰 경계다.

테스트 명령은 운영자가 지정하는 신뢰 코드이며 OS 수준의 악성 코드 샌드박스가 아니다.
초기 실제 연결 시험은 합성/공개 자료만 사용한다. 고객 자료를 외부 모델에 보내는
승인을 대신하지 않는다. 오래된 제품 검수 요청의 소스가 바뀌면 자동 재결속하지 않는다.
자동 repair와 평가 기준 변경은 다음 범위다.

공식 동작 근거: https://code.claude.com/docs/en/headless 및
https://code.claude.com/docs/en/cli-reference (2026-10-03 확인).
