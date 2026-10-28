# Celery Worker 분리 구현 계획

> 변경 이력: 사용자가 배포 전임을 확인하고 기존 `celery` 큐 호환 소비 제거를 요청했다. 아래 전환 호환 관련 단계는 당시 계획이며 현재 구성은 `ai_general`만 소비한다.

> **실행 스킬:** 구현 시 `superpowers:executing-plans` 또는 사용자가 명시적으로 선택한 경우 `superpowers:subagent-driven-development`를 사용한다. 각 단계는 체크박스로 기록한다.

**목표:** 일반 AI 작업과 LightRAG 색인을 다른 큐와 Worker에서 처리해 서로의 실행 자리를 점유하지 않도록 한다.

**구조:** 기본 큐를 `ai_general`로 변경하고 기존 `worklog_index` 라우팅을 유지한다. 일반 Worker는 전환 기간에 `ai_general,celery`를 소비하고, 색인 Worker는 `worklog_index`만 소비한다. 두 Worker는 동일 이미지를 사용한다.

**기술:** Python 3.12, Celery 5.6.3, Redis, Docker Compose, POSIX shell, pytest.

**설계:** [Celery Worker 분리 설계](../specs/2026-10-29-celery-worker-isolation-design.md)

## 공통 제약

- 색인 Worker 인스턴스 1개, `concurrency=1`; 일반 Worker도 우선 `concurrency=1`.
- 색인 락은 프로세스·이벤트 루프 범위다. 별도 직접 실행 경로와 다른 배포 인스턴스까지 안전하다고 가정하지 않는다.
- 기존 `celery` 큐 소비는 이번 변경에서 제거하지 않는다. 운영 Redis의 큐 삭제·메시지 이동을 하지 않는다.
- 공유 색인 결과 디렉터리·이미지·환경·접근 권한을 유지한다. 비밀값을 로그나 테스트 출력에 남기지 않는다.
- Outbox 통합, AI 로직 변경, 색인 병렬화, 운영 배포는 범위 밖이다. 명시적 지시 없이 커밋하지 않는다.
- 작업 전 `ai/AGENTS.md`, `infra/AGENTS.md` 및 그 참조 문서를 적용하고, 기존 사용자 변경을 보존한다.

## 파일과 책임

- `ai/app/task/celery_app.py`: 기본 큐와 기존 색인 작업 라우팅.
- `ai/tests/test_celery_routing.py`: 실제 Celery 라우터의 기본·명시 라우팅 계약.
- `ai/tests/test_celery_worker_isolation.py`: 격리 Redis에서 두 Worker의 큐 소비와 동시 실행 검증.
- `infra/compose.local.yml`, `infra/compose.deploy.yml`: 두 Worker 서비스와 큐, 이미지·환경·볼륨 설정.
- `infra/scripts/remote-deploy.sh`: 두 Worker의 pull·교체·시작.
- `docs/infra/ec2-gitlab-cicd-guide.md`, `docs/ai/lightrag-postgres-setup.md`: 배포 대상, 기존 큐 및 롤백 절차.
- `ai/AGENTS.md`: 기존 기능 매트릭스에 변경 대상 행을 추가하는 프로젝트 규칙을 확인해 적용.

## 검토 초점

1. 라우팅 예외·기본 큐: 파일 요약·업무 요약·태그·임베딩이 `ai_general`, 두 색인 작업이 `worklog_index`에 도착하는지 작업 1에서 검증.
2. 전환 중 오래된 메시지: 기존 `celery` 메시지도 새 일반 Worker가 처리하며 색인 Worker가 가져가지 않는지 작업 3에서 검증.
3. 한 Worker가 점유된 경우: 양쪽 방향의 반대 큐가 실제로 진행되는지 작업 3에서 검증.
4. Worker 누락·공유 저장소: 로컬·배포 Compose 렌더에서 큐, 동시성, 이미지와 결과 볼륨 및 배포 명령 세 곳을 작업 2에서 검증.
5. 롤백과 재전달: 기존 큐가 비어도 예약·선점 메시지가 있을 수 있고 롤백 후 `ai_general` 메시지가 남을 수 있음을 작업 2의 문서 및 전환 점검에서 검증한다. 정확히 한 번 실행은 가정하지 않는다.

---

### 작업 1: Celery 큐 라우팅

**파일:** 수정 `ai/app/task/celery_app.py`; 생성 `ai/tests/test_celery_routing.py`.

**인터페이스:** 기존 `celery_app` 설정을 사용한다. `task_default_queue`는 `ai_general`; `worklog.light_index`, `worklog.light_index_result`는 `worklog_index`를 유지한다.

- [ ] 실제 `celery_app.amqp.router.route`를 호출하는 테스트를 작성한다. `file.summary`, `worklog.summary`, `worklog.tagging`, `worklog.embedding`이 각각 `ai_general`을, 두 색인 작업이 각각 `worklog_index`를 반환해야 한다. 명시적 `queue="worklog_index"` 발행 경로가 유지되는지도 확인한다.
- [ ] `cd ai`에서 `.venv/Scripts/python.exe -m pytest tests/test_celery_routing.py -q`를 실행해 기본 큐가 `celery`여서 실패하는지 확인한다. POSIX에서는 `.venv/bin/python`을 사용한다.
- [ ] `celery_app.conf.update`에 `task_default_queue="ai_general"`을 설정하고 기존 색인 라우팅을 변경하지 않는다.
- [ ] 같은 테스트와 `tests/test_light_worklog_tasks.py`, `tests/test_tagging_tasks.py`를 실행해 통과 결과를 확인한다.

### 작업 2: 로컬·배포 Worker와 전환 절차

**파일:** 수정 `infra/compose.local.yml`, `infra/compose.deploy.yml`, `infra/scripts/remote-deploy.sh`, `docs/infra/ec2-gitlab-cicd-guide.md`, `docs/ai/lightrag-postgres-setup.md`; 필요 시 관련 검증 테스트 추가.

**인터페이스:** `ai_worker`는 일반 Worker로 유지하고 `ai_index_worker`를 추가한다. 각각 `--queues=ai_general,celery`, `--queues=worklog_index`를 사용하며 `--concurrency=1`이다. 노드 이름은 각각 `general@%h`, `index@%h`로 구분한다.

- [ ] Compose 렌더 결과의 서비스·명령 검증을 먼저 작성한다. 두 Worker의 큐가 겹치지 않고 각 동시성이 1이며 같은 AI 이미지와 `/app/data` 공유 볼륨을 쓰는지 확인한다. 배포 스크립트의 pull·rm·up에 양쪽 서비스가 포함되는지도 검증한다. 이 검증은 현재 구성에서 실패해야 한다.
- [ ] 로컬·배포 Compose에 `ai_index_worker`를 추가하고 `ai_worker`의 큐를 변경한다. 기존 worker 설정을 재사용해 환경·의존성·볼륨의 차이를 만들지 않고 색인 결과 전달 경로를 보존한다.
- [ ] `infra/scripts/remote-deploy.sh`의 `pull`, `rm -sf`, `up -d --remove-orphans` 각각에 `ai_index_worker`를 포함한다. 기존 상태 저장 서비스나 볼륨을 제거하지 않는다.
- [ ] `docker compose --env-file infra/.env.example -f infra/compose.deploy.yml config --quiet`과 로컬 Compose 렌더를 실행한다. 필수 렌더 변수 또는 로컬 env 파일이 없으면 실제 비밀값 대신 격리된 임시 예시 값을 사용하고 렌더된 환경값은 출력하지 않는다. `sh -n infra/scripts/remote-deploy.sh` 및 앞서 작성한 서비스 계약 검증을 실행한다.
- [ ] 배포 안내에 기존 `celery` 소비 유지, 구버전 생산자와 예약·선점·재전달 확인, 롤백 시 `ai_general` 호환 소비자 유지 절차를 기록한다. 큐 길이 0만으로 전환 완료라고 판단하거나 Redis 데이터를 삭제하지 않는다.
- [ ] `docs/infra/adr.yaml`, `docs/infra/code-convention.yaml`, `.gitlab-ci.yml`의 서비스·배포 관련 규칙과 문서를 확인해 필요한 동기화 여부를 기록한다. 배포 서비스 목록의 검증이 기존 CI에서 빠진다면 안전한 검증을 추가한다.

### 작업 3: 격리 Redis에서 소비·병렬성 검증

**파일:** 생성 `ai/tests/test_celery_worker_isolation.py`; 필요 시 기존 테스트 헬퍼 재사용.

**인터페이스:** `WORKLOG_WORKER_PROBE_BROKER_URL`로 명시적으로 제공된 격리 Redis만 사용하며, 테스트별 고유 큐와 결과 키를 사용한다. 기존 `tests/test_light_worker_runtime_integration.py`와 같이 미설정·지원 불가 환경에서는 명시적으로 skip한다. 운영 브로커와 외부 AI API는 사용하지 않는다.

- [ ] 두 독립 Worker를 실행하는 통합 테스트를 작성한다. 테스트 전용 task와 동기화 신호를 사용해 일반 Worker가 점유된 동안 색인 전용 Worker가 작업을 시작하고, 반대 방향도 성립하는지 제한 시간 안에 확인한다. 수면 시간만으로 동시성을 추론하지 않는다.
- [ ] 일반 Worker가 전환 중 `celery` 큐의 테스트 메시지와 새 `ai_general` 메시지를 각각 처리하는지, 색인 Worker가 일반 큐를 처리하지 않는지 확인한다. 실제 설정의 큐 이름 검증은 작업 2의 Compose 계약 테스트와 연결한다.
- [ ] 격리 Redis가 제공된 Linux 환경에서 `cd ai && WORKLOG_WORKER_PROBE_BROKER_URL=<격리 Redis URL> .venv/bin/python -m pytest tests/test_celery_worker_isolation.py tests/test_light_worker_runtime_integration.py -q`를 실행한다. 브로커 주소의 자격정보는 출력하지 않는다. 미실행 또는 skip은 최종 보고에 명시한다.
- [ ] 외부 결과 전달 보관·재전송 관련 기존 테스트 `tests/test_worklog_index_result_store.py`, `tests/test_worklog_index_result_delivery.py`, 색인 태스크 테스트를 실행한다. 비정상 종료 후 재전달은 적어도 한 번 전달이 가능한 구성과 실제 테스트 결과 범위에서만 설명한다.

### 작업 4: 최종 검증과 보고

**파일:** 위 변경 파일 전체.

**인터페이스:** 두 Compose 서비스와 `ai_general`, `celery`, `worklog_index` 큐 계약은 작업 1~3의 결과를 사용한다.

- [ ] 작업 1~3의 테스트를 새로 실행하거나 바로 앞 단계의 성공 출력을 수집하고 실패·skip을 분리해 기록한다. 테스트 불가 항목은 이유와 대체 검증을 보고한다.
- [ ] `git diff --check`와 변경 파일별 diff를 확인하고, 사용자 기존 변경 및 미추적 파일이 보존됐는지 확인한다. 실제 운영 배포, Redis 큐 조작, 커밋은 실행하지 않는다.
- [ ] 변경 내용, 실제 검증 결과, 운영 큐·색인 단일 실행 주체의 미확인 사항을 보고한다.
