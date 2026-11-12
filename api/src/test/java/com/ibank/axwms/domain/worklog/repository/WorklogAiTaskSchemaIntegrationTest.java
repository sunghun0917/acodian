package com.ibank.axwms.domain.worklog.repository;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.ibank.axwms.testsupport.IntegrationTestSupport;
import com.ibank.axwms.domain.worklog.dto.AiTaskApiDto;
import com.ibank.axwms.domain.worklog.event.WorklogAiPostProcessRequestedEvent;
import java.util.List;
import java.util.Map;
import java.util.HexFormat;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.transaction.support.TransactionTemplate;

class WorklogAiTaskSchemaIntegrationTest extends IntegrationTestSupport {

    @Autowired
    private JdbcTemplate jdbcTemplate;

    @Autowired
    private WorklogAiTaskRepository taskRepository;

    @Autowired
    private TransactionTemplate transactionTemplate;

    @Test
    void permitsIndependentCurrentTasksAndRejectsDuplicateTarget() {
        insert(101L, "SUMMARY", 101L, UUID.randomUUID());
        insert(101L, "TAG", 101L, UUID.randomUUID());
        insert(101L, "FILE_SUMMARY", 900L, UUID.randomUUID());
        insert(101L, "FILE_SUMMARY", 901L, UUID.randomUUID());

        assertThat(jdbcTemplate.queryForObject("""
                SELECT count(*) FROM tb_worklog_ai_post_process_outbox WHERE worklog_id = 101
                """, Integer.class)).isEqualTo(4);
        assertThatThrownBy(() -> insert(101L, "SUMMARY", 101L, UUID.randomUUID()))
                .isInstanceOf(DataIntegrityViolationException.class);
    }

    @Test
    void startIsIdempotentAndRejectsConcurrentExecution() {
        UUID requestId = UUID.randomUUID();
        UUID executionId = UUID.randomUUID();
        insert(102L, "SUMMARY", 102L, requestId);
        jdbcTemplate.update("""
                UPDATE tb_worklog_ai_post_process_outbox SET input_hash = ? WHERE request_id = ?
                """, "a".repeat(64), requestId);
        var identity = new AiTaskApiDto.Identity(requestId, executionId, "a".repeat(64), "SUMMARY", 102L);

        assertThat(taskRepository.start(102L, identity)).isEmpty();
        markQueued(requestId);

        var first = taskRepository.start(102L, identity);
        var repeated = taskRepository.start(102L, identity);
        var other = taskRepository.start(102L, new AiTaskApiDto.Identity(requestId, UUID.randomUUID(), "a".repeat(64), "SUMMARY", 102L));

        assertThat(first).isPresent().isEqualTo(repeated);
        assertThat(other).isEmpty();
        assertThat(jdbcTemplate.queryForObject("""
                SELECT attempts FROM tb_worklog_ai_post_process_outbox WHERE request_id = ?
                """, Integer.class, requestId)).isEqualTo(1);
    }











    @Test
    void retryTransitionIsIdempotentAndFourthFailureRemainsAvailableForTerminalResult() {
        UUID request = UUID.randomUUID();
        insert(105L, "SUMMARY", 105L, request);
        jdbcTemplate.update("UPDATE tb_worklog_ai_post_process_outbox SET input_hash = ? WHERE request_id = ?",
                "a".repeat(64), request);
        var identity = new AiTaskApiDto.Identity(request, UUID.randomUUID(), "a".repeat(64), "SUMMARY", 105L);
        markQueued(request);
        UUID token = taskRepository.start(105L, identity).orElseThrow();
        identity = new AiTaskApiDto.Identity(request, identity.executionId(), "a".repeat(64), "SUMMARY", 105L);
        var first = taskRepository.retryableFailure(105L, identity, "TimeoutError");
        var repeated = taskRepository.retryableFailure(105L, identity, "TimeoutError");
        assertThat(first).isEqualTo(new AiTaskApiDto.TransitionResponse(true, true)).isEqualTo(repeated);
        assertThat(jdbcTemplate.queryForObject("SELECT execution_state FROM tb_worklog_ai_post_process_outbox WHERE request_id = ?",
                String.class, request)).isEqualTo("RETRY_READY");
        assertThat(jdbcTemplate.queryForObject("SELECT delivery_state FROM tb_worklog_ai_post_process_outbox WHERE request_id = ?",
                String.class, request)).isEqualTo("PENDING");

        jdbcTemplate.update("UPDATE tb_worklog_ai_post_process_outbox SET execution_state = 'RUNNING', attempts = 4, "
                + "lease_until = CURRENT_TIMESTAMP + INTERVAL '60 seconds' WHERE request_id = ?", request);
        assertThat(taskRepository.retryableFailure(105L, identity, "TimeoutError"))
                .isEqualTo(new AiTaskApiDto.TransitionResponse(true, false));
        assertThat(jdbcTemplate.queryForObject("SELECT execution_state FROM tb_worklog_ai_post_process_outbox WHERE request_id = ?",
                String.class, request)).isEqualTo("RUNNING");
    }

    @Test
    void deliveryClaimsCountAttemptsAndRejectsStaleWorkers() {
        UUID request = UUID.randomUUID();
        insert(106L, "SUMMARY", 106L, request);
        jdbcTemplate.update("UPDATE tb_worklog_ai_post_process_outbox SET input_hash = ? WHERE request_id = ?",
                "a".repeat(64), request);
        UUID firstToken = UUID.randomUUID();
        var first = taskRepository.claimDelivery(106L, firstToken, true, true).orElseThrow();
        assertThat(first.requestId()).isEqualTo(request);
        assertThat(first.attempts()).isEqualTo(1);
        assertThat(taskRepository.claimDelivery(106L, UUID.randomUUID(), true, true)).isEmpty();
        assertThat(taskRepository.deliveryFailed(first, "timeout")).isTrue();
        assertThat(taskRepository.deliveryFailed(first, "stale")).isFalse();
        jdbcTemplate.update("UPDATE tb_worklog_ai_post_process_outbox SET next_attempt_at = CURRENT_TIMESTAMP WHERE request_id = ?", request);
        var second = taskRepository.claimDelivery(106L, UUID.randomUUID(), true, true).orElseThrow();
        assertThat(second.attempts()).isEqualTo(2);
        assertThat(taskRepository.deliveryAccepted(first)).isFalse();
        assertThat(taskRepository.deliveryAccepted(second)).isTrue();
        assertThat(taskRepository.claimDelivery(106L, UUID.randomUUID(), true, true)).isEmpty();
    }

    @Test
    void fourthFailedDeliveryIsExhaustedAndNoLongerClaimable() {
        UUID request = UUID.randomUUID();
        insert(107L, "TAG", 107L, request);
        jdbcTemplate.update("UPDATE tb_worklog_ai_post_process_outbox SET input_hash = ?, attempts = 3 WHERE request_id = ?",
                "b".repeat(64), request);
        var claim = taskRepository.claimDelivery(107L, UUID.randomUUID(), true, true).orElseThrow();
        assertThat(claim.attempts()).isEqualTo(4);
        assertThat(taskRepository.deliveryFailed(claim, "timeout")).isTrue();
        assertThat(jdbcTemplate.queryForObject("SELECT delivery_state FROM tb_worklog_ai_post_process_outbox WHERE request_id = ?",
                String.class, request)).isEqualTo("EXHAUSTED");
        assertThat(taskRepository.claimDelivery(107L, UUID.randomUUID(), true, true)).isEmpty();
    }

    @Test
    void acceptedAfterStartDoesNotOverwriteRunningState() {
        UUID request = UUID.randomUUID();
        insert(108L, "SUMMARY", 108L, request);
        jdbcTemplate.update("UPDATE tb_worklog_ai_post_process_outbox SET input_hash = ? WHERE request_id = ?",
                "c".repeat(64), request);
        var claim = taskRepository.claimDelivery(108L, UUID.randomUUID(), true, true).orElseThrow();
        var identity = new AiTaskApiDto.Identity(request, UUID.randomUUID(), "c".repeat(64), "SUMMARY", 108L);
        assertThat(taskRepository.start(108L, identity)).isPresent();
        assertThat(taskRepository.deliveryAccepted(claim)).isTrue();
        assertThat(jdbcTemplate.queryForObject("SELECT execution_state FROM tb_worklog_ai_post_process_outbox WHERE request_id = ?",
                String.class, request)).isEqualTo("RUNNING");
    }

    @Test
    void delayedDeliveryAcknowledgementDoesNotEraseEarlierRetryTransition() {
        UUID request = UUID.randomUUID();
        insert(109L, "TAG", 109L, request);
        jdbcTemplate.update("UPDATE tb_worklog_ai_post_process_outbox SET input_hash = ? WHERE request_id = ?",
                "d".repeat(64), request);
        var claim = taskRepository.claimDelivery(109L, UUID.randomUUID(), true, true).orElseThrow();
        var start = new AiTaskApiDto.Identity(request, UUID.randomUUID(), "d".repeat(64), "TAG", 109L);
        UUID token = taskRepository.start(109L, start).orElseThrow();
        var identity = new AiTaskApiDto.Identity(request, start.executionId(), start.inputHash(), start.taskKind(), start.targetId());
        assertThat(taskRepository.retryableFailure(109L, identity, "TimeoutError").retryScheduled()).isTrue();
        var duplicate = new AiTaskApiDto.Identity(request, UUID.randomUUID(), start.inputHash(), start.taskKind(), start.targetId());
        assertThat(taskRepository.start(109L, duplicate)).isEmpty();
        assertThat(taskRepository.deliveryAccepted(claim)).isFalse();
        assertThat(jdbcTemplate.queryForObject("SELECT delivery_state FROM tb_worklog_ai_post_process_outbox WHERE request_id = ?",
                String.class, request)).isEqualTo("PENDING");
        jdbcTemplate.update("UPDATE tb_worklog_ai_post_process_outbox SET next_attempt_at = CURRENT_TIMESTAMP WHERE request_id = ?", request);
        var retry = taskRepository.claimDelivery(109L, UUID.randomUUID(), true, true).orElseThrow();
        assertThat(retry.attempts()).isEqualTo(2);
    }

    @Test
    void crashedFourthDeliveryDoesNotRemainIndefinitelyPending() {
        UUID request = UUID.randomUUID();
        insert(110L, "SUMMARY", 110L, request);
        jdbcTemplate.update("UPDATE tb_worklog_ai_post_process_outbox SET input_hash = ?, attempts = 3 WHERE request_id = ?",
                "e".repeat(64), request);
        var claim = taskRepository.claimDelivery(110L, UUID.randomUUID(), true, true).orElseThrow();
        jdbcTemplate.update("UPDATE tb_worklog_ai_post_process_outbox SET lease_until = CURRENT_TIMESTAMP - INTERVAL '1 second' WHERE request_id = ?", request);
        assertThat(taskRepository.recoveryCandidates(20)).extracting(item -> item.requestId()).contains(request);
        assertThat(taskRepository.deliveryFailed(claim, "DELIVERY_UNCONFIRMED")).isTrue();
        assertThat(taskRepository.deliveryAccepted(claim)).isFalse();
        assertThat(jdbcTemplate.queryForObject("SELECT delivery_state FROM tb_worklog_ai_post_process_outbox WHERE request_id = ?",
                String.class, request)).isEqualTo("EXHAUSTED");
    }

    @Test
    void initialEnqueueCreatesIndependentTasksWithoutReplacingPendingRows() {
        var first = new WorklogAiPostProcessRequestedEvent(111L, "제목", "요청", "내용", 1L, 2L, 3L,
                List.of(new WorklogAiPostProcessRequestedEvent.FileSummaryTarget(900L, "key", "a.pdf", "pdf"),
                        new WorklogAiPostProcessRequestedEvent.FileSummaryTarget(901L, "key2", "b.pdf", "pdf")));
        taskRepository.enqueueInitialTasks(first);
        var before = jdbcTemplate.queryForList("SELECT task_kind, target_id, request_id, input_hash, payload::text FROM tb_worklog_ai_post_process_outbox WHERE worklog_id = 111 ORDER BY task_kind, target_id");
        assertThat(before).hasSize(4);
        taskRepository.enqueueInitialTasks(new WorklogAiPostProcessRequestedEvent(111L, "변경", "새 요청", "새 내용", 1L, 2L, 3L,
                List.of(new WorklogAiPostProcessRequestedEvent.FileSummaryTarget(900L, "other", "a.pdf", "pdf"))));
        var after = jdbcTemplate.queryForList("SELECT task_kind, target_id, request_id, input_hash, payload::text FROM tb_worklog_ai_post_process_outbox WHERE worklog_id = 111 ORDER BY task_kind, target_id");
        assertThat(after).isEqualTo(before);
        assertThat(before).extracting(row -> row.get("input_hash")).allMatch(value -> ((String) value).matches("[0-9a-f]{64}"));
    }

    @Test
    void terminalResultIsIdempotentAndRejectsStaleOrContradictoryCallbacks() {
        UUID request = UUID.randomUUID();
        UUID execution = UUID.randomUUID();
        insert(112L, "SUMMARY", 112L, request);
        jdbcTemplate.update("UPDATE tb_worklog_ai_post_process_outbox SET input_hash = ? WHERE request_id = ?", "a".repeat(64), request);
        markQueued(request);
        var start = new AiTaskApiDto.Identity(request, execution, "a".repeat(64), "SUMMARY", 112L);
        UUID token = taskRepository.start(112L, start).orElseThrow();
        var current = new AiTaskApiDto.Identity(request, execution, "a".repeat(64), "SUMMARY", 112L);
        var stale = new AiTaskApiDto.Identity(request, UUID.randomUUID(), "a".repeat(64), "SUMMARY", 112L);
        String digest = "b".repeat(64);

        transactionTemplate.executeWithoutResult(ignored -> {
            assertThat(taskRepository.lockResult(112L, stale, "COMPLETED", digest)).isEqualTo(WorklogAiTaskRepository.ResultAuthority.REJECTED);
            assertThat(taskRepository.lockResult(112L, current, "COMPLETED", digest)).isEqualTo(WorklogAiTaskRepository.ResultAuthority.APPLY);
            taskRepository.completeResult(112L, current, "COMPLETED", digest, null);
        });
        transactionTemplate.executeWithoutResult(ignored -> {
            assertThat(taskRepository.lockResult(112L, current, "COMPLETED", digest)).isEqualTo(WorklogAiTaskRepository.ResultAuthority.DUPLICATE);
            assertThat(taskRepository.lockResult(112L, current, "FAILED", digest)).isEqualTo(WorklogAiTaskRepository.ResultAuthority.REJECTED);
            assertThat(taskRepository.lockResult(112L, current, "COMPLETED", "c".repeat(64))).isEqualTo(WorklogAiTaskRepository.ResultAuthority.REJECTED);
        });
    }

    @Test
    void resultAndDomainTransactionRollbackTogether() {
        UUID request = UUID.randomUUID();
        insert(113L, "SUMMARY", 113L, request);
        jdbcTemplate.update("UPDATE tb_worklog_ai_post_process_outbox SET input_hash = ? WHERE request_id = ?", "a".repeat(64), request);
        markQueued(request);
        var start = new AiTaskApiDto.Identity(request, UUID.randomUUID(), "a".repeat(64), "SUMMARY", 113L);
        UUID token = taskRepository.start(113L, start).orElseThrow();
        var current = new AiTaskApiDto.Identity(request, start.executionId(), start.inputHash(), "SUMMARY", 113L);
        assertThatThrownBy(() -> transactionTemplate.executeWithoutResult(ignored -> {
            assertThat(taskRepository.lockResult(113L, current, "FAILED", "e".repeat(64))).isEqualTo(WorklogAiTaskRepository.ResultAuthority.APPLY);
            taskRepository.completeResult(113L, current, "FAILED", "e".repeat(64), "TimeoutError");
            throw new IllegalStateException("notification write failed");
        })).isInstanceOf(IllegalStateException.class);
        assertThat(jdbcTemplate.queryForObject("SELECT execution_state FROM tb_worklog_ai_post_process_outbox WHERE request_id = ?", String.class, request)).isEqualTo("RUNNING");
    }

    @Test
    void lockedTaskIsSkippedWithoutBlockingOtherTask() {
        var event = new WorklogAiPostProcessRequestedEvent(114L, "제목", "요청", "내용", 1L, 2L, 3L, List.of());
        taskRepository.enqueueInitialTasks(event);
        transactionTemplate.executeWithoutResult(ignored -> {
            jdbcTemplate.queryForList("SELECT worklog_id FROM tb_worklog_ai_post_process_outbox WHERE worklog_id = 114 AND task_kind = 'SUMMARY' FOR UPDATE");
            try (var pool = java.util.concurrent.Executors.newSingleThreadExecutor()) {
                var claim = pool.submit(() -> taskRepository.claimDelivery(114L, UUID.randomUUID(), true, true).orElseThrow())
                        .get(3, java.util.concurrent.TimeUnit.SECONDS);
                assertThat(claim.taskKind()).isEqualTo("TAG");
            } catch (Exception exception) {
                throw new AssertionError("잠긴 작업이 다른 작업 선점을 막았습니다", exception);
            }
        });
    }

    @Test
    void schemaHasOnlyCurrentTaskIdentityAndSingleAttemptLease() {
        var columns = jdbcTemplate.queryForList("SELECT column_name FROM information_schema.columns WHERE table_name = 'tb_worklog_ai_post_process_outbox'", String.class);
        assertThat(columns).contains("request_id", "execution_id", "attempts", "lease_until", "retried_from_request_id")
                .doesNotContain("dispatch_seq", "execution_token", "delivery_count", "execution_count", "provider_attempts",
                        "execution_lease_until", "result_deadline_at", "retry_payload", "completed_stages");
        assertThat(jdbcTemplate.queryForObject("SELECT to_regclass('public.tb_worklog_light_gate') IS NULL", Boolean.class)).isTrue();
    }

    /** 각 대상에 독립된 현재 작업 행을 생성한다. */
    private void insert(long worklogId, String kind, long targetId, UUID requestId) {
        jdbcTemplate.update("""
                INSERT INTO tb_worklog_ai_post_process_outbox
                    (worklog_id, task_kind, target_id, request_id, payload)
                VALUES (?, ?, ?, ?, CAST('{}' AS jsonb))
                """, worklogId, kind, targetId, requestId);
    }

    /** 시작 승인은 발행 선점이 완료된 작업에만 허용한다. */
    private void markQueued(UUID requestId) {
        jdbcTemplate.update("UPDATE tb_worklog_ai_post_process_outbox SET execution_state = 'QUEUED', attempts = GREATEST(attempts, 1), lease_until = CURRENT_TIMESTAMP + INTERVAL '10 minutes' WHERE request_id = ?", requestId);
    }
}
