package com.ibank.axwms.domain.worklog.repository;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

import java.util.List;
import java.util.UUID;

/** PostgreSQL 단일 행으로 업무일지별 최신 AI 반영 의도를 보존한다. */
@Repository
public class WorklogLightOutboxRepository {

    private final JdbcTemplate jdbcTemplate;

    public WorklogLightOutboxRepository(JdbcTemplate jdbcTemplate) {
        this.jdbcTemplate = jdbcTemplate;
    }

    /** 수정·삭제 트랜잭션에 참여하며 삭제 의도가 수정 의도보다 우선한다. */
    public void enqueue(Long worklogId, String operation) {
        jdbcTemplate.update("""
                INSERT INTO tb_worklog_light_outbox (worklog_id, operation)
                VALUES (?, ?)
                ON CONFLICT (worklog_id) DO UPDATE SET
                    operation = CASE WHEN tb_worklog_light_outbox.operation = 'DELETE'
                                      OR EXCLUDED.operation = 'DELETE' THEN 'DELETE' ELSE 'REINDEX' END,
                    revision = tb_worklog_light_outbox.revision + 1,
                    attempts = 0,
                    next_attempt_at = CURRENT_TIMESTAMP,
                    last_error = NULL,
                    updated_at = CURRENT_TIMESTAMP
                """, worklogId, operation);
    }

    /** 다른 인스턴스가 점유하지 않은 기한 도래 작업 하나를 원자적으로 선점한다. */
    public List<Claim> claim(Long worklogId, UUID token) {
        return jdbcTemplate.query("""
                UPDATE tb_worklog_light_outbox AS o
                SET claim_token = ?, lease_until = CURRENT_TIMESTAMP + INTERVAL '10 minutes'
                FROM (
                    SELECT worklog_id FROM tb_worklog_light_outbox
                    WHERE (? IS NULL OR worklog_id = ?)
                      AND next_attempt_at <= CURRENT_TIMESTAMP
                      AND (lease_until IS NULL OR lease_until < CURRENT_TIMESTAMP)
                    ORDER BY next_attempt_at, worklog_id
                    FOR UPDATE SKIP LOCKED LIMIT 1
                ) AS due
                WHERE o.worklog_id = due.worklog_id
                RETURNING o.worklog_id, o.operation, o.revision
                """, (rs, rowNum) -> new Claim(
                rs.getLong("worklog_id"), rs.getString("operation"), rs.getLong("revision"), token),
                token, worklogId, worklogId);
    }

    /** 선점한 revision 만 제거하여 처리 중 유입된 최신 변경을 잃지 않는다. */
    public void complete(Claim claim) {
        jdbcTemplate.update("""
                DELETE FROM tb_worklog_light_outbox
                WHERE worklog_id = ? AND revision = ? AND claim_token = ?
                """, claim.worklogId(), claim.revision(), claim.token());
        jdbcTemplate.update("""
                UPDATE tb_worklog_light_outbox SET claim_token = NULL, lease_until = NULL
                WHERE worklog_id = ? AND claim_token = ? AND revision <> ?
                """, claim.worklogId(), claim.token(), claim.revision());
    }

    /** 실패 revision 에만 지수 백오프를 부여하고 새 revision 은 즉시 재시도 가능하게 한다. */
    public void fail(Claim claim, String error) {
        jdbcTemplate.update("""
                UPDATE tb_worklog_light_outbox SET
                    attempts = attempts + 1,
                    next_attempt_at = CURRENT_TIMESTAMP
                        + (LEAST(300, CAST(POWER(2, LEAST(attempts, 8)) AS INTEGER)) * INTERVAL '1 second'),
                    lease_until = NULL, claim_token = NULL,
                    last_error = LEFT(?, 1000), updated_at = CURRENT_TIMESTAMP
                WHERE worklog_id = ? AND revision = ? AND claim_token = ?
                """, error, claim.worklogId(), claim.revision(), claim.token());
        jdbcTemplate.update("""
                UPDATE tb_worklog_light_outbox SET claim_token = NULL, lease_until = NULL
                WHERE worklog_id = ? AND claim_token = ? AND revision <> ?
                """, claim.worklogId(), claim.token(), claim.revision());
    }

    /** 한 번의 원격 요청에 대해 선점된 불변 작업 식별자이다. */
    public record Claim(Long worklogId, String operation, long revision, UUID token) {
    }
}
