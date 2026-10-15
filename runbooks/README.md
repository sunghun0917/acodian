# Runbooks — 운영 장애 대응 절차 가이드

> **핵심 목적**: 운영 환경 장애 발생 시 신속하고 표준화된 복구를 위한 모듈별 절차서 모음입니다. "장애가 발생했을 때 어떻게 진단하고 복구하는가"를 단계별로 명확하게 기록합니다.

---

## 📂 모듈별 런북 인덱스

| 대상 모듈 | 런북 인덱스 경로 | 상태 |
| :--- | :--- | :---: |
| **API (Spring Boot)** | [`api/README.md`](api/README.md) | 인덱스 정의 완료 (개별 런북 작성 예정) |
| **Infra (인프라/배포)** | [`infra/README.md`](infra/README.md) | CI 인증 자격증명 트러블슈팅 런북 1건 등록 |
| **Web (Next.js)** | `runbooks/web/README.md` | 신규 장애 발생 시 신설 예정 |
| **AI (FastAPI)** | `runbooks/ai/README.md` | 신규 장애 발생 시 신설 예정 |
| **Nginx (게이트웨이)** | `runbooks/nginx/README.md` | 신규 장애 발생 시 신설 예정 |

---

## 📝 새 Runbook 작성 규약

1. **사후 기록 원칙**: 장애가 **실제 발생한 후** 문서를 추가합니다 (사전 추측으로 불필요한 문서를 작성하지 않음).
2. **명명 규칙**: `runbooks/{module}/{slug}.md` 형태로 저장하며, `slug`는 증상을 요약한 kebab-case로 작성합니다.
   - *예시*: `runbooks/api/deploy-failure.md`, `runbooks/api/db-connection-lost.md`
3. **인덱스 동기화**: 신규 파일 추가 시 해당 모듈의 `README.md` 인덱스 표에 반드시 링크를 추가합니다.
4. **신규 디렉터리 생성**: 새로운 모듈(Web/AI/Nginx 등)의 첫 런북이 생성되는 시점에 해당 하위 디렉터리와 인덱스 `README.md`를 함께 개설합니다.

---

## 📋 Runbook 표준 5단계 템플릿

모든 개별 런북은 아래 5개 섹션 규격을 준수하여 작성합니다:

1. **증상 (Symptoms)**: 사용자 화면이나 모니터링 알림에 어떻게 표출되는가
2. **1차 점검 (Triage)**: 5분 이내에 즉시 확인해야 할 핵심 3가지
3. **복구 절차 (Recovery)**: 순차적으로 실행할 구체적인 복구 명령어 및 단계
4. **근본 원인 분류 (Root Cause Categories)**: 재발 방지를 위한 장애 원인 분석
5. **후속 조치 (Follow-up)**: 코드 수정, 알림 보강, 정책 반영 등 재발 방지 액션

---

## 🔗 관련 정책 및 참조 문서

- **jOOQ 코드 생성 운영 정책**: [`docs/api/jooq-codegen-policy.md`](../docs/api/jooq-codegen-policy.md)
- **전체 문서화 관리 전략**: [`docs/docs-strategy.md`](../docs/docs-strategy.md)
