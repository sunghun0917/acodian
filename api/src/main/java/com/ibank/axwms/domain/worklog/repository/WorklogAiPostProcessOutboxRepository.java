package com.ibank.axwms.domain.worklog.repository;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.json.JsonMapper;
import com.ibank.axwms.domain.worklog.event.WorklogAiPostProcessRequestedEvent;
import com.ibank.axwms.global.error.BusinessException;
import com.ibank.axwms.global.error.ErrorCode;
import java.util.Arrays;
import java.util.List;
import java.util.Optional;
import java.util.Set;
import java.util.UUID;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;
import org.springframework.transaction.annotation.Transactional;

/** 등록 snapshot과 단계별 전달 결과를 보존하며, claim token으로 오래된 워커의 갱신을 차단한다. */
@Repository
public class WorklogAiPostProcessOutboxRepository {

    private static final ObjectMapper OBJECT_MAPPER = JsonMapper.builder().build();
    private final JdbcTemplate jdbcTemplate;

    public WorklogAiPostProcessOutboxRepository(JdbcTemplate jdbcTemplate) {
        this.jdbcTemplate = jdbcTemplate;
    }

    /** 진행 중 snapshot은 유지하고 명시적 수동 재요청은 다음 실행 입력으로 보존한다. */
    @Transactional
    public void enqueue(WorklogAiPostProcessRequestedEvent event) {
        UUID requestId = UUID.randomUUID();
        lockWorklog(event.worklogId());
        jdbcTemplate.update("""
                INSERT INTO tb_worklog_ai_post_process_outbox (worklog_id, payload, light_request_id)
                VALUES (?, CAST(? AS jsonb), ?)
                ON CONFLICT (worklog_id) DO UPDATE SET
                    retry_payload = EXCLUDED.payload,
                    retry_light_request_id = EXCLUDED.light_request_id,
                    terminal_failed = false,
                    next_attempt_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                """, event.worklogId(), serialize(event), requestId);
        // 진행 중 수동 재요청은 승격 시 새 요청을 활성화한다.
        jdbcTemplate.update("""
                UPDATE tb_worklog SET light_index_request_id = ?, light_index_execution_id = NULL,
                    light_index_status = 'PENDING', light_index_error = NULL, light_index_completed_at = NULL
                WHERE worklog_id = ? AND EXISTS (
                    SELECT 1 FROM tb_worklog_ai_post_process_outbox
                    WHERE worklog_id = ? AND light_request_id = ?)
                """, requestId, event.worklogId(), event.worklogId(), requestId);
        discardSupersededReindex(event.worklogId());
    }

    /** lease로 작업을 선점하되 최종 실패 행은 원본 삭제 후 정리할 때만 자동 선점한다. */
    public List<Claim> claim(Long worklogId, UUID token) {
        return jdbcTemplate.query("""
                UPDATE tb_worklog_ai_post_process_outbox AS o
                SET claim_token = ?, lease_until = CURRENT_TIMESTAMP + INTERVAL '10 minutes'
                FROM (
                    SELECT p.worklog_id FROM tb_worklog_ai_post_process_outbox p
                    WHERE (CAST(? AS BIGINT) IS NULL OR p.worklog_id = ?)
                      AND p.next_attempt_at <= CURRENT_TIMESTAMP
                      AND (NOT p.terminal_failed OR NOT EXISTS (
                          SELECT 1 FROM tb_worklog w WHERE w.worklog_id = p.worklog_id AND w.is_deleted = false))
                      AND (p.lease_until IS NULL OR p.lease_until < CURRENT_TIMESTAMP)
                    ORDER BY p.next_attempt_at, p.worklog_id
                    FOR UPDATE SKIP LOCKED LIMIT 1
                ) AS due
                WHERE o.worklog_id = due.worklog_id
                RETURNING o.worklog_id, o.payload, o.completed_stages, o.attempts
                """, (rs, rowNum) -> new Claim(rs.getLong("worklog_id"),
                deserialize(rs.getString("payload")),
                Set.copyOf(Arrays.asList((String[]) rs.getArray("completed_stages").getArray())),
                rs.getInt("attempts"), token), token, worklogId, worklogId);
    }

    /** 각 외부 호출 직전에 소유권과 업무 생존을 확인하고 다음 단계의 실행 기한을 확보한다. */
    public boolean renewLease(Claim claim) {
        return jdbcTemplate.update("""
                UPDATE tb_worklog_ai_post_process_outbox
                SET lease_until = CURRENT_TIMESTAMP + INTERVAL '10 minutes'
                WHERE worklog_id = ? AND claim_token = ? AND lease_until > CURRENT_TIMESTAMP
                  AND EXISTS (SELECT 1 FROM tb_worklog WHERE worklog_id = ? AND is_deleted = false)
                """, claim.worklogId(), claim.token(), claim.worklogId()) == 1;
    }

    /** 접수·색인 성공을 즉시 기록하여 이후 단계 실패 시 이미 끝난 요청을 반복하지 않는다. */
    public boolean checkpoint(Claim claim, String stage) {
        return jdbcTemplate.update("""
                UPDATE tb_worklog_ai_post_process_outbox
                SET completed_stages = CASE WHEN ? = ANY(completed_stages) THEN completed_stages
                    ELSE array_append(completed_stages, ?) END,
                    lease_until = CURRENT_TIMESTAMP + INTERVAL '10 minutes', updated_at = CURRENT_TIMESTAMP
                WHERE worklog_id = ? AND claim_token = ? AND lease_until > CURRENT_TIMESTAMP
                """, stage, stage, claim.worklogId(), claim.token()) == 1;
    }

    /** 완료 트랜잭션에서 소유권을 재확인하며 동일 업무의 새 선점·수동 재시도를 직렬화한다. */
    public boolean lockClaim(Claim claim) {
        return !jdbcTemplate.queryForList("""
                SELECT worklog_id FROM tb_worklog_ai_post_process_outbox
                WHERE worklog_id = ? AND claim_token = ? AND lease_until > CURRENT_TIMESTAMP
                FOR UPDATE
                """, Long.class, claim.worklogId(), claim.token()).isEmpty();
    }

    /** 수동 재요청은 요약·색인을 새로 실행하되, 원래 첨부 파일과 DB의 최신 파일 성공 기록은 보존한다. */
    public boolean activateQueuedRetry(Claim claim) {
        boolean activated = jdbcTemplate.update("""
                UPDATE tb_worklog_ai_post_process_outbox
                SET payload = jsonb_set(retry_payload, '{files}', payload -> 'files'),
                    retry_payload = NULL, light_request_id = retry_light_request_id,
                    retry_light_request_id = NULL, light_accepted = false,
                    terminal_failed = false, light_result_deadline_at = NULL,
                    completed_stages = ARRAY(SELECT stage FROM unnest(completed_stages) AS stage
                                             WHERE stage NOT IN ('WORKLOG_PIPELINE', 'LIGHT_INDEX')),
                    attempts = 0, next_attempt_at = CURRENT_TIMESTAMP,
                    lease_until = NULL, claim_token = NULL, last_error = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE worklog_id = ? AND claim_token = ? AND retry_payload IS NOT NULL
                """, claim.worklogId(), claim.token()) == 1;
        if (activated) {
            jdbcTemplate.update("""
                    UPDATE tb_worklog w SET light_index_request_id = p.light_request_id,
                        light_index_execution_id = NULL, light_index_status = 'PENDING',
                        light_index_error = NULL, light_index_completed_at = NULL
                    FROM tb_worklog_ai_post_process_outbox p WHERE w.worklog_id = p.worklog_id
                    AND w.worklog_id = ?
                    """, claim.worklogId());
            discardSupersededReindex(claim.worklogId());
        }
        return activated;
    }

    /** 등록·수동 재시도와 같은 업무→Outbox 잠금 순서를 사용하고 삭제된 업무의 후처리를 취소한다. */
    public Optional<Boolean> lockWorklog(Long worklogId) {
        return jdbcTemplate.queryForList("""
                SELECT is_deleted FROM tb_worklog WHERE worklog_id = ? FOR UPDATE
                """, Boolean.class, worklogId).stream().findFirst();
    }

    /** 지연된 등록 작업이 이미 삭제된 업무에 새 AI 요청을 보내지 않도록 확인한다. */
    public boolean isWorklogActive(Long worklogId) {
        return Boolean.TRUE.equals(jdbcTemplate.queryForObject("""
                SELECT EXISTS (SELECT 1 FROM tb_worklog WHERE worklog_id = ? AND is_deleted = false)
                """, Boolean.class, worklogId));
    }

    /** 수동 복구 대기를 포함해 전체 AI 처리 상태를 등록 후처리가 소유하는지 확인한다. */
    public boolean hasPostProcess(Long worklogId) {
        return Boolean.TRUE.equals(jdbcTemplate.queryForObject("""
                SELECT EXISTS (SELECT 1 FROM tb_worklog_ai_post_process_outbox WHERE worklog_id = ?)
                """, Boolean.class, worklogId));
    }

    /** 현재 token으로 선점한 작업만 제거하여 재선점된 작업을 보존한다. */
    public void complete(Claim claim) {
        jdbcTemplate.update("""
                DELETE FROM tb_worklog_ai_post_process_outbox WHERE worklog_id = ? AND claim_token = ?
                """, claim.worklogId(), claim.token());
    }

    /** 성공 checkpoint는 유지한 채 최대 256초 백오프로 실패한 작업을 다시 실행 가능하게 한다. */
    public void fail(Claim claim, String error) {
        jdbcTemplate.update("""
                UPDATE tb_worklog_ai_post_process_outbox
                SET attempts = attempts + 1,
                    next_attempt_at = CURRENT_TIMESTAMP
                        + (CAST(POWER(2, LEAST(attempts, 8)) AS INTEGER) * INTERVAL '1 second'),
                    lease_until = NULL, claim_token = NULL,
                    last_error = LEFT(?, 1000), updated_at = CURRENT_TIMESTAMP
                WHERE worklog_id = ? AND claim_token = ?
                """, error, claim.worklogId(), claim.token());
    }

    /** 수동 재요청까지 첨부 snapshot과 checkpoint를 보존하고 자동 선점만 중단한다. */
    public void suspendAfterLightFailure(Claim claim, String error) {
        jdbcTemplate.update("""
                UPDATE tb_worklog_ai_post_process_outbox
                SET terminal_failed = true, claim_token = NULL, lease_until = NULL,
                    last_error = LEFT(?, 1000), updated_at = CURRENT_TIMESTAMP
                WHERE worklog_id = ? AND claim_token = ?
                """, error, claim.worklogId(), claim.token());
    }

    /** 수정·삭제 Outbox가 전달하는 요청을 등록 폴러가 중복 재발행하거나 중단하지 않게 한다. */
    public boolean ownsLightRequest(Claim claim) {
        return Boolean.TRUE.equals(jdbcTemplate.queryForObject("""
                SELECT EXISTS (
                    SELECT 1 FROM tb_worklog_ai_post_process_outbox p
                    JOIN tb_worklog w ON w.worklog_id = p.worklog_id AND w.light_index_request_id = p.light_request_id
                    WHERE p.worklog_id = ? AND p.claim_token = ?
                      AND NOT EXISTS (SELECT 1 FROM tb_worklog_light_outbox o
                                      WHERE o.worklog_id = p.worklog_id AND o.request_id = p.light_request_id))
                """, Boolean.class, claim.worklogId(), claim.token()));
    }

    /** 결과가 없는 등록 요청만 다시 전달 가능하게 하고 기존 실행의 늦은 결과는 계속 허용한다. */
    public boolean recoverTimedOutLightRequest(Claim claim) {
        if (!ownsLightRequest(claim)) {
            return false;
        }
        return jdbcTemplate.update("""
                UPDATE tb_worklog_ai_post_process_outbox
                SET light_accepted = false, light_result_deadline_at = NULL
                WHERE worklog_id = ? AND claim_token = ? AND light_accepted
                  AND light_result_deadline_at <= CURRENT_TIMESTAMP
                """, claim.worklogId(), claim.token()) == 1;
    }

    /** 최신 등록 의도가 같은 트랜잭션에 보존된 경우에만 이전 재색인을 취소하며 삭제 의도는 건드리지 않는다. */
    private void discardSupersededReindex(Long worklogId) {
        jdbcTemplate.update("""
                DELETE FROM tb_worklog_light_outbox o
                USING tb_worklog w, tb_worklog_ai_post_process_outbox p
                WHERE o.worklog_id = ? AND w.worklog_id = o.worklog_id AND p.worklog_id = w.worklog_id
                  AND w.light_index_request_id = p.light_request_id
                  AND o.request_id <> p.light_request_id AND o.operation = 'REINDEX'
                """, worklogId);
    }

    /** 현재 연결된 요청만 전달하며 수정·삭제에 의해 이미 대체된 최초 요청을 재발행하지 않는다. */
    public Optional<UUID> pendingLightRequest(Claim claim) {
        return jdbcTemplate.queryForList("""
                SELECT light_request_id FROM tb_worklog_ai_post_process_outbox
                WHERE worklog_id = ? AND claim_token = ? AND NOT light_accepted
                  AND NOT ('LIGHT_INDEX' = ANY(completed_stages))
                """, UUID.class, claim.worklogId(), claim.token()).stream().findFirst();
    }

    /** 늦은 202 응답은 접수 여부만 남기고 실행 상태나 완료 checkpoint를 되돌리지 않는다. */
    public void acceptLightRequest(Claim claim, UUID requestId) {
        jdbcTemplate.update("""
                UPDATE tb_worklog_ai_post_process_outbox p SET light_accepted = true,
                    light_result_deadline_at = COALESCE(light_result_deadline_at, CURRENT_TIMESTAMP + INTERVAL '10 minutes')
                WHERE worklog_id = ? AND claim_token = ? AND light_request_id = ?
                  AND EXISTS (SELECT 1 FROM tb_worklog w WHERE w.worklog_id = p.worklog_id
                              AND w.light_index_request_id = p.light_request_id
                              AND w.light_index_status IN ('PENDING', 'RUNNING'))
                """, claim.worklogId(), claim.token(), requestId);
    }

    /** callback이 먼저 온 경우까지 포함해 현재 요청의 실제 실행 결과만 읽는다. */
    public String lightState(Claim claim) {
        return jdbcTemplate.queryForObject("""
                SELECT CASE WHEN 'LIGHT_INDEX' = ANY(p.completed_stages) THEN 'COMPLETED'
                    ELSE COALESCE(w.light_index_status, 'PENDING') END
                FROM tb_worklog_ai_post_process_outbox p
                LEFT JOIN tb_worklog w ON w.worklog_id = p.worklog_id
                    AND w.light_index_request_id = p.light_request_id
                WHERE p.worklog_id = ? AND p.claim_token = ?
                """, String.class, claim.worklogId(), claim.token());
    }

    /** 실행 대기는 실패 재시도와 구분하고 선점·트랜잭션을 해제한다. */
    public void awaitLightResult(Claim claim) {
        jdbcTemplate.update("""
                UPDATE tb_worklog_ai_post_process_outbox
                SET claim_token = NULL, lease_until = NULL,
                    next_attempt_at = CURRENT_TIMESTAMP + INTERVAL '30 seconds'
                WHERE worklog_id = ? AND claim_token = ?
                """, claim.worklogId(), claim.token());
    }

    /** 직렬화 실패를 등록 트랜잭션의 실패로 전파하여 원본만 커밋되는 것을 막는다. */
    private String serialize(WorklogAiPostProcessRequestedEvent event) {
        try {
            return OBJECT_MAPPER.writeValueAsString(event);
        } catch (JsonProcessingException e) {
            throw new BusinessException(ErrorCode.COMMON_INTERNAL_SERVER_ERROR, e);
        }
    }

    /** 저장된 snapshot을 복구하지 못하면 작업을 성공 처리하지 않고 lease 만료 후 재시도 대상으로 남긴다. */
    private WorklogAiPostProcessRequestedEvent deserialize(String payload) {
        try {
            return OBJECT_MAPPER.readValue(payload, WorklogAiPostProcessRequestedEvent.class);
        } catch (JsonProcessingException e) {
            throw new BusinessException(ErrorCode.COMMON_INTERNAL_SERVER_ERROR, e);
        }
    }

    /** 같은 업무의 재선점과 구분되는 실행 시도 및 이전에 성공한 단계의 snapshot이다. */
    public record Claim(Long worklogId, WorklogAiPostProcessRequestedEvent event,
                        Set<String> completedStages, int attempts, UUID token) {
    }
}
