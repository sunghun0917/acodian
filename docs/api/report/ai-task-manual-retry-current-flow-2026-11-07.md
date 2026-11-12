# AI 비동기 작업·알림·수동 재시도 현재 구현 흐름

2026-11-12 S1 단순화 기준. 변경 근거와 테스트·한계는 [S1 보고서](../../ai/report/ai-task-simplification-2026-11-12.md)를 참고한다.

## 등록부터 결과까지

업무 등록과 파일 추가 시 일반 작업 `SUMMARY`, `TAG`, 파일별 `FILE_SUMMARY`와 Light `INDEX` 의도를 같은 트랜잭션에 저장한다. 각 일반 작업과 Light는 독립적으로 실행되고 작업별 결과 알림을 만든다. Light는 업무당 한 Outbox 행에 최신 `INDEX`/`REINDEX`/`DELETE` 의도와 증가하는 revision을 유지하며 DELETE가 우선한다.

스케줄러는 `FOR UPDATE SKIP LOCKED`로 선점하고 `attempts`와 하나의 `lease_until`을 사용한다. 외부 HTTP는 트랜잭션 밖에서 호출하며 AI는 `ai_general` 또는 `worklog_index` 큐에 발행한다. Worker는 최신 요청의 유효한 선점인지 시작 API로 확인하고 실행한다. 현재 요청의 `requestId`, `executionId`, 입력 해시를 검증한 결과만 반영하며 중복 결과는 멱등 처리한다.

결과는 AI 측 파일에 보관한 후 콜백한다. 전송이 실패하면 보관된 결과를 재전송하고 성공 또는 stale ACK 후 파일을 삭제한다. 콜백 실패가 AI 재실행을 유발하지는 않는다. API가 영구 4xx(408/429 제외)로 거절한 결과는 재전송하지 않고 파일을 지우며, 해당 작업은 lease 만료 회수로 FAILED 처리된다.

## 실패·재시도

전달 실패나 재시도 가능한 실행 실패는 attempts 한도 내에서 1/2/4/…초의 backoff로 재선점한다. 기본 최대 attempts는 4이며 설정으로 변경할 수 있다. 색인 실행 실패 시 부분 산출물을 보상 삭제한다. 설정 오류 또는 한도 소진은 `FAILED`와 작업별 알림으로 끝난다. 접수 후 시작이나 결과가 오지 않아 단일 lease가 만료된 경우에도 `FAILED`로 전환한다.

작성자는 실패 알림에서 해당 작업을 수동 재시도할 수 있다. 새 `requestId`와 초기화된 attempts를 사용하고 이전 요청과 연결한다. 중복 클릭은 같은 새 요청으로 수렴한다. 색인 삭제 실패도 작성자가 재시도할 수 있다.

## 확인할 한계

timeout 이후에도 이미 전송된 원격 저장소 쓰기가 계속될 수 있으며 프로세스 내부 잠금은 다중 worker를 직렬화하지 않는다. 보상 실패·강제 종료에서는 부분 데이터가 남을 수 있다. provider 호출 횟수와 결과 콜백 동시 횟수는 별도 제한하지 않는다. 실행 결과와 opt-in E2E 범위는 S1 보고서를 따른다.

주요 코드: `WorklogAiTaskOutboxWorker`, `WorklogLightOutboxWorker`, `WorklogAiTaskRetryService`, `InternalWorklogLightIndexService`, `ai/app/task/light_worklog_tasks.py`, `ai/app/service/ai_task_result_delivery.py`.
