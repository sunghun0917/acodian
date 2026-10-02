# 프로젝트 공통 Git 및 보안 원칙

본 문서는 Codex, Antigravity를 포함한 모든 AI 에이전트와 개발자가 공통으로 준수해야 하는 최상위 원칙입니다.

## 1. Git 커밋 및 상태 변경 원칙
- **사용자의 명시적 지시 없는 커밋 절대 금지**: `git commit`, `git commit --amend`, `git push`, `git rebase`, `git reset --hard` 등 모든 공유/파괴적 Git 작업은 사용자의 명시적인 지시("커밋해줘", "commit" 등)가 있을 때만 실행합니다.
- **작업 완료와 커밋의 분리**: 작업 완료 보고는 코드 변경 내역을 요약하여 전달하는 것이며, 커밋 실행 여부는 항상 사용자가 최종 결정합니다.
- **Conventional Commits 준수**: 커밋 시 `feat(api): ...`, `fix(ai): ...`, `docs: ...` 형태의 명확한 컨벤션을 사용합니다.

## 2. 보안 및 기밀 보호
- `.env` 및 시크릿 키, 인증 토큰(`GEMINI_API_KEY`, DB 비밀번호 등)은 절대 소스코드나 Git 추적 파일에 직접 작성하지 않습니다.
- SQL Injection, XSS, CSRF 등 기본적인 웹/API 보안 취약점이 발생하지 않도록 방어 로직을 준수합니다.
