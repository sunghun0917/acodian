# CLAUDE.md — AX-WMS 프로젝트 Claude 지침

> 이 파일은 루트 `AGENTS.md`의 공통 원칙(§1~§5)을 Claude Code용으로 옮긴 것이다.
> 공통 규칙이 바뀌면 `AGENTS.md`와 이 파일을 함께 갱신한다.
> `AGENTS.md` §6 이하(스킬 관리, 모델 라우팅, Codex 서브에이전트, OMX Runtime Invariants)는 Codex/OMX 전용이므로 Claude에는 적용하지 않는다.

## 1. 응답 언어

- **항상 한국어로 답변한다.**
- 코드, 파일 경로, 기술 식별자(변수명, 클래스명, 라이브러리명 등)는 원문 그대로 유지한다.

---

## 2. 적용 범위

- 이 파일은 저장소 전체에 적용되는 공통 원칙이다.
- Claude Code는 `AGENTS.md`를 자동으로 읽지 않는다. 하위 디렉토리를 수정할 때는 해당 영역의 `AGENTS.md`를 **직접 먼저 읽고** 따른다.
- 하위 `AGENTS.md`는 해당 디렉토리 아래에서 **추가 규칙**과 **우선 규칙**을 제공한다.
- 실제로 수정하는 파일 경로 기준으로 관련 지침을 모두 따른다.
  - 예: `api/...` 수정 시 이 파일 + `/api/AGENTS.md`
  - 예: `web/...` 수정 시 이 파일 + `/web/AGENTS.md`
  - 예: `ai/...` 수정 시 이 파일 + `/ai/AGENTS.md`
  - 예: `infra/...` 수정 시 이 파일 + `/infra/AGENTS.md`

---

## 3. 프로젝트 개요

- **프로젝트**: AX-WMS (`S14P31S209`)
- **구조**: 모노레포 — `web` / `api` / `ai` / `infra`

| 영역 | 기술 | 역할 |
|---|---|---|
| `web` | Next.js | 사용자 UI, 화면 조합, 사용자 상호작용 진입점 |
| `api` | Spring Boot | 비즈니스 규칙, 인증/인가, 조직/업무/파일/알림 기준 시스템 |
| `ai` | FastAPI | AI 파이프라인 — 요약/태그/청킹/임베딩/시맨틱 검색 |
| `infra` | Nginx / Docker / GitLab CI/CD | 외부 진입 게이트웨이, 배포, 운영 자동화, 보안 경계 |

전체 아키텍처 기준 문서: [`docs/architecture/AX-WMS_기획서_아키텍처가이드.md`](docs/architecture/AX-WMS_기획서_아키텍처가이드.md)

---

## 4. 작업 전 공통 규칙

- 작업 대상 디렉토리의 하위 `AGENTS.md`가 있으면 **코드 수정 전에 먼저 읽는다.**
- 요청한 범위 밖의 리팩토링이나 불필요한 변경을 하지 않는다.
- 보안 취약점(SQL Injection, XSS, CSRF, 비밀정보 노출 등)이 생기지 않도록 주의한다.
- 새 규칙을 추측하지 말고, 해당 영역 문서에 있는 규칙을 우선 적용한다.
- UI/프론트엔드 변경 후에는 개발 서버에서 직접 동작을 확인한 뒤 완료로 보고한다.

### 4.1 Git 커밋 규칙

- **사용자의 명시적 동의 없이 절대 커밋하지 않는다.** (`git commit`, `git commit --amend` 포함)
- "커밋해줘", "commit" 등 사용자가 분명히 지시한 경우에만 커밋을 실행한다.
- 작업 완료 보고는 커밋 실행 권한과 별개다. 변경 사항을 요약해 보고하되, 커밋 여부는 사용자가 결정한다.
- `git push`, `git reset --hard`, 브랜치 삭제 등 파괴적/공유 상태 변경 작업도 동일하게 명시적 동의가 필요하다.

### 4.2 코드 탐색 시 Graphify 활용

- `api` 또는 `ai`의 구조, 호출 관계, 데이터 흐름을 탐색할 때는 해당 영역 그래프의 증분 변경 여부를 먼저 확인한다.
- 변경이 없고 해당 영역의 `graphify-out/graph.json`이 있으면 Graphify를 우선 활용한다.
- Graphify 결과는 탐색 단서로 취급하고, 실제 소스 코드와 테스트로 검증한다.
- 변경이 있거나 그래프가 없거나 최신 상태를 확인할 수 없으면 일반 검색과 실제 코드 확인을 우선한다. 오래된 그래프를 참고할 때는 최신 상태가 아님을 밝힌다.
- 단순 파일명·문자열 검색에는 일반 검색을 사용한다.
- 그래프는 자동으로 갱신하지 않는다. 사용자가 요청하거나 작업에 최신 그래프가 필요한 경우 해당 영역을 갱신한다.
- 서브에이전트에 해당 탐색을 위임할 때도 이 기준을 작업 지시에 포함한다.
- Graphify 갱신 범위는 기본적으로 `api`와 `ai`로 제한한다.

---

## 5. 영역별 진입점

작업 영역의 `AGENTS.md`를 먼저 읽고, 그 안에서 지정한 문서를 따른다.
`adr.yaml` / `code-convention.yaml`이 1차 기준(source of truth)이며, `.docx` 표준 가이드는 보조 참고 자료다.

| 영역 | 진입점 | 1차 기준 문서 | 보조 참고 |
|---|---|---|---|
| `api` | [`api/AGENTS.md`](api/AGENTS.md) | [`docs/api/adr.yaml`](docs/api/adr.yaml), [`docs/api/code-convention.yaml`](docs/api/code-convention.yaml) | `docs/api/*.md` |
| `web` | [`web/AGENTS.md`](web/AGENTS.md) | [`docs/web/adr.yaml`](docs/web/adr.yaml), [`docs/web/code-convention.yaml`](docs/web/code-convention.yaml) | `docs/web/AX-WMS_NextJS_표준가이드.docx` |
| `ai` | [`ai/AGENTS.md`](ai/AGENTS.md) | [`docs/ai/adr.yaml`](docs/ai/adr.yaml), [`docs/ai/code-convention.yaml`](docs/ai/code-convention.yaml) | `docs/ai/*`, `docs/ai/AX-WMS_FastAPI_표준가이드.docx` |
| `infra` | [`infra/AGENTS.md`](infra/AGENTS.md) | [`docs/infra/adr.yaml`](docs/infra/adr.yaml), [`docs/infra/code-convention.yaml`](docs/infra/code-convention.yaml) | `docs/infra/AX-WMS_Nginx_표준가이드.docx` |

- `api` 코드 변경 시 `/api/AGENTS.md`의 주석/Javadoc 게이트(`SB-020`, `ADR-023`)를 따른다. 신규·변경 helper/util/factory/normalization 메서드는 짧아도 Javadoc 누락 여부를 확인한다.
