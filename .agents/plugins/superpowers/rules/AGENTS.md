# Superpowers Plugin — Engineering Discipline Rules

> **핵심 철학**: "코딩 전에 계획하고, 배포 전에 독립 검증한다."

이 플러그인이 활성화된 세션의 모든 에이전트는 다음 워크플로우를 강제합니다.

## 1. 계획 및 스펙 우선 (Spec-Driven)
- 무작정 코딩을 시작하지 않습니다.
- 구현 전에 반드시 `Code Read`로 기존 코드를 분석하고, `Planner`가 명확한 작업 단위(TODO)와 입출력 스펙을 확정한 후 작업을 시작합니다.

## 2. 테스트 주도 및 엣지 케이스 방어 (TDD Mindset)
- 정상 케이스(Happy Path)뿐만 아니라, 예외 상황(4xx/5xx 에러, 빈 값, 타임아웃 등)에 대한 테스트 코드를 함께 작성합니다.

## 3. 독립 검증 및 자체 승인 차단 (Independent Verification)
- 구현 에이전트는 절대 자신의 작업물을 최종 완료로 승인하지 않습니다.
- 반드시 `qa_verifier`가 자동화 테스트(`./gradlew test`, `pytest`)를 돌려 100% 통과했을 때만 승인합니다.
