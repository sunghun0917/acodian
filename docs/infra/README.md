# 인프라 문서 안내

배포, CI/CD, Nginx 라우팅 관련 문서를 관리한다. 코드·설정 작업 규칙은 [infra/AGENTS.md](../../infra/AGENTS.md)를 먼저 읽는다.

## 규칙·운영 가이드

- [설계 결정 기록](adr.yaml) · [코드 컨벤션](code-convention.yaml)
- [EC2·GitLab CI/CD 가이드](ec2-gitlab-cicd-guide.md)
- [Web·API 라우팅](web-api-routing.md)
- [Nginx 표준 가이드 (Word 참고 자료)](AX-WMS_Nginx_표준가이드.docx)

## 검토·과거 기록

- [유예된 결정·후속 검토 항목](pending-decisions.md): 작성 시점의 기록으로, 실행 전 현재 상태를 확인한다.
- [레거시 ADR](adr.legacy.yaml): 과거 기록을 보존하며, 현재 결정은 `adr.yaml`을 확인한다.

실제 배포 절차는 [포팅 매뉴얼](../../exec/포팅_매뉴얼.md)도 참고한다. [전체 문서 안내](../README.md)
