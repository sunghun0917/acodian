# 권한 범위별 RAG 2-Stage Cache 설계

## 목표와 경계

- `/api/.../worklogs/search/semantic`에서 확인한 접근 가능 팀 집합을 AI 검색과 Redis 캐시 모두의 보안 범위로 사용한다.
- 비스트리밍 LightRAG v3 질의에 Exact(SHA-256) → Semantic(코사인 유사도 0.90 이상) → RAG 순서를 적용한다. 성공 응답만 24시간 저장한다.
- 4번 항목의 namespace version을 키 형식에 포함하지만, 문서 변경 시 `INCR`/무효화는 이번 범위에서 구현하지 않는다.
- 캐시와 권한 검사 실패를 구분한다. Redis 장애는 기존 RAG로 폴백할 수 있지만, 권한 범위를 확인할 수 없으면 실패로 닫는다.
- 기존 SSE 경로는 캐시 대상에서 제외하되 동일한 권한 경계를 적용한다.

## 선택한 설계

1. Spring API가 로그인 principal로 `allowedTeamIds`를 계산한다. `null`은 전체 권한을 가진 내부 호출의 `ALL`, 빈 목록은 즉시 빈 응답이다. AI 질의 경로는 내부 공유 토큰으로 보호해 클라이언트가 팀 목록을 위조할 수 없게 한다.
2. 제한 범위는 LightRAG `aquery_data`로 후보만 회수하고, `file_path`/`full_doc_id`의 업무일지 ID를 원본 DB에서 `team_id`·삭제 상태로 재검증한다. 허용된 원본 업무일지 본문만 Gemini에 전달한다. KG의 병합된 entity/relation 설명은 사용하지 않는다. `aquery_llm`을 다시 호출하면 재검색하므로 사용하지 않는다.
3. 전체 권한도 같은 후보·원본 검증 경로를 사용한다. 권한 목록의 누락과 명시적 `null`은 구별한다. 권한 없는 후보, 출처가 불명확한 후보, 삭제된 문서는 답변·references에서 제외한다.
4. 캐시 범위는 `ALL` 또는 정렬·중복 제거한 팀 ID 집합의 SHA-256이다. Exact 키는 namespace version, 범위, 질의·검색 옵션·모델 설정 서명을 포함한다. Semantic HASH는 같은 범위 TAG와 768차원 FLOAT32 embedding을 저장하고 Redis Search KNN을 범위 안에서만 실행한다. `COSINE distance <= 0.10`을 적중으로 판정한다.
5. Redis Open Source 8.10의 Search 기능을 사용한다. 로컬 개발 환경(`infra/compose.local.yml`)의 `axwms-redis`를 `redis:8.10.0-alpine`으로 기본 통합 구성하여 포트 6379에서 RedisSearch 및 Vector KNN을 서비스한다. 운영(EC2) 환경 적용 시에는 볼륨 백업 절차 후 compose.deploy.yml을 통해 적용한다.

## 대안과 제외

- 사용자 ID별 캐시: 같은 권한 사용자 간 재사용이 안 되어 적중률이 낮다.
- 팀 하나별 캐시: 여러 팀을 볼 수 있는 사용자의 종합 답변을 안전하게 구분할 수 없다.
- 팀별 LightRAG workspace: 격리는 쉽지만 색인 중복·복수 팀 질의 병합·팀 간 관계 손실이 크다.
- 캐시 키 분리만 하거나 생성 후 references만 필터: 최초 LLM 입력의 권한 누출을 막지 못한다.

## 검증 기준

- 권한 목록 순서·중복이 달라도 같은 캐시 범위를 쓰고, 다른 범위는 서로 적중하지 않는다.
- 제한 사용자의 LLM 입력·references에는 다른 팀 또는 삭제된 업무일지가 없다. 혼합 KG 설명과 타 팀 선행 업무 제목은 전달되지 않는다.
- Exact 적중 시 embedding·RAG 호출이 없고, Semantic 적중 시 RAG 호출이 없다. 임계값 미달·Redis 장애는 권한 제한된 RAG로 진행한다.
- 내부 토큰이 없거나 틀린 직접 AI 호출은 거절한다. 누락한 `allowedTeamIds`는 전체 권한으로 해석되지 않는다.
- 로컬 `compose.local.yml`의 `axwms-redis` (6379 포트) 및 실제 FastAPI HTTP 엔드포인트(`POST /ai/light/worklogs-v3/query`)에서 Redis 8.10 Search 명령과 Exact/Semantic 캐시 HTTP 네트워크 실측이 검증된다. 운영 데이터·컨테이너는 명시적 지시 전까지 변경하지 않는다.

## 알려진 한계

- 기존 LightRAG 상위 후보를 사후 권한 필터링하므로 비허용 후보가 많으면 재현율이 낮아질 수 있다. 추후 Qdrant의 팀 메타데이터 사전 필터가 필요하다.
- 무효화를 미루므로 문서 내용 변경 후 최대 24시간 이전 답변이 남을 수 있다. 권한 팀 집합 변경은 새 범위 키를 사용한다.
- Semantic 적중은 질문이 유사해도 답변이 다를 수 있다. 임계값 0.90과 품질은 별도 평가 결과로 검증해야 하며 수치를 포트폴리오 기존 측정치로 주장하지 않는다.
