-- 일반 후처리를 요약·태그·파일별 현재 요청으로 분리한다. 기존 버전은 로컬 초기화 후 순서대로 적용한다.
ALTER TABLE tb_worklog_ai_post_process_outbox
    DROP CONSTRAINT tb_worklog_ai_post_process_outbox_pkey,
    DROP COLUMN retry_payload,
    DROP COLUMN completed_stages,
    DROP COLUMN pending_failed_stages,
    DROP COLUMN terminal_failed,
    ADD COLUMN task_kind VARCHAR(20) NOT NULL CHECK (task_kind IN ('SUMMARY', 'TAG', 'FILE_SUMMARY')),
    ADD COLUMN target_id BIGINT NOT NULL,
    ADD COLUMN request_id UUID NOT NULL DEFAULT gen_random_uuid(),
    ADD COLUMN execution_id UUID,
    ADD COLUMN input_hash VARCHAR(64),
    ADD COLUMN delivery_state VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    ADD COLUMN execution_state VARCHAR(20) NOT NULL DEFAULT 'UNKNOWN',
    ADD COLUMN retried_from_request_id UUID,
    ADD COLUMN result_digest VARCHAR(64),
    ADD COLUMN result_error_code VARCHAR(100),
    ADD CONSTRAINT pk_worklog_ai_task PRIMARY KEY (worklog_id, task_kind, target_id),
    ADD CONSTRAINT uq_worklog_ai_task_request UNIQUE (request_id);

ALTER TABLE tb_worklog_light_outbox ADD COLUMN retried_from_request_id UUID;

ALTER TABLE tb_notification
    ADD COLUMN ai_request_id UUID,
    ADD COLUMN ai_task_kind VARCHAR(20),
    ADD COLUMN ai_target_id BIGINT,
    ADD COLUMN ai_result VARCHAR(30);

CREATE UNIQUE INDEX uq_notification_ai_task_result
    ON tb_notification (user_id, ai_request_id, ai_result) WHERE ai_request_id IS NOT NULL;
