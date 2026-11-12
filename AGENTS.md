# AGENTS.md — AX-WMS 전역 공통 원칙

## 1. 응답 언어

- 항상 한국어로 답변한다.
- 코드, 파일 경로, 기술 식별자(변수명, 클래스명, 라이브러리명 등)는 원문 그대로 유지한다.

## 2. 적용 범위

- 이 파일은 저장소 전체에 적용되는 공통 원칙이다.
- 하위 디렉토리에 별도 `AGENTS.md` 가 있으면 그 파일이 해당 디렉토리 아래에서 **추가 규칙**과 **우선 규칙**을 제공한다.
- 실제로 수정하는 파일 경로 기준으로 관련 `AGENTS.md` 를 모두 따른다.
  - 예: `api/...` 수정 시 `/AGENTS.md` + `/api/AGENTS.md`
  - 예: `web/...` 수정 시 `/AGENTS.md` + `/web/AGENTS.md`
  - 예: `ai/...` 수정 시 `/AGENTS.md` + `/ai/AGENTS.md`
  - 예: `infra/...` 수정 시 `/AGENTS.md` + `/infra/AGENTS.md`

## 3. 프로젝트 개요

- **프로젝트**: AX-WMS (`S14P31S209`)
- **구조**: 모노레포 — `web` / `api` / `ai` / `infra`

| 영역 | 기술 | 역할 |
|---|---|---|
| `web` | Next.js | 사용자 UI, 화면 조합, 사용자 상호작용 진입점 |
| `api` | Spring Boot | 비즈니스 규칙, 인증/인가, 조직/업무/파일/알림 기준 시스템 |
| `ai` | FastAPI | AI 파이프라인 — 요약/태그/청킹/임베딩/시맨틱 검색 |
| `infra` | Nginx / Docker / GitLab CI/CD | 외부 진입 게이트웨이, 배포, 운영 자동화, 보안 경계 |

전체 아키텍처 기준 문서:
- `docs/architecture/AX-WMS_기획서_아키텍처가이드.md`

## 4. 작업 전 공통 규칙

- 작업 대상 디렉토리의 하위 `AGENTS.md` 가 있으면 **코드 수정 전에 먼저 읽는다.**
- 요청한 범위 밖의 리팩토링이나 불필요한 변경을 하지 않는다.
- 보안 취약점(SQL Injection, XSS, CSRF, 비밀정보 노출 등)이 생기지 않도록 주의한다.
- 새 규칙을 추측하지 말고, 해당 영역 문서에 있는 규칙을 우선 적용한다.

## 4.1 Git 커밋 규칙

- **사용자의 명시적 동의 없이 절대 커밋하지 않는다.** (`git commit`, `git commit --amend` 포함)
- "커밋해줘", "commit" 등 사용자가 분명히 지시한 경우에만 커밋을 실행한다.
- 작업 완료 보고는 커밋 실행 권한과 별개다. 변경 사항을 요약해 보고하되, 커밋 여부는 사용자가 결정한다.
- `git push`, `git reset --hard`, 브랜치 삭제 등 파괴적/공유 상태 변경 작업도 동일하게 명시적 동의가 필요하다.

## 4.2 코드 탐색 시 Graphify 활용

- `api` 또는 `ai`의 구조, 호출 관계, 데이터 흐름을 탐색할 때는 해당 영역 그래프의 증분 변경 여부를 먼저 확인한다.
- 변경이 없고 해당 영역의 `graphify-out/graph.json`이 있으면 Graphify를 우선 활용한다.
- Graphify 결과는 탐색 단서로 취급하고, 실제 소스 코드와 테스트로 검증한다.
- 변경이 있거나 그래프가 없거나 최신 상태를 확인할 수 없으면 일반 검색과 실제 코드 확인을 우선한다. 오래된 그래프를 참고할 때는 최신 상태가 아님을 밝힌다.
- 단순 파일명·문자열 검색에는 일반 검색을 사용한다.
- 그래프는 자동으로 갱신하지 않는다. 사용자가 요청하거나 작업에 최신 그래프가 필요한 경우 해당 영역을 갱신한다.
- 서브에이전트에 해당 탐색을 위임할 때도 이 기준을 작업 지시에 포함한다.
- Graphify 갱신 범위는 기본적으로 `api`와 `ai`로 제한한다.

## 5. 영역별 진입점

- `web` 작업: `/web/AGENTS.md`
- `api` 작업: `/api/AGENTS.md`
  - API 코드 변경 시 `/api/AGENTS.md` 의 주석/Javadoc 게이트를 따른다. 신규·변경 helper/util/factory/normalization 메서드는 짧아도 Javadoc 누락 여부를 확인한다.
- `ai` 작업: `/ai/AGENTS.md` 및 해당 `docs/ai/*` 문서
- `infra` 작업: `/infra/AGENTS.md`

## 6. 에이전트 설정 및 스킬 관리

- `AGENTS.md` 는 프로젝트 공통 작업 규칙이고, 각 `SKILL.md` 는 특정 작업에 필요한 절차다. 스킬은 이 파일의 범위·보안·Git 규칙을 덮어쓰지 않는다.
- `.agents/skills/` 는 Codex와 Antigravity가 공유하는 프로젝트 스킬 원본 위치다. `git-branch-naming`, `git-commit`, `git-pr` 는 이 경로에서만 관리하며 `.codex/skills/` 에 중복 사본을 만들지 않는다.
- `.codex/skills/` 는 Codex/OMX 전용 스킬 위치이고, `.codex/agents/` 와 `.codex/prompts/` 는 역할별 실행 지침이다. 역할 파일과 스킬을 같은 기능으로 취급하지 않는다.
- 동일한 이름의 스킬이 여러 위치에 있으면 내용을 비교하고 한 원본으로 통합한다. 통합 전에는 각 도구의 발견 경로와 기존 참조를 확인하며, 어느 사본이 자동으로 우선 적용된다고 가정하지 않는다.
- 스킬이 겹치면 명시적 호출과 현재 세션의 훅 라우팅을 따르고, 그 외에는 작업 산출물에 가장 구체적인 스킬을 선택한다. 같은 목적의 선택적 스킬을 불필요하게 중복 실행하지 않는다.
- `.agents/roles/`, `.agents/orchestration/`, `.agents/plugins/` 는 현재 Antigravity용 역할·운영 문서다. 이름만으로 Codex 네이티브 역할이나 설치된 플러그인이라고 판단하지 않는다.

### 6.1 프로젝트 스킬 적용 기준

- API 계약·인가·실패 경로 테스트는 `.agents/skills/api-contract-test/SKILL.md` 를 작업 성격에 맞게 사용한다.
- 모든 코드 구현·수정 작업에는 Ponytail(Karpathy 가이드라인 통합본)을 적용한다. 이 지침은 API 전용 스킬이나 프로젝트 규칙을 대체하지 않는다.
- Ponytail은 Codex 플러그인(`ponytail@ponytail`)을 단일 원본으로 사용한다. 작업 전 실제 플러그인 로딩·신뢰 상태를 확인하고, 사용할 수 없으면 그 사실을 보고한다.
- Superpowers는 일괄 적용하지 않는다. 설계·디버깅·테스트·검증·리뷰 등 작업 역할에 맞는 스킬을 해당 `SKILL.md` 의 발동 조건에 따라 추가 적용하고, 적용한 스킬의 필수 절차는 생략하지 않는다.
- Ponytail의 최소 구현 원칙은 계약 확인·보안·회귀 검증을 생략할 근거가 아니다. 부가 스킬이나 자동 훅을 사용할 때는 실제 설치 버전과 세션 로딩 상태를 확인한다.
- OMX 자동 생성 영역은 수동 정리 대상에서 제외한다. 프로젝트 전용 규칙은 이 영역 밖에 유지한다.

### 6.2 프로젝트 모델 라우팅

- Codex 추론 수준은 최소 `medium`이다. 총괄은 `gpt-6.1-terra` / `high`를 사용하며, 복잡한 설계·계획은 `gpt-6.1-sol` / `high`를 사용한다. 단순한 설계·계획은 Terra가 처리할 수 있다.
- 비평 및 복잡한 디버깅·보안·동시성 등 고위험 검토와 검증은 `gpt-6.1-sol` / `high`로 수행한다. 일반 코드 리뷰·디버깅은 `gpt-6.1-terra` / `high`, 일반 검증은 `gpt-6.1-terra` / `medium`을 시험한다.
- 일반 구현·테스트는 Terra, 단순 탐색·문서는 Luna 또는 Terra로 처리할 수 있다. 역할별 경로는 `.codex/guides/model-routing.md`를 따른다. 트랜잭션·인증·락·마이그레이션 등 고위험 변경은 Sol이 설계·검토하거나 직접 수행한다.
- 네이티브 서브에이전트는 부모 공급자를 상속한다. 역할별 모델은 `.codex/agents/*.toml`을 확인한다.
- 위임할 때에는 필요한 파일·계약·완료 조건만 전달한다. 네이티브 서브에이전트는 부모 권한을 상속하므로 탐색 역할에는 수정을 금지한다고 명시한다. 별도 프로세스를 사용할 때는 탐색에 `read-only`, 구현에 `workspace-write` 샌드박스를 적용한다. 커밋·푸시 권한은 위임하지 않는다.
- 역할별 모델 선택 기준은 `.codex/guides/model-routing.md`를 따른다. 이 정책은 기존 보안·Git·검증 규칙을 대체하지 않는다.
- iBank 페르소나 에이전트는 이 모델 라우팅 변경 대상에서 제외한다. 모델과 추론 수준은 실제 프로젝트 설정과 역할별 TOML을 확인한다.

### 6.3 Codex 서브에이전트 역할 및 위임

- Orca가 작업을 배정한 경우 Orca가 전체 작업과 결과 통합을 조율하며, Codex worker는 할당된 범위 안에서 세션의 권한과 지침이 허용하는 서브에이전트를 사용한다.
- 독립적인 탐색·구현·검토·검증을 위임하면 도움이 되는 경우 적합한 역할을 선택한다. 사용자의 요청이나 적용되는 상위 지침이 위임을 허용하고 세션에 호출 도구가 있을 때만 실행하며, 단순한 작업은 직접 처리한다. 역할 지침에서 재위임을 금지하면 그 지침을 따른다.
- 역할 정의는 `.codex/agents/*.toml`을 기준으로 한다. 사용 가능한 역할은 실행 세션마다 확인하고, 역할 파일이나 스킬이 실행 권한을 추가한다고 가정하지 않는다.

| 작업 | 역할 |
|---|---|
| 저장소 탐색 | `explore` |
| 요구사항 분석·계획 | `analyst`, `planner` |
| 설계·계획 비평 | `architect`, `critic` |
| 구현·원인 분석 | `executor`, `debugger` |
| 코드 리뷰·검증·테스트 | `code-reviewer`, `verifier`, `test-engineer` |
| 외부 문서·의존성 | `researcher`, `dependency-expert` |
| UI·이미지·문서·Git | `designer`, `vision`, `writer`, `git-master` |
| 코드 단순화·팀 실행 | `code-simplifier`, `team-executor` (OMX 내부 역할) |
| iBank 페르소나 | `ceo`, `user-001` ~ `user-035` |

- 위임 시 작업 범위, 파일 소유권, 완료 조건과 필요한 검증을 명시한다. 여러 작업자가 같은 파일을 수정하면 충돌을 조율하고, 결과 통합과 최종 검증은 위임자가 책임진다.
- 서브에이전트는 부모 세션의 권한을 상속한다. 역할 지침의 읽기 전용 요청을 별도의 샌드박스 강제로 간주하지 않는다.
- OMX 스킬을 명시적으로 사용하면 해당 `SKILL.md`와 설치된 런타임의 절차를 따른다. Orca의 조율과 OMX Team을 동일한 실행 환경으로 취급하지 않는다.

## Durable Runtime Invariants (canonical SSOT)

This section is the single source of truth for durable state ownership, hook boundaries, cancellation, and Team coordination. Skills and role prompts reference it; they must not restate or weaken these rules.

### State and hook ownership

- Durable state is authoritative only in the current, proven session or Team scope. Compatibility discovery is read-only and never grants write authority.
- Hooks own normal skill activation and workflow-state persistence under `.omx/state/`; skills do not duplicate or mutate hook-owned state except through documented recovery paths.
- Native hook payloads, prompt labels, task text, cwd, environment, pointers, transcripts, markers, and local trackers are routing or diagnostic data, not ownership or write authority.
- The Team state files and `omx team api ... --json` are the source of truth for task lifecycle and mailbox coordination.

### Cancellation boundary

- Cancellation parses and validates arguments before mutation, resolves one exact writable scope, freezes and revalidates target identity, mutates only proven targets, and leaves unrelated sessions, legacy roots, Team artifacts, and tmux sessions untouched.
- Ralph cancellation must satisfy its documented terminal post-conditions in the same scope; linked modes are handled only when the link is proven.
- `--force` does not widen cancellation scope; it only removes the selected exact-session native-stop entry after the same authority checks. `--all` is unsupported.
- Team cancellation requires exact frozen Team root, internal name, session, leader pane, and runtime identity. It fails closed when that proof is unavailable or changes; it must not enumerate or broadly kill Team sessions or recursively delete unrelated Team state.

### Team protocol

- Team runtime is explicit and outside the default workflow. Ultragoal does not auto-launch Team, and ordinary workflows do not silently become Team runs.
- Workers ACK startup, claim before work, transition task status through the lifecycle API, use release only for rollback, and report verification evidence. Leaders own integration, final verification, and shutdown decisions.
- Prefer durable state writes and `omx team api ... --json` dispatch. Direct `tmux send-keys` is fallback-only, never primary dispatch; manual pane actions require prior state/evidence checks.
- Team shutdown waits for terminal task state and uses exact Team authority. It does not shut down active work unless explicitly aborting.

### Ultragoal ownership

- `.omx/ultragoal/goals.json` is the leader-owned plan and `.omx/ultragoal/ledger.jsonl` is its durable audit trail. Workers report task evidence only; they do not create worker ledgers, mutate Ultragoal artifacts, or checkpoint goals.
- Shell commands and hooks do not mutate hidden Codex goal state. The active agent uses `get_goal`, `create_goal`, and `update_goal` only at the documented gates, then checkpoints with a fresh `get_goal` snapshot.
