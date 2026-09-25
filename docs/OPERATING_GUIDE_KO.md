# 개인 에이전트 코딩 업무 환경

## 어디서 에이전트와 대화하는가

주 대화 창구는 **ChatGPT 데스크톱 앱의 Codex 로컬 프로젝트**다. 로컬
프로젝트의 기본 폴더를 `C:\dev\ai-company-os`로 지정하고, 만들거나 고칠
결과 하나마다 새 채팅을 시작한다. Codex CLI를 사용할 때도 이 폴더에서
`codex`를 실행하거나 `codex -C C:\dev\ai-company-os`로 같은 작업 문맥을
선택한다.

Codex는 현재 작업 트리와 `AGENTS.md`를 읽고 코드를 수정·시험하는 생산자다.
`company` CLI는 대화형 에이전트가 아니라 승인 상태, Evidence, Event,
WorkOrder를 보존하는 회사 원장이다. Claude Opus는 생성된 ReviewRequest와
소스 ZIP을 받아 독립 검수하는 외부 수동 검토자다.

따라서 일상 사용에서 창구는 다음처럼 나뉜다.

| 목적 | 사용하는 곳 | 역할 |
|---|---|---|
| 코딩 지시, 질문, 수정 협업 | Codex 로컬 프로젝트 채팅 | 생산·시험·보고 |
| 회사 상태, 중단·재개, 증거 등록 | 통합 터미널의 `company` CLI | 공식 로컬 원장 |
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

1. ChatGPT 데스크톱 앱에서 `C:\dev\ai-company-os` 로컬 프로젝트를 연다.
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

## 현재 구축 수준

V0.1-alpha는 아이디어, CTO/CPO/CMO handoff, 계약, WorkOrder, Evidence,
독립 리뷰, 중단·재개를 로컬에서 보존한다. V0.2 첫 기반 작업은 실제 실행자를
붙이기 전에 장시간 executor와 SQLite 쓰기 트랜잭션을 분리하는 것이다.

아직 AI Company OS 내부에서 OpenAI·Anthropic 모델을 자동 호출하지 않는다.
OpenClaw, Deep Agents, Langfuse, Graphiti, Browser Use, OpenSandbox, Docling,
DSPy를 한꺼번에 설치하지 않는다. 다음 수직 슬라이스는 평가 사례가 있는
조사 직원 한 명이며, 통과한 작업 방식만 새 스킬 후보로 승격한다.
