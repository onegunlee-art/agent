# V0.2 변경 출처와 검수 경계

검수 기준 diff는 `main...codex/v0.2-first-production-cycle` 하나다. 최종 로컬
커밋 뒤 이 범위의 binary patch와 파일 목록을 검수 묶음에 포함한다. 아래 표는
그 diff 안에서 “누가 무엇을 만들었는가”를 분리한다.

| 구분 | 변경/증거 | 정확한 의미 |
|---|---|---|
| Claude 코딩 킷 | `reference/v02/**` | 사용자가 제공한 참조 구현과 수용 테스트. 제품 원장의 독립 검수 결과가 아님 |
| 빌더 세션(Codex 작업 채팅) | 실행 lease, rubric, executor, 백업·복원, 현황판, 문서와 제품 테스트 | V0.2 OS 구현 및 결함 교정 |
| AI 직원 실행 | Run `run_e1037947ca3e4b80b2cc48f2c5341dbc`; `synthetic_faq.py` 변경 보고; 211,901토큰; `DONE` | 제한된 sparse worktree에서 출처 펼치기 프로그램 코드를 수정한 실제 모델 실행 |
| 빌더 자체 검토/내부 QA | 토글 상태 반전 실패 테스트와 `sources.hidden=expanded` 교정, source element id/`aria-controls` 보강 | AI 직원 산출물을 받은 뒤 빌더가 직접 검토·수정. Claude 독립 검수가 아님 |
| 빌더 후속 평가 보강 | q02 “추석 당일”, q12 “검은깨”와 동의어 보강 | 모델 결과를 본 뒤 빌더가 직접 추가. AI 직원 Run 산출물로 주장하지 않음 |
| Claude 독립 검수 | 아직 없음 | 최종 ZIP과 요청서를 Claude 새 대화에서 검토해 PASS를 받아야 성립 |

## 실행 증거

- 초과 거부 Run `run_3c736c848323458289bb354e7e4d6b18`은 원장에
  `USAGE_LIMIT_EXCEEDED`, 359,324토큰, 195.451초로 남아 있다. USD 금액은
  `UNAVAILABLE`이다. 사후 거부이므로 이미 소비된 토큰을 되돌렸다는 뜻이 아니다.
- 성공 Run `run_e1037947ca3e4b80b2cc48f2c5341dbc`은 `DONE`,
  211,901토큰, 172.084초다.
- 성공 Run에 당시 제시한 sparse 패턴 `examples`, `src/company_os`, `tests`,
  지시문과 테스트 명령을 보강한 Evidence는
  `evidence_82432ef8e83c4fe5962cbfaf186e5539`다.
- 후검토 source diff와 파일 해시를 추가한 Evidence는
  `evidence_34f1160479a348a9940f87b0df98f392`다. 이 diff에는 빌더의 한 줄
  토글 교정도 포함되며, 둘 다 `RETROACTIVE_OBSERVATION`이다.
- 위 두 사후 재구성 Evidence 파일은 원본 `MODEL_EXECUTION` Evidence를 수정하지
  않는다. 각 보강에는 근거와 원본 Evidence id를 담은 추가 전용
  `EVIDENCE_BACKFILLED` Event를 별도로 기록했다.
- 과거 실행 시점의 순수한 executor-only patch는 캡처되지 않았다. 따라서 위
  Evidence를 실행 당시 원본 diff로 표현하지 않는다. 이후 `model-run`은 같은
  정보와 정확한 diff를 실행 직후 `CAPTURED_AT_EXECUTION`으로 자동 기록한다.

## 검수 시 사용할 구분

최종 요청서에는 다음을 함께 넣는다.

1. `main...feature` 단일 patch와 파일 목록
2. 두 실제 Run의 원장 행과 `MODEL_EXECUTION` Evidence
3. 위 두 `RUN_REPRODUCIBILITY` Evidence와 각 파일 SHA-256
4. CEO 승인 뒤 생성할 공식 rubric 리포트와 Evidence
5. 전체 제품 테스트 및 Claude 참조 테스트 원문 결과

이 문서의 “자체 검토”는 내부 QA만 뜻한다. “독립 검수”는 위 자료를 받은
Claude가 최종 소스 결속을 확인하고 판정한 경우에만 사용한다.
