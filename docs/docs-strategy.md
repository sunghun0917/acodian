# 문서 구조 및 유지 규칙

문서의 진입점은 [docs/README.md](README.md)다. 업무 영역을 먼저 구분하고, 영역 안에서 규칙·명세·보고서·평가 자료를 분리한다.

## 디렉터리 구조

```text
docs/
├── README.md                # 전체 문서 목차
├── architecture/            # 통합 아키텍처 가이드 (Markdown 기준, Word 참고)
├── database/                # ERD·데이터베이스 설계 초안
├── api/                     # Spring Boot 규칙·개발 가이드
│   ├── spec/                # 도메인별 API 명세와 인덱스
│   └── report/              # 구현 검증 보고서·측정 원본
├── web/                     # Next.js 규칙·디자인·협업 가이드
│   └── inho/                # 기존 담당자별 작업·검토 기록
├── ai/                      # FastAPI·LightRAG 규칙·가이드·조사
│   ├── evaluation/          # 버전별 평가 데이터·스크립트·실행 결과
│   └── report/              # 분석 보고서·흐름도·측정 원본
├── infra/                   # 배포·CI/CD·라우팅·Nginx 가이드
├── shared/                  # 온보딩·로컬 실행·보안·공통 용어
├── superpowers/
│   ├── specs/               # 작업별 설계서
│   └── plans/               # 작업별 구현 계획
├── images/                  # 공용 이미지
├── archive/                 # 현재 기술 스택의 기준이 아닌 과거 문서
├── docs-strategy.md         # 이 문서
├── skills-reference.md      # 스킬 빠른 참조
└── guidance-schema.md       # OMX가 참조하는 스키마 계약 (경로 유지)
```

## 기준 문서

- 전체 아키텍처: [Markdown 가이드](architecture/AX-WMS_기획서_아키텍처가이드.md)
- API 규칙: [ADR](api/adr.yaml) · [컨벤션](api/code-convention.yaml)
- Web 규칙: [ADR](web/adr.yaml) · [컨벤션](web/code-convention.yaml)
- AI 규칙: [ADR](ai/adr.yaml) · [컨벤션](ai/code-convention.yaml)
- 인프라 규칙: [ADR](infra/adr.yaml) · [컨벤션](infra/code-convention.yaml)

각 영역의 규칙은 해당 영역 파일 한 곳에서 관리한다. 공통 규칙은 루트 `AGENTS.md`, 코드 작업의 추가 규칙은 각 코드 영역의 `AGENTS.md`를 따른다. 컨벤션 미러나 다른 영역 규칙의 사본을 만들지 않는다.

Word 가이드는 참고 자료다. 현재 API는 Spring Boot이므로 NestJS 가이드는 `archive/`에 보존한다. 날짜가 있는 설계·계획·작업 기록과 평가 보고서는 당시 맥락을 보존하며, 현재 구현 상태나 운영 규칙으로 자동 승격하지 않는다.

## 추가·이동 규칙

1. 새 문서는 담당 영역에 추가한다. 여러 영역에서 함께 사용하는 내용만 `shared/`에 둔다.
2. API 명세는 `api/spec/`, 보고서와 측정 근거는 해당 영역의 `report/`에 둔다. AI 평가 자료는 `ai/evaluation/`의 기존 버전 구조를 따른다.
3. 영역 안내와 전체 목차에서 찾을 수 있도록 상대 링크를 추가한다. 폴더는 실제로 분류할 자료가 있을 때만 만든다.
4. 문서 이동 시 Markdown 링크뿐 아니라 코드 블록·명령·설정·`AGENTS.md`·`CLAUDE.md` 등의 경로 참조도 확인한다. 실행 스크립트가 참조하는 자료는 탐색 편의를 위해 옮기지 않는다.
5. 파일명·본문은 불필요하게 바꾸지 않는다. 과거 문서나 중복 후보도 확인 없이 삭제·병합하지 않는다.
6. 평가 데이터, manifest, 실행 결과는 정리 목적으로 재생성하지 않는다. SQL은 분류 대상이지 실행 대상이 아니다.
7. `.omx/` 등 자동 생성 영역은 수동 정리 대상에서 제외한다.

## 점검 체크리스트

- [ ] 전체 목차와 영역 안내에서 문서를 찾을 수 있는가
- [ ] 링크와 경로 참조가 실제 파일을 가리키는가
- [ ] 영역별 ADR·컨벤션의 기준 경로가 하나로 유지되는가
- [ ] 현재 규칙, 참고 가이드, 초안, 과거 기록이 구분되는가
- [ ] 이동 전후 파일 내용과 평가 원본이 보존되는가
