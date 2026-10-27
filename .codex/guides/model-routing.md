# Terra 총괄 + DeepSeek 워커

## 역할

| 역할 | 모델 | 추론 |
|---|---|---|
| 총괄 | `gpt-6.1-terra` | `high` |
| 복잡한 설계·계획 | `gpt-6.1-sol` | `high` |
| 비평 및 복잡한 디버깅·고위험 검토·검증 | `gpt-6.1-sol` | `high` |
| 일반 코드 리뷰·디버깅·코드 평가 | `gpt-6.1-terra` | `high` |
| 일반 검증·디자인·시각 분석 | `gpt-6.1-terra` | `medium` |
| 범위가 명확한 일반 구현·테스트 | 별도 `deepseek-worker` 프로필의 `deepseek-flash` 우선 고려 | 항상 `high` |
| 단순 탐색·문서 | `gpt-6-luna` 또는 `gpt-6.1-terra` | `high` 또는 `medium` |

네이티브 서브에이전트는 부모의 공급자를 상속하므로 DeepSeek는 별도 Codex 프로세스로 실행한다. 프로젝트 TOML에는 공급자 인증 설정을 넣지 않는다. 사용자 기본 설정과 Sol 로그인은 변경하지 않는다.

2026-10-28 확인 기준 `deepseek-flash`는 DeepSeek 공식 API의 **DeepSeek-V4.1-Flash-1017**이다. 버전명을 추측해 `deepseek-4.1-flash` 같은 미확인 모델 ID로 바꾸지 않는다.

## Codex 네이티브 추론 수준

Codex 추론 수준은 최소 `medium`으로 설정한다. 단순한 설계·계획은 Terra로 처리하고 복잡하거나 위험한 경우 Sol을 선택한다.

| 역할 | 추론 |
|---|---|
| 총괄 | `high` (`gpt-6.1-terra`) |
| `analyst`, `code-simplifier`, `executor`, `researcher`, `team-executor`, `test-engineer` | `medium` (`gpt-6.1-terra`) |
| `architect`, `planner`, `critic`, `dependency-expert` | `high` |
| `code-reviewer`, `debugger`, `code-evaluator` | `high` (`gpt-6.1-terra`) |
| `verifier`, `designer`, `vision` | `medium` (`gpt-6.1-terra`) |
| `explore` | `high` (`gpt-6-luna`) |
| `git-master`, `writer` | `high` (`gpt-6-luna`) |
| `benchmark-evaluator` | `medium` (`gpt-6.1-sol`) |

iBank 페르소나 에이전트는 이 모델 라우팅 변경의 대상이 아니며 기존 설정을 유지한다. 고위험 작업은 Sol 설계·검토·검증 경로로 처리한다.

## 일반 역할의 실행 경로

- 범위와 완료 조건이 명확한 일반 구현·테스트는 Plus 사용량을 분산하도록 아래 외부 DeepSeek 명령을 우선 고려한다. 단순 탐색·문서는 작업 규모와 API 비용을 고려해 Luna 또는 Terra로 처리할 수 있다.
- 네이티브 `spawn_agent`는 DeepSeek가 아니라 해당 TOML에 설정된 GPT 모델로 실행된다. DeepSeek 실행이 막히면 차단 사유를 보고하고 권한을 우회하지 않는다. 가능한 경우 작업의 위험도와 비용을 고려해 Luna 또는 Terra로 처리할 수 있으며 편의상 Sol 워커를 추가하지 않는다.
- 복잡한 설계·디버깅과 보안·동시성 등 고위험 리뷰·검증은 Sol 경로를 유지한다. Terra 역할 TOML은 위험도를 자동 분류하거나 Sol로 전환하지 않으므로 총괄이 직접 Sol 경로를 선택한다. 이 문서는 실행 선택 규칙이며, 네이티브 에이전트의 공급자를 자동 전환하는 기능이 아니다.

## 프로필

`deepseek-worker.config.toml`은 사용자 Codex 홈과 현재 OMX 세션의 `CODEX_HOME`에 설치한다. 각 프로필의 `model_catalog_json`은 같은 홈의 DeepSeek 공식 모델 메타데이터 `deepseek-models.json`을 가리킨다. 키는 파일에 저장하지 않고 `DEEPSEEK_API_KEY`에서 읽는다. 다른 Codex 홈을 사용하는 세션에서는 그 홈에 같은 프로필과 모델 메타데이터를 별도로 설치해야 한다.

프로젝트 모델 설정이 프로필보다 우선하므로 실행할 때 모델·추론 수준을 CLI에 명시한다. 현재 Windows 설치에서는 PowerShell 실행 정책에 걸리는 `codex.ps1` 대신 `codex.cmd`를 사용한다.

```powershell
# 탐색: 필요한 파일과 질문만 구체적으로 전달한다.
codex.cmd exec --profile deepseek-worker --model deepseek-flash -c model_reasoning_effort=high --sandbox read-only --ephemeral "지정한 파일의 호출 관계만 조사하고 근거를 보고해. 파일 수정과 커밋은 하지 마."

# 일반 구현: 작업 파일의 소유권·계약·테스트·완료 조건을 프롬프트에 포함한다.
codex.cmd exec --profile deepseek-worker --model deepseek-flash -c model_reasoning_effort=high --sandbox workspace-write --ephemeral "명시한 파일만 수정하고 지정한 테스트를 실행해. 다른 변경은 보존하고 커밋·푸시는 하지 마."
```

DeepSeek는 사용자 정책에 따라 항상 `high`로 실행한다. 비용 절감을 위해 임의로 낮추지 않는다. 외부 워커는 재귀 위임을 하지 않고 결과를 총괄에게 반환한다. DeepSeek는 코드·프롬프트를 외부 공급자로 전송하므로 비밀값과 불필요한 전체 대화를 전달하지 않는다.

## 키와 검증

`DEEPSEEK_API_KEY`는 실행 프로세스의 환경변수로 제공한다. 키를 채팅·Git·명령 인수·프로필에 기록하지 않는다. 사용자 환경변수로 등록했다면 새 Codex 세션을 시작해야 기존 프로세스에도 전달된다.

이미 실행 중인 PowerShell에서 사용자 환경변수를 읽어 자식 Codex 프로세스에 전달할 수도 있다. 값은 출력하지 않는다.

```powershell
$env:DEEPSEEK_API_KEY = [Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY', 'User')
```

호출 결과가 `402 Insufficient Balance`이면 DeepSeek 계정 잔액을 충전한 후 다시 검증한다. 이 상태에서는 모델 응답·도구 실행 검증이 완료된 것이 아니다.

현재 Windows 별도 CLI의 `read-only` 실행에서는 파일 조회 명령이 `blocked by policy`로 차단된 사례가 있다. 텍스트 응답 성공을 파일 탐색·구현 가능의 증거로 삼지 않는다. 같은 차단을 반복 재시도하거나 권한을 높이지 말고, 해당 작업의 미검증·실행 불가 상태를 보고한다.

키가 없는 상태에서는 TOML 문법·역할 매핑과 누락 키 오류까지만 검사할 수 있다. 실제 모델 호출·도구 실행·호환성·비용은 API 키 등록 후 별도 검증해야 하며, 검증 전에는 DeepSeek 워커가 활성화됐다고 보고하지 않는다.

공식 문서:
- https://learn.chatgpt.com/docs/agent-configuration/subagents
- https://learn.chatgpt.com/docs/config-file/config-advanced
- https://api-docs.deepseek.com/quick_start/agent_integrations/codex/
