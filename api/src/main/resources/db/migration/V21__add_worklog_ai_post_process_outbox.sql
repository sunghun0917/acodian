-- 등록 후처리는 수정·삭제 색인과 별도로 보존하여 작업 병합으로 요약 요청이 사라지지 않게 한다.
CREATE TABLE tb_worklog_ai_post_process_outbox (
    worklog_id BIGINT PRIMARY KEY,
    payload JSONB NOT NULL,
    retry_payload JSONB,
    completed_stages TEXT[] NOT NULL DEFAULT '{}',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    lease_until TIMESTAMP,
    claim_token UUID,
    last_error VARCHAR(1000),
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX ix_worklog_ai_post_process_outbox_due
    ON tb_worklog_ai_post_process_outbox (next_attempt_at, lease_until);
