# Infra Runbooks — 인프라 장애 대응 가이드

> **핵심 안내**: 인프라 및 배포 환경의 운영 장애 대응 절차 인덱스입니다. 전반적인 런북 작성 규약은 최상위 [`../README.md`](../README.md)를 준수합니다.

---

## 📂 장애 대응 런북 인덱스

| 런북 파일 | 주요 장애 증상 | 관련 표준 및 근거 |
| :--- | :--- | :--- |
| **[`ci-auth-troubleshoot.md`](ci-auth-troubleshoot.md)** | CI 파이프라인의 인증 자격증명(`GHCR_USER`, `GHCR_TOKEN`, `SSH_PRIVATE_KEY`)이 런타임에 주입되지 않거나 형식이 손상되어 `docker login` 또는 `ssh` 접속이 실패하는 장애 | ADR-007, ADR-013<br>OPS-006, OPS-012<br>OPS-013, OPS-015 |
