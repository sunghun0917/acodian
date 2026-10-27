package com.ibank.axwms.domain.worklog.repository;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;
import org.springframework.transaction.annotation.Transactional;

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
    @Transactional
    public void enqueue(Long worklogId, String operation) {
        UUID requestId = UUID.randomUUID();
        jdbcTemplate.update("""
                UPDATE tb_worklog SET light_index_request_id = ?, light_index_execution_id = NULL,
                    light_index_status = 'PENDING', light_index_error = NULL, light_index_completed_at = NULL
                WHERE worklog_id = ?
                """, requestId, worklogId);
        jdbcTemplate.update("""
                INSERT INTO tb_worklog_light_outbox (worklog_id, operation, request_id)
                VALUES (?, ?, ?)
                ON CONFLICT (worklog_id) DO UPDATE SET
                    operation = CASE WHEN tb_worklog_light_outbox.operation = 'DELETE'
                                      OR EXCLUDED.operation = 'DELETE' THEN 'DELETE' ELSE 'REINDEX' END,
                    revision = tb_worklog_light_outbox.revision + 1,
                    request_id = EXCLUDED.request_id,
                    attempts = 0,
                    next_attempt_at = CURRENT_TIMESTAMP,
                    last_error = NULL,
                    updated_at = CURRENT_TIMESTAMP
                """, worklogId, operation, requestId);
        jdbcTemplate.update("""
                UPDATE tb_worklog_ai_post_process_outbox
                SET light_request_id = ?, light_accepted = true,
                    completed_stages = array_remove(completed_stages, 'LIGHT_INDEX'),
                    next_attempt_at = CURRENT_TIMESTAMP
                WHERE worklog_id = ?
                """, requestId, worklogId);
    }

    /** null ID는 전체 대기 작업을 대상으로 하며 다른 인스턴스가 점유하지 않은 작업 하나를 원자적으로 선점한다. */
    public List<Claim> claim(Long worklogId, UUID token) {
        return jdbcTemplate.query("""
                UPDATE tb_worklog_light_outbox AS o
                SET claim_token = ?, lease_until = CURRENT_TIMESTAMP + INTERVAL '10 minutes'
                FROM (
                    SELECT worklog_id FROM tb_worklog_light_outbox
                    WHERE (CAST(? AS BIGINT) IS NULL OR worklog_id = ?)
                      AND next_attempt_at <= CURRENT_TIMESTAMP
                      AND (lease_until IS NULL OR lease_until < CURRENT_TIMESTAMP)
                    ORDER BY next_attempt_at, worklog_id
                    FOR UPDATE SKIP LOCKED LIMIT 1
                ) AS due
                WHERE o.worklog_id = due.worklog_id
                RETURNING o.worklog_id, o.operation, o.revision, o.request_id
                """, (rs, rowNum) -> new Claim(
                rs.getLong("worklog_id"), rs.getString("operation"), rs.getLong("revision"), token, rs.getObject("request_id", UUID.class)),
                token, worklogId, worklogId);
    }

    /** 접수는 완료가 아니므로 결과 대기 기한을 두고, 교체된 revision은 즉시 전달 가능하게 풀어 준다. */
    @Transactional
    public void awaitResult(Claim claim) {
        jdbcTemplate.update("""
                UPDATE tb_worklog_light_outbox
                SET next_attempt_at = CURRENT_TIMESTAMP + INTERVAL '10 minutes',
                    claim_token = NULL, lease_until = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE worklog_id = ? AND revision = ? AND claim_token = ?
                """, claim.worklogId(), claim.revision(), claim.token());
        jdbcTemplate.update("""
                UPDATE tb_worklog_light_outbox SET claim_token = NULL, lease_until = NULL
                WHERE worklog_id = ? AND claim_token = ? AND revision <> ?
                """, claim.worklogId(), claim.token(), claim.revision());
    }

    /** 콜백 서비스가 최신 요청·실행을 검증한 뒤 해당 요청의 의도만 완료한다. */
    public void completeResult(Long worklogId, UUID requestId) {
        jdbcTemplate.update("""
                DELETE FROM tb_worklog_light_outbox WHERE worklog_id = ? AND request_id = ?
                """, worklogId, requestId);
    }

    /** 최종 실패도 의도를 보존하며 빠른 콜백이 접수 대기로 덮이지 않게 전달 선점을 해제한다. */
    public void retryResult(Long worklogId, UUID requestId, String error) {
        jdbcTemplate.update("""
                UPDATE tb_worklog_light_outbox SET
                    attempts = attempts + 1,
                    next_attempt_at = CURRENT_TIMESTAMP
                        + (LEAST(300, CAST(POWER(2, LEAST(attempts, 8)) AS INTEGER)) * INTERVAL '1 second'),
                    lease_until = NULL, claim_token = NULL,
                    last_error = LEFT(?, 1000), updated_at = CURRENT_TIMESTAMP
                WHERE worklog_id = ? AND request_id = ?
                """, error, worklogId, requestId);
    }

    /** 실패 revision 에만 지수 백오프를 부여하고 새 revision 은 즉시 재시도 가능하게 한다. */
    @Transactional
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
    public record Claim(Long worklogId, String operation, long revision, UUID token, UUID requestId) {
    }
}
