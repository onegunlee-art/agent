# V0.2 전체 테스트 비정상 종료 분석

## 관찰

첫 전체 실행의 JUnit은 195개 완료, failure 0, internal error 1을 기록했다.
마지막 시작 항목은
`test_review_ingest_rejects_tampered_markdown_handoff`였고 JUnit 시간은
10,146.333초였다. pytest 내부 오류는 테스트 assertion이나 SQLite 예외가
아니라 `terminalwriter.flush()`의 `OSError: [Errno 22] Invalid argument`였다.

## 원인

Windows System 로그가 시간의 원인을 확정한다. PC는 2026-09-26 01:05:28에
`System Idle` 이유로 절전에 들어갔고 03:54:30에 복귀했다. 저전원 구간은
약 10,142초이며 JUnit에 늘어난 시간과 일치한다. 복귀 뒤 실행 도구의 터미널
출력 핸들이 무효화되어 pytest가 내부 오류로 종료됐다. 문제로 표시된 테스트를
`pytest -x -vv`로 단독 실행한 결과 call 0.61초, 전체 1.74초로 통과했다.
따라서 새 코드의 무한 대기나 실제 Codex CLI 로그인 대기가 원인은 아니다.

## 재발 방지

- 제품 테스트와 보관된 V0.2 참조 테스트의 모든 `subprocess.run`에 명시적
  timeout을 추가했다.
- 이 정책을 AST로 검사하는 `tests/test_subprocess_policy.py`를 추가했다.
- 장기 `Popen` 경로는 기존 `communicate(timeout=...)`와 Windows 프로세스 트리
  종료를 계속 강제한다.
- 설치된 실제 Codex 실행 파일을 부르는 검사는 `integration` marker로 분리했고
  기본 pytest 실행은 `not integration`만 선택한다. 명시적 실행은
  `pytest -m integration tests/integration`이다.
- 전체 기본 suite는 `pytest -x -v`로 262 passed, 1 deselected,
  146.71초에 완료됐다. 최종 커밋 직전 JUnit을 다시 생성한다.

PC 절전 자체를 저장소 코드가 영구 변경하지는 않는다. 장시간 수동 검증 시에는
사용자가 PC 전원 정책을 관리하며, 테스트 프로세스의 10분 완료 기준은 별도로
계속 확인한다.
