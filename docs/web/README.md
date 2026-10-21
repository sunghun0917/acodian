# Web 프론트엔드 협업 가이드

> **핵심 안내**: `web/` (또는 `apps/web/`)는 실제 화면 구현 코드 영역이며, 본 디렉터리(`docs/web/`)는 **프론트엔드 개발자 간 장기적으로 유지해야 할 협업 규칙과 담당 범위**를 관리하는 문서 영역입니다.

---

## 📂 폴더 구조

```text
docs/web/
├── README.md                 # 본 문서 (협업 가이드 개요)
├── adr.yaml                  # 설계 결정 기록
├── code-convention.yaml      # 코드 컨벤션
├── collaboration-rules.md    # 프론트엔드 코딩 및 협업 최우선 규칙
├── ownership.md              # 화면 및 기능별 담당자 소유권(Ownership) 정의
├── design-tokens.md          # 디자인 토큰
├── AX-WMS_NextJS_표준가이드.docx # 기존 Word 참고 가이드
├── work-summary.md           # 초기 구성 작업 기록
└── inho/                     # 기존 작업·검토 기록
```

---

## 📌 사용 원칙

1. **소유권 명시**: 담당 화면 및 기능 소유권은 [`ownership.md`](ownership.md)에 명확히 정의합니다.
2. **단기성 메모 분리**: 새 작업의 일시적인 진행 상황, 인수인계 메모는 문서 파일 대신 Issue 또는 PR 본문에 기록합니다. 기존 `inho/` 기록은 당시 맥락을 보존하며 현재 규칙과 구분합니다.
3. **코드 디렉터리 분리**: 실행 코드와 직접 관련 없는 협업 문서는 소스코드 디렉터리 내부에 두지 않고 본 디렉터리(`docs/web/`)에 보관합니다.

---

## 🚀 빠른 참조 링크

- **공통 컴포넌트 및 코드 공유**: `web/src/app/_common`
- **협업 문서 베이스**: `docs/web/`
- **최우선 프론트엔드 규칙**: [`collaboration-rules.md`](collaboration-rules.md)
- **화면별 담당자 매핑**: [`ownership.md`](ownership.md)
- **설계 결정 / 코드 컨벤션**: [ADR](adr.yaml) · [컨벤션](code-convention.yaml)
- **디자인 토큰**: [design-tokens.md](design-tokens.md)
- **Next.js 표준 가이드 (Word 참고 자료)**: [AX-WMS_NextJS_표준가이드.docx](AX-WMS_NextJS_표준가이드.docx)
- **초기 구성 작업 기록 / 기존 검토 메모**: [work-summary.md](work-summary.md) · [inho/](inho/)
- **코드 작업 규칙 / 전체 문서 안내**: [web/AGENTS.md](../../web/AGENTS.md) · [docs/README.md](../README.md)
