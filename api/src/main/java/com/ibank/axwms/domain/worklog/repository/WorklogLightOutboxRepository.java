package com.ibank.axwms.domain.worklog.repository;

import com.ibank.axwms.domain.worklog.config.AiTaskExecutionBudgetProperties;
import java.util.List;
import java.util.UUID;
import lombok.RequiredArgsConstructor;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;
import org.springframework.transaction.annotation.Transactional;

/** 업무별 최신 색인 의도 한 행과 단일 시도 횟수·lease를 보존한다. */
@Repository
@RequiredArgsConstructor
public class WorklogLightOutboxRepository {
    private final JdbcTemplate jdbcTemplate;
    private final AiTaskExecutionBudgetProperties budget;

    /** 최신 요청으로 교체하되 이미 기록된 DELETE가 수정 요청에 밀리지 않게 한다. */
    @Transactional
    public void enqueue(Long worklogId, String operation) {
        UUID requestId = UUID.randomUUID();
        jdbcTemplate.update("""
                UPDATE tb_worklog SET light_index_request_id = ?, light_index_execution_id = NULL,
                    light_index_status = 'PENDING', light_index_error = NULL, light_index_completed_at = NULL
                WHERE worklog_id = ?
                """, requestId, worklogId);
        jdbcTemplate.update("""
                INSERT INTO tb_worklog_light_outbox (worklog_id, operation, request_id) VALUES (?, ?, ?)
                ON CONFLICT (worklog_id) DO UPDATE SET
                    operation = CASE WHEN tb_worklog_light_outbox.operation = 'DELETE' OR EXCLUDED.operation = 'DELETE'
                        THEN 'DELETE' ELSE EXCLUDED.operation END,
                    revision = tb_worklog_light_outbox.revision + 1, request_id = EXCLUDED.request_id,
                    retried_from_request_id = NULL, attempts = 0, terminal_failed = false,
                    claim_token = NULL, lease_until = NULL, next_attempt_at = CURRENT_TIMESTAMP,
                    last_error = NULL, updated_at = CURRENT_TIMESTAMP
                """, worklogId, operation, requestId);
    }

    /** 다른 발행자와 경합하지 않고 due 작업 하나를 선점하며 전달·실행을 같은 횟수로 센다. */
    public List<Claim> claim(Long worklogId, UUID token) {
        return jdbcTemplate.query("""
                UPDATE tb_worklog_light_outbox o
                SET claim_token = ?, lease_until = CURRENT_TIMESTAMP + (? * INTERVAL '1 second'), attempts = o.attempts + 1
                FROM (
                    SELECT worklog_id FROM tb_worklog_light_outbox
                    WHERE (CAST(? AS BIGINT) IS NULL OR worklog_id = ?) AND NOT terminal_failed
                      AND attempts < ? AND next_attempt_at <= CURRENT_TIMESTAMP AND lease_until IS NULL
                    ORDER BY next_attempt_at, worklog_id FOR UPDATE SKIP LOCKED LIMIT 1
                ) due
                WHERE o.worklog_id = due.worklog_id
                RETURNING o.worklog_id, o.operation, o.revision, o.request_id
                """, (rs, rowNum) -> new Claim(rs.getLong("worklog_id"), rs.getString("operation"),
                rs.getLong("revision"), token, rs.getObject("request_id", UUID.class)),
                token, budget.timeout().toSeconds(), worklogId, worklogId, budget.maxAttempts());
    }

    /** 접수 뒤에도 같은 lease를 유지해 실행되지 않은 큐 메시지도 timeout으로 종결한다. */
    public void awaitResult(Claim claim) {
        jdbcTemplate.update("""
                UPDATE tb_worklog_light_outbox SET claim_token = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE worklog_id = ? AND revision = ? AND request_id = ? AND claim_token = ?
                """, claim.worklogId(), claim.revision(), claim.requestId(), claim.token());
    }

    /** 성공 콜백은 검증한 최신 요청의 행만 지운다. */
    public void completeResult(Long worklogId, UUID requestId) {
        jdbcTemplate.update("DELETE FROM tb_worklog_light_outbox WHERE worklog_id = ? AND request_id = ?", worklogId, requestId);
    }

    /** 최종 실패 의도는 수동 재시도까지 남기며 늦은 접수 응답이 되살릴 수 없게 한다. */
    public void retryResult(Long worklogId, UUID requestId, String error) {
        jdbcTemplate.update("""
                UPDATE tb_worklog_light_outbox SET terminal_failed = true, lease_until = NULL, claim_token = NULL,
                    last_error = LEFT(?, 1000), updated_at = CURRENT_TIMESTAMP WHERE worklog_id = ? AND request_id = ?
                """, error, worklogId, requestId);
    }

    /** 최신 요청의 최종 실패만 조회해 수동 복구 표시와 일치시킨다. */
    public boolean isTerminalFailure(Long worklogId) {
        return Boolean.TRUE.equals(jdbcTemplate.queryForObject("""
                SELECT EXISTS (SELECT 1 FROM tb_worklog_light_outbox o JOIN tb_worklog w
                    ON w.worklog_id = o.worklog_id AND w.light_index_request_id = o.request_id
                    WHERE o.worklog_id = ? AND o.terminal_failed)
                """, Boolean.class, worklogId));
    }

    /** 시작 확인 전 전달 실패만 같은 revision의 backoff로 돌리고 한도 소진 시 자동 중단한다. */
    public void fail(Claim claim, String error) {
        jdbcTemplate.update("""
                UPDATE tb_worklog_light_outbox o SET terminal_failed = attempts >= ?,
                    next_attempt_at = CURRENT_TIMESTAMP + (POWER(2, LEAST(attempts - 1, 8)) * INTERVAL '1 second'),
                    lease_until = NULL, claim_token = NULL, last_error = LEFT(?, 1000), updated_at = CURRENT_TIMESTAMP
                WHERE worklog_id = ? AND revision = ? AND request_id = ? AND claim_token = ?
                """, budget.maxAttempts(), error, claim.worklogId(), claim.revision(), claim.requestId(), claim.token());
    }

    /** 결과가 오지 않은 작업은 재발행 대신 같은 lease 만료를 기준으로 실패 정리한다. */
    public List<Claim> expiredClaims(int limit) {
        return jdbcTemplate.query("""
                SELECT worklog_id, operation, revision, request_id, claim_token FROM tb_worklog_light_outbox
                WHERE NOT terminal_failed AND lease_until <= CURRENT_TIMESTAMP ORDER BY lease_until LIMIT ?
                """, (rs, rowNum) -> new Claim(rs.getLong("worklog_id"), rs.getString("operation"),
                rs.getLong("revision"), rs.getObject("claim_token", UUID.class), rs.getObject("request_id", UUID.class)), limit);
    }

    public record Claim(Long worklogId, String operation, long revision, UUID token, UUID requestId) {}
}
