# Web E2E 테스트 실행 가이드 (Playwright)

> **핵심 안내**: `web/e2e` 테스트는 가상 Mock 데이터가 아닌 **실제 구동 중인 백엔드 API 서버를 직접 호출**하여 브라우저 수준의 종단 간(End-to-End) 사용자 경험과 권한 흐름을 검증합니다.

---

## 🔑 1. 필수 환경변수 설정

E2E 실행 전 로컬 `.env` 파일 또는 CI Secret에 아래 환경변수가 반드시 설정되어 있어야 합니다:

```bash
# 브라우저가 호출할 API 서버 Base URL
NEXT_PUBLIC_API_BASE_URL="https://k14s209.p.ssafy.io:8443/api"

# 권한별 테스트 계정 인증정보 (기본 iBank 더미 계정 규격)
WEB_E2E_DIRECTOR_EMAIL="director@example.com"
WEB_E2E_DIRECTOR_PASSWORD="******"

WEB_E2E_DEPT_HEAD_EMAIL="dept_head@example.com"
WEB_E2E_DEPT_HEAD_PASSWORD="******"

WEB_E2E_TEAM_LEAD_EMAIL="team_lead@example.com"
WEB_E2E_TEAM_LEAD_PASSWORD="******"

WEB_E2E_MEMBER_EMAIL="member@example.com"
WEB_E2E_MEMBER_PASSWORD="******"
```

> [!CAUTION]
> 비밀번호 등 실제 인증 자격증명은 저장소에 절대 커밋하지 마세요.

---

## 🛡️ 2. 데이터 격리 및 테스트 정책

- **실제 서버 상태 전제**: 인위적인 Mock fixture 문자열에 의존하지 않고 실제 백엔드 응답을 검증합니다.
- **파괴적 작업 금지**: 기존 조직/팀/업무일지 수정, 데이터 병합, 일괄 읽음 처리 등 원격 서버 데이터를 오염시키는 동작은 기본 E2E에서 일체 수행하지 않습니다.
- **빈 상태(Empty State) 허용**: 서버 데이터가 비어 있을 수 있는 목록/다운로드/병합 후보 항목은 empty state 처리 또는 `test.skip`으로 격리합니다.
- **신규 Mutation 규칙**: 데이터 생성이 불가피한 테스트의 경우 `E2E_${Date.now()}_${test.info().parallelIndex}` 형태의 고유 마커를 부여하고 완료 후 즉시 cleanup하도록 작성합니다.

---

## 🚀 3. 로컬 테스트 실행

Playwright가 로컬 Next.js 개발 서버(`127.0.0.1`)를 자동으로 실행하며, 실행 중에만 `NEXT_PUBLIC_E2E_AUTH_BOOTSTRAP=1`이 자동 주입됩니다.

```bash
cd web

# 1. 헤드리스 모드로 Chromium E2E 전체 실행
pnpm e2e --project=chromium

# 2. 대화형 UI Mode 실행 (Action Snapshot 확인용 --trace on 자동 적용)
pnpm e2e:ui
```

*(※ 이미 실행 중인 별도 개발 서버를 사용하려면 `WEB_E2E_BASE_URL` 환경변수를 지정하세요.)*

---

## 🔍 4. UI Mode 및 Trace 디버깅 팁

### Q. UI Mode에서 화면 오른쪽 `After` 스냅샷이 `about:blank`로 보일 때
1. UI Mode 창을 닫고 `pnpm e2e:ui`로 다시 실행했는지 확인합니다.
2. 왼쪽 테스트 목록 전체나 가운데 `Passed` 요약 행이 아니라, **`page.goto`, `click`, `expect` 같은 개별 Action 항목을 직접 클릭**하세요.

### 📊 HTML Trace Report 생성 및 분석
개별 액션 스냅샷을 브라우저 리포트로 정밀 분석하고 싶을 때:

```bash
cd web

# Trace를 포함하여 테스트 실행
pnpm e2e:trace --project=chromium

# 브라우저에서 인터랙티브 HTML Report 열기
pnpm e2e:report
```

- **Trace 용량 최적화**: 디스크 용량을 아끼려면 실패한 테스트에만 trace를 남기는 `WEB_E2E_TRACE=retain-on-failure pnpm e2e --project=chromium`을 사용합니다.
- **디버깅 규칙**: 커밋되는 코드에는 `page.pause()`를 절대 남기지 않으며, 로컬 디버깅 시에만 `pnpm exec playwright test --debug` 또는 임시 `page.pause()`를 사용합니다.
