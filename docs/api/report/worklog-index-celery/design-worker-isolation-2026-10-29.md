# Celery Worker 분리 설계

## 목적과 범위

일반 AI 작업과 LightRAG 색인이 하나의 Celery 실행 자리를 공유해 서로 대기하는 문제를 줄인다. 로컬과 배포 환경에서 일반 AI와 색인을 각각 전용 Worker가 처리한다. 사용자 요청에 따라 일반 AI 큐의 새 이름은 `ai_general`로 한다.

이 변경은 Outbox 통합, AI 작업의 의미 변경, 색인 병렬화, Redis 분리 또는 실제 운영 배포를 포함하지 않는다.

## 검토한 방안

1. 기존 `celery`, `worklog_index` 큐를 유지하고 Worker만 분리한다. 변경과 전환이 간단하지만 일반 AI 큐의 이름이 목적을 드러내지 않는다.
2. **선택: 새 `ai_general` 큐와 기존 `worklog_index` 큐에 전용 Worker를 둔다.** 목적이 명확하지만 기존 `celery` 큐와 되돌리기 절차를 함께 관리해야 한다.

## 실행 구조

```text
file.summary, worklog.summary, worklog.tagging, worklog.embedding
                       │
                       ▼
            ai_general 큐 ──────→ 일반 AI Worker (concurrency=1)

worklog.light_index, worklog.light_index_result
                       │
                       ▼
          worklog_index 큐 ─────→ 색인 Worker (인스턴스 1개, concurrency=1)
```

명시적으로 라우팅하지 않은 일반 작업은 기본 큐 `ai_general`을 사용한다. 색인 두 작업의 기존 라우팅과 명시적인 `worklog_index` 발행은 유지한다. 일반 Worker는 `ai_general`만 소비하고, 색인 Worker는 `worklog_index`만 소비한다. 일반 Worker의 동시 실행 수도 우선 1로 유지한다. 이후 변경은 별도 성능 측정과 결정이 필요하다.

색인 작업의 `asyncio.Lock`은 프로세스와 이벤트 루프 범위이므로 색인 Worker 복제나 concurrency 증가로 분산 락을 대신할 수 없다. 별도의 직접 색인 실행 경로와 다른 배포 인스턴스도 색인 단일 실행 주체를 보장하는지 운영에서 확인해야 한다. Worker 분리는 저장소 전체의 동시성 안전성을 새로 보장하지 않는다.

사용자 결정: 태그 생성 여부를 기다리지 않고 색인한다. 태그 콜백으로 재색인을 추가 요청하지 않는다. 색인이 태그 저장보다 먼저 원본을 읽으면 확정 태그 관계가 다음 재색인까지 누락될 수 있으며, Worker 분리로 기존 경합이 더 자주 나타날 수 있다는 점을 수용한다.

## 배포 및 기존 작업 전환

- `infra/compose.local.yml`과 `infra/compose.deploy.yml`에 두 서비스와 서로 겹치지 않는 큐 구독을 반영한다. 동일 AI 이미지와 필요한 환경변수, 저장소 접근, 색인 결과 디렉터리 공유 볼륨을 유지한다. Worker 이름은 서로 구분한다.
- `infra/scripts/remote-deploy.sh`가 서비스 목록을 명시하므로 `pull`, 기존 애플리케이션 컨테이너 정리, `up` 모두에 두 Worker를 포함한다. 관련 배포 안내와 CI 검증도 점검한다.
- 사용자 확인에 따라 배포 전 단계에서 기존 `celery` 큐 호환 소비를 제거한다. 이후 이전 버전의 대기·재시도 작업이 존재한다면 별도 소비자가 필요하다.
- 배포·롤백 시 `ai_general`에 미처리 작업이 남을 수 있다. 구버전이 해당 큐를 소비하지 않으므로 롤백 때 `ai_general`을 소비할 호환 Worker를 유지하거나 안전하게 처리한 뒤 종료한다. 운영 큐·Redis 데이터를 삭제하거나 수동 이동하지 않는다.
- 기존 배포 스크립트가 컨테이너를 교체하는 동안 작업이 중단될 수 있다. Celery 승인·재전달은 설정과 작업의 멱등성에 의존하므로 중복 실행과 복구를 검증하고, 무중단이나 정확히 한 번 실행을 약속하지 않는다.

## 검증 및 완료 조건

1. Celery 라우팅 검증: 파일 요약·업무 요약·태그·임베딩은 `ai_general`, 색인·결과 작업은 `worklog_index`로 전달된다.
2. Compose 렌더와 배포 스크립트 검증: 두 Worker의 구독 큐가 의도대로 다르고 두 서비스가 배포 대상에 포함된다. 설정값과 비밀정보를 로그에 노출하지 않는다.
3. 격리된 Redis/Celery 통합 검증: 일반 작업이 `ai_general`에서 처리되고 색인 전용 Worker는 색인 큐만 소비한다.
4. 일반 Worker의 느린 작업을 실행하는 동안 색인 Worker가 독립적으로 작업을 시작하고, 반대로 색인 Worker가 점유된 동안 일반 작업이 시작되는지 테스트한다. 실제 외부 AI 호출이나 운영 저장소를 사용하지 않는다.
5. 기존 색인 테스트와 관련 검증을 실행하고, 배포 절차의 기존 큐 처리·롤백 조건을 문서화한다. 검증 불가능한 환경이나 실제 운영 Worker 수와 대기량은 별도로 보고한다.

## 트레이드오프

Worker 프로세스가 하나 늘어 메모리와 운영 대상이 증가한다. 큐와 실행 자리는 분리되지만 Redis, DB, 외부 모델 API의 자원과 호출 한도는 공유한다.
