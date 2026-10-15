# API Runbooks — Spring Boot 운영 장애 대응 가이드

> **핵심 안내**: Spring Boot `api` 모듈의 장애 유형별 긴급 대응 절차 인덱스입니다. 실제 운영 장애가 발생했을 때 신속한 복구를 위해 작성하며, 사전 예방 정책은 상위 정책 문서를 참조합니다.

---

## 📂 장애 대응 절차 인덱스

| 장애 유형 | 런북 파일 | 상태 | 핵심 대응 내용 |
| :--- | :--- | :---: | :--- |
| **배포 실패** | `deploy-failure.md` | *미작성* | JAR 빌드 실패, 환경변수 누락, 컨테이너 기동 실패 대응 |
| **DB 연결 불가** | `db-connection-lost.md` | *미작성* | PostgreSQL 커넥션 풀 고갈, 타임아웃, 네트워크 단절 대응 |
| **Flyway 마이그레이션 실패** | `flyway-migration-failure.md` | *미작성* | DDL 충돌, 체크섬 불일치, 롤백 및 복구 절차 |

---

## 📋 Runbook 표준 작성 규격

신규 런북 작성 시 아래 5개 필수 섹션을 유지합니다:

1. **증상 (Symptoms)**: 사용자 에러 코드(5xx) 및 모니터링 알림 양상
2. **1차 점검 (Triage)**: 로그 확인 위치, 헬스체크 엔드포인트 등 5분 내 확인할 항목
3. **복구 절차 (Recovery)**: 서비스 롤백, 파드 재기동, 커넥션 정리 등 단계별 명령어
4. **근본 원인 분류 (Root Cause Categories)**: 코드 결함, 인프라 부하, 설정 오류 등
5. **후속 조치 (Follow-up)**: 단위 테스트 추가, 인덱스 최적화, 재발 방지 액션

---

## 🔗 관련 정책 문서

- [`docs/api/jooq-codegen-policy.md`](../../docs/api/jooq-codegen-policy.md): jOOQ 코드 생성 운영 모델 및 빌드 정책
