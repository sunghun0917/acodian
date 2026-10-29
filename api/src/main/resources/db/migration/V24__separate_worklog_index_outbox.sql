-- 기존 작업의 폐기는 별도 운영 승인 후에만 수행한다.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM tb_worklog_ai_post_process_outbox)
        OR EXISTS (SELECT 1 FROM tb_worklog_light_outbox) THEN
        RAISE EXCEPTION 'Worklog outboxes must be empty before migration';
    END IF;
END $$;

ALTER TABLE tb_worklog_light_outbox
    DROP CONSTRAINT tb_worklog_light_outbox_operation_check,
    ADD CONSTRAINT tb_worklog_light_outbox_operation_check
        CHECK (operation IN ('INDEX', 'REINDEX', 'DELETE')),
    ADD COLUMN terminal_failed BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE tb_worklog_ai_post_process_outbox
    DROP COLUMN light_request_id,
    DROP COLUMN retry_light_request_id,
    DROP COLUMN light_accepted,
    DROP COLUMN light_result_deadline_at,
    ADD COLUMN pending_failed_stages TEXT[] NOT NULL DEFAULT '{}';
