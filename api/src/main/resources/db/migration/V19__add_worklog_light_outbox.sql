-- 업무일지 변경과 AI 색인 작업을 같은 DB 트랜잭션에 기록한다.
CREATE TABLE tb_worklog_light_outbox (
    worklog_id BIGINT PRIMARY KEY,
    operation VARCHAR(10) NOT NULL CHECK (operation IN ('REINDEX', 'DELETE')),
    revision BIGINT NOT NULL DEFAULT 1,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    lease_until TIMESTAMP,
    claim_token UUID,
    last_error VARCHAR(1000),
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX ix_worklog_light_outbox_due
    ON tb_worklog_light_outbox (next_attempt_at, lease_until);
