# Codex 모델 라우팅

Codex 추론 수준은 최소 `medium`으로 설정한다. 역할별 실제 설정은 `.codex/agents/*.toml`을 기준으로 하며, 네이티브 서브에이전트는 부모 세션의 공급자와 권한을 상속한다.

| 작업 | 모델 | 추론 |
|---|---|---|
| 총괄 | `gpt-6.1-terra` | `high` |
| 복잡한 설계·계획·비평, 고위험 검토·검증 | `gpt-6.1-sol` | `high` |
| 일반 구현·테스트·검증 | `gpt-6.1-terra` | `medium` |
| 일반 코드 리뷰·디버깅 | `gpt-6.1-terra` | `high` |
| 단순 탐색·문서 | `gpt-6-luna` | `high` |

고위험 작업은 총괄이 Sol 설계·검토·검증 경로를 선택한다. 역할 파일은 위험도를 자동 분류하지 않는다. iBank 페르소나는 별도 설정을 유지한다.

| 서브에이전트 | 모델 | 추론 |
|---|---|---|
| `architect`, `planner`, `critic`, `dependency-expert` | `gpt-6.1-sol` | `high` |
| `code-reviewer`, `debugger` | `gpt-6.1-terra` | `high` |
| `analyst`, `code-simplifier`, `executor`, `researcher`, `team-executor`, `test-engineer`, `verifier`, `designer`, `vision` | `gpt-6.1-terra` | `medium` |
| `explore`, `git-master`, `writer` | `gpt-6-luna` | `high` |
