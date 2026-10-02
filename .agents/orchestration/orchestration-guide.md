# AX-WMS Antigravity 5단계 오케스트레이션 가이드

본 문서는 Codex/OMX 환경과 완벽히 격리된 **Antigravity 전용 멀티 에이전트 협업 체계**를 정의합니다.

---

## 1. 5단계 파이프라인 워크플로우

```
[사용자]
   │
   ▼
[1. 대화 에이전트] (Coordinator / 메인 세션) ── Model: "flash"
   │ • 사용자와의 자연어 소통, 요구사항 접수, 최종 브리핑
   │
   ▼ (요구사항 전달)
[2. 코드 읽기 에이전트] (agy_code_reader) ─────── Model: "flash" (Pure Read-Only)
   │ • 【수정 권한 없음】 기존 코드, ADR, 컨벤션, 재사용 함수 탐색
   │ • 현재 구현 현황 및 분석 리포트 작성
   │
   ▼ (코드 분석 리포트 인계)
[3. 기획 에이전트] (agy_planner) ─────────────── Model: "pro" (Frontier급)
   │ • 요구사항 분석 및 세부 작업(TODO) 분해
   │ • Spring Boot ↔ FastAPI API 계약(DTO/Pydantic) 사전 정의
   │ • Ponytail 플러그인(YAGNI, 미니멀리즘) 기반의 단순한 계획 수립
   │
   ▼ (계획 및 스펙 인계)
[4. 구현 에이전트] (agy_executor) ────────────── Model: "flash" (High Speed)
   │ • 계획에 따라 Spring Boot / FastAPI 코드 구현
   │ • 로컬 단위 테스트 확인 (자체 승인 금지)
   │
   ▼ (작업물 인계)
[5. 검증 에이전트] (agy_verifier) ────────────── Model: "pro" (엄격한 비판)
   │ • Superpowers 플러그인 기반 독립 검증
   │ • 전체 테스트(`./gradlew test`, `pytest`) 및 API 계약 일치 검증
   │ • 통과 시 최종 승인 보고 / 결함 시 executor에 재작업 반려
   │
   ▼ (최종 승인)
[1. 대화 에이전트] ──► [사용자에게 완료 보고]
```

---

## 2. 모델 라우팅 요약

| 역할 | 서브에이전트 이름 | 권장 모델 | 권한 |
|---|---|---|---|
| **대화 에이전트** | 메인 세션 (Coordinator) | `flash` | 전역 인터랙션 |
| **코드 읽기** | `agy_code_reader` | `flash` | **Pure Read-Only** (쓰기/명령 차단) |
| **기획/설계** | `agy_planner` | `pro` | 읽기 + 계획 작성 + 에이전트 지휘 |
| **코드 구현** | `agy_executor` | `flash` | 읽기 + 코드 작성 + 로컬 빌드 |
| **독립 검증** | `agy_verifier` | `pro` | 읽기 + 전체 테스트 실행 + 승인/반려 |

---

## 3. 활성화된 플러그인

1. **`ponytail`** (`.agents/plugins/ponytail/`):
   - "게으른 시니어 개발자" 마인드셋
   - YAGNI 강제, 기존 코드 재사용 극대화, 오버엔지니어링(AI Slop) 방지
2. **`superpowers`** (`.agents/plugins/superpowers/`):
   - 체계적인 소프트웨어 엔지니어링 규율
   - 스펙 주도 개발, TDD, 독립 Verifier 검증 강제
