package com.ibank.axwms.domain.worklog.service;

import static org.assertj.core.api.Assertions.assertThat;

import com.ibank.axwms.domain.organization.department.DepartmentStatus;
import com.ibank.axwms.domain.organization.department.entity.Department;
import com.ibank.axwms.domain.organization.department.repository.DepartmentRepository;
import com.ibank.axwms.domain.organization.team.TeamStatus;
import com.ibank.axwms.domain.organization.team.entity.Team;
import com.ibank.axwms.domain.organization.team.repository.TeamRepository;
import com.ibank.axwms.domain.organization.user.EmploymentStatus;
import com.ibank.axwms.domain.organization.user.UserRole;
import com.ibank.axwms.domain.organization.user.entity.User;
import com.ibank.axwms.domain.organization.user.repository.UserRepository;
import com.ibank.axwms.domain.worklog.WorklogImportance;
import com.ibank.axwms.domain.worklog.WorklogStatus;
import com.ibank.axwms.domain.worklog.entity.Worklog;
import com.ibank.axwms.domain.worklog.repository.WorklogLightOutboxRepository;
import com.ibank.axwms.domain.worklog.repository.WorklogRepository;
import com.ibank.axwms.testsupport.IntegrationTestSupport;
import com.ibank.axwms.domain.notification.redis.NotificationRedisStreamPublisher;
import com.ibank.axwms.domain.notification.repository.jooq.projection.NotificationSearchProjection;
import com.ibank.axwms.domain.worklog.dto.TriggerWorklogLightIndexDto;
import com.ibank.axwms.domain.worklog.external.WorklogLightIndexClient;
import com.ibank.axwms.domain.worklog.controller.InternalWorklogLightIndexController;
import com.ibank.axwms.domain.worklog.external.AiWorklogLightIndexProperties;
import com.ibank.axwms.global.error.GlobalExceptionHandler;
import com.ibank.axwms.global.response.GlobalResponseAdvice;
import java.time.Duration;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;
import java.math.BigDecimal;
import java.time.LocalDate;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import org.jooq.DSLContext;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import com.ibank.axwms.global.security.CustomUserPrincipal;
import com.ibank.axwms.global.error.BusinessException;
import com.ibank.axwms.global.error.ErrorCode;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.jdbc.core.JdbcTemplate;

class WorklogLightTaskResultIntegrationTest extends IntegrationTestSupport {
    @Autowired private JdbcTemplate jdbc;
    @Autowired private org.springframework.transaction.PlatformTransactionManager transactionManager;
    @Autowired private DepartmentRepository departments;
    @Autowired private UserRepository users;
    @Autowired private TeamRepository teams;
    @Autowired private WorklogRepository worklogs;
    @Autowired private WorklogLightOutboxRepository outbox;
    @Autowired private InternalWorklogLightIndexService service;
    @Autowired private WorklogAiTaskRetryService retry;
    @Autowired private WorklogLightOutboxWorker lightWorker;
    @Autowired private DSLContext dsl;
    @MockitoBean private WorklogLightIndexClient lightClient;
    @MockitoBean private NotificationRedisStreamPublisher streamPublisher;
    private static final String INTERNAL_TOKEN = "light-index-test-token";
    @Autowired private javax.sql.DataSource dataSource;

    /** 시간 추측 대신 해당 격리 PostgreSQL 세션이 실제 행 잠금을 기다리는지 확인한다. */

    /** 격리 DB에서 각 사례를 이전 실행이 종료된 배포 경계로 시작한다. */

    @ParameterizedTest
    @ValueSource(strings = {"INDEX", "REINDEX", "DELETE"})
    void finalFailureStopsAutomaticDeliveryAndNotifiesAuthorOnce(String operation) {
        Worklog worklog = seed();
        if ("DELETE".equals(operation)) {
            worklog.softDelete();
            worklogs.saveAndFlush(worklog);
        }
        outbox.enqueue(worklog.getId(), operation);
        UUID request = jdbc.queryForObject("SELECT request_id FROM tb_worklog_light_outbox WHERE worklog_id = ?", UUID.class, worklog.getId());
        UUID execution = UUID.randomUUID();
        assertThat(start(worklog.getId(), request, execution)).isTrue();
        service.result(worklog.getId(), request, execution, "FAILED", "INDEX_FAILED");
        service.result(worklog.getId(), request, execution, "FAILED", "INDEX_FAILED");
        assertThat(jdbc.queryForObject("SELECT terminal_failed FROM tb_worklog_light_outbox WHERE worklog_id = ?", Boolean.class, worklog.getId())).isTrue();
        assertThat(outbox.claim(worklog.getId(), UUID.randomUUID())).isEmpty();
        assertThat(jdbc.queryForObject("SELECT count(*) FROM tb_notification WHERE ai_request_id = ? AND user_id = ? AND ai_result = 'FAILED'",
                Integer.class, request, worklog.getAuthorId())).isEqualTo(1);
    }

    @Test
    void deletedIndexRetryIsAuthorOnlyAndConcurrentClicksReturnSameRequest() throws Exception {
        Worklog worklog = seed();
        worklog.softDelete();
        worklogs.saveAndFlush(worklog);
        outbox.enqueue(worklog.getId(), "DELETE");
        UUID oldRequest = jdbc.queryForObject("SELECT request_id FROM tb_worklog_light_outbox WHERE worklog_id = ?", UUID.class, worklog.getId());
        UUID execution = UUID.randomUUID();
        assertThat(start(worklog.getId(), oldRequest, execution)).isTrue();
        service.result(worklog.getId(), oldRequest, execution, "FAILED", "INDEX_FAILED");

        var other = new CustomUserPrincipal(worklog.getAuthorId() + 10000, "other@test.invalid", "MEMBER");
        assertThatThrownBy(() -> retry.retry(other, worklog.getId(), oldRequest)).isInstanceOf(BusinessException.class);
        var author = new CustomUserPrincipal(worklog.getAuthorId(), "author@test.invalid", "MEMBER");
        var gate = new CountDownLatch(1);
        try (var pool = Executors.newFixedThreadPool(2)) {
            var first = pool.submit(() -> { gate.await(); return retry.retry(author, worklog.getId(), oldRequest); });
            var second = pool.submit(() -> { gate.await(); return retry.retry(author, worklog.getId(), oldRequest); });
            gate.countDown();
            UUID next = first.get(10, TimeUnit.SECONDS);
            assertThat(next).isNotEqualTo(oldRequest).isEqualTo(second.get(10, TimeUnit.SECONDS));
            assertThat(retry.retry(author, worklog.getId(), oldRequest)).isEqualTo(next);
            assertThat(jdbc.queryForObject("SELECT count(*) FROM tb_worklog_light_outbox WHERE worklog_id = ?", Integer.class, worklog.getId())).isEqualTo(1);
            assertThat(jdbc.queryForObject("SELECT is_deleted FROM tb_worklog WHERE worklog_id = ?", Boolean.class, worklog.getId())).isTrue();
        }
    }

    @Test
    @DisplayName("최신 색인이 완료된 뒤 이전 실패 알림의 재시도를 거부한다")
    void oldFailureNotificationCannotRetryAfterNewIndexCompleted() {
        Worklog worklog = seed();
        Long id = worklog.getId();
        outbox.enqueue(id, "INDEX");
        UUID oldRequest = jdbc.queryForObject("SELECT request_id FROM tb_worklog_light_outbox WHERE worklog_id = ?", UUID.class, id);
        UUID oldExecution = UUID.randomUUID();
        assertThat(start(id, oldRequest, oldExecution)).isTrue();
        service.result(id, oldRequest, oldExecution, "FAILED", "INDEX_FAILED");

        assertThat(jdbc.queryForObject("SELECT count(*) FROM tb_notification WHERE ai_request_id = ? AND ai_result = 'FAILED'", Integer.class, oldRequest)).isEqualTo(1);
        UUID notificationRequest = jdbc.queryForObject("SELECT ai_request_id FROM tb_notification WHERE ai_request_id = ? AND ai_result = 'FAILED'", UUID.class, oldRequest);

        jdbc.update("UPDATE tb_worklog SET work_content = ? WHERE worklog_id = ?", "수정된 본문", id);
        outbox.enqueue(id, "REINDEX");
        UUID latestRequest = jdbc.queryForObject("SELECT request_id FROM tb_worklog_light_outbox WHERE worklog_id = ?", UUID.class, id);
        UUID latestExecution = UUID.randomUUID();
        assertThat(latestRequest).isNotEqualTo(oldRequest);
        assertThat(start(id, latestRequest, latestExecution)).isTrue();
        service.result(id, latestRequest, latestExecution, "COMPLETED", null);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM tb_worklog_light_outbox WHERE worklog_id = ?", Integer.class, id)).isZero();

        var author = new CustomUserPrincipal(worklog.getAuthorId(), "author@test.invalid", "MEMBER");
        assertThatThrownBy(() -> retry.retry(author, id, notificationRequest))
                .isInstanceOf(BusinessException.class)
                .extracting(error -> ((BusinessException) error).getErrorCode())
                .isEqualTo(ErrorCode.WORKLOG_AI_RETRY_STATUS_INVALID);
        assertThat(start(id, oldRequest, UUID.randomUUID())).isFalse();
        assertThat(jdbc.queryForObject("SELECT count(*) FROM tb_worklog_light_outbox WHERE worklog_id = ?", Integer.class, id)).isZero();
        assertThat(jdbc.queryForObject("SELECT light_index_request_id FROM tb_worklog WHERE worklog_id = ?", UUID.class, id)).isEqualTo(latestRequest);
        assertThat(jdbc.queryForObject("SELECT light_index_execution_id FROM tb_worklog WHERE worklog_id = ?", UUID.class, id)).isEqualTo(latestExecution);
        assertThat(jdbc.queryForObject("SELECT light_index_status FROM tb_worklog WHERE worklog_id = ?", String.class, id)).isEqualTo("COMPLETED");
        assertThat(jdbc.queryForObject("SELECT count(*) FROM tb_notification WHERE ai_request_id = ? AND ai_result = 'FAILED'", Integer.class, oldRequest)).isEqualTo(1);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM tb_notification WHERE ai_request_id = ? AND ai_result = 'COMPLETED'", Integer.class, latestRequest)).isEqualTo(1);
    }

    @Test
    void teamWithoutDepartmentCanReceiveIndexResultNotification() {
        Worklog worklog = seedWithoutTeamDepartment();
        outbox.enqueue(worklog.getId(), "INDEX");
        UUID request = jdbc.queryForObject("SELECT request_id FROM tb_worklog_light_outbox WHERE worklog_id = ?", UUID.class, worklog.getId());
        UUID execution = UUID.randomUUID();
        assertThat(start(worklog.getId(), request, execution)).isTrue();
        service.result(worklog.getId(), request, execution, "FAILED", "INDEX_FAILED");
        assertThat(jdbc.queryForObject("SELECT count(*) FROM tb_notification WHERE ai_request_id = ?", Integer.class, request)).isEqualTo(1);
    }

    @Test
    void indexStartRejectsOtherExecutionUntilCurrentAttemptReportsFailure() {
        Worklog worklog = seed();
        outbox.enqueue(worklog.getId(), "INDEX");
        UUID request = jdbc.queryForObject("SELECT request_id FROM tb_worklog_light_outbox WHERE worklog_id = ?", UUID.class, worklog.getId());
        UUID first = UUID.randomUUID();
        UUID second = UUID.randomUUID();
        assertThat(start(worklog.getId(), request, first)).isTrue();
        assertThat(service.start(worklog.getId(), request, first)).isTrue();
        assertThat(start(worklog.getId(), request, second)).isFalse();

        assertThat(service.attemptFailed(worklog.getId(), request, first)).isTrue();
        assertThat(service.attemptFailed(worklog.getId(), request, first)).isTrue();
        jdbc.update("UPDATE tb_worklog_light_outbox SET next_attempt_at = CURRENT_TIMESTAMP WHERE worklog_id = ?", worklog.getId());
        outbox.claim(worklog.getId(), UUID.randomUUID());
        assertThat(start(worklog.getId(), request, second)).isTrue();
        assertThat(jdbc.queryForObject("SELECT attempts FROM tb_worklog_light_outbox WHERE worklog_id = ?", Integer.class, worklog.getId())).isEqualTo(2);
    }

    @Test
    @DisplayName("내부 결과 콜백 HTTP 재전송은 같은 최신 결과를 200으로 멱등 ACK하고 알림·상태를 한 번만 반영한다")
    void repeatedLatestResultOverHttpIsIdempotentAck() throws Exception {
        Worklog worklog = seed();
        Long id = worklog.getId();
        outbox.enqueue(id, "INDEX");
        UUID request = currentRequest(id);
        UUID execution = UUID.randomUUID();
        outbox.claim(id, UUID.randomUUID());
        MockMvc mvc = internalLightIndexMvc();
        String ids = "\"requestId\":\"" + request + "\",\"executionId\":\"" + execution + "\"";
        mvc.perform(post("/internal/worklogs/" + id + "/light-index/start").header("X-AI-Internal-Token", INTERNAL_TOKEN)
                        .contentType(MediaType.APPLICATION_JSON).content("{" + ids + "}"))
                .andExpect(status().isOk()).andExpect(jsonPath("$.execute").value(true));
        String body = "{" + ids + ",\"status\":\"COMPLETED\",\"error\":null}";
        Map<String, Object> completed = null;
        for (int delivery = 0; delivery < 3; delivery++) {
            mvc.perform(post("/internal/worklogs/" + id + "/light-index/result").header("X-AI-Internal-Token", INTERNAL_TOKEN)
                            .contentType(MediaType.APPLICATION_JSON).content(body))
                    .andExpect(status().isOk());
            if (completed == null) {
                completed = worklogState(id);
            }
            assertThat(worklogState(id)).as("delivery %s", delivery).isEqualTo(completed);
        }
        assertThat(completed.get("light_index_status")).isEqualTo("COMPLETED");
        assertThat(jdbc.queryForObject("SELECT count(*) FROM tb_notification WHERE ai_request_id = ?", Integer.class, request)).isEqualTo(1);
        assertThat(outboxCount(id)).isZero();
        // 같은 실행의 모순 실패 재통보도 HTTP 계층에서 오류 없이 무시된다.
        mvc.perform(post("/internal/worklogs/" + id + "/light-index/result").header("X-AI-Internal-Token", INTERNAL_TOKEN)
                        .contentType(MediaType.APPLICATION_JSON).content("{" + ids + ",\"status\":\"FAILED\",\"error\":\"INDEX_FAILED\"}"))
                .andExpect(status().isOk());
        assertThat(worklogState(id)).isEqualTo(completed);
        assertThat(notifications(request, "FAILED")).isZero();
    }

    @Test
    @DisplayName("최신 성공 결과가 동시에 반복 도착해도 알림 1건만 남기고 모순 실패는 현재값을 바꾸지 않는다")
    void concurrentLatestSuccessReplaysNotifyOnceAndIgnoreContradiction() throws Exception {
        Worklog worklog = seed();
        Long id = worklog.getId();
        outbox.enqueue(id, "INDEX");
        UUID request = currentRequest(id);
        UUID execution = UUID.randomUUID();
        assertThat(start(id, request, execution)).isTrue();
        var gate = new CountDownLatch(1);
        try (var pool = Executors.newFixedThreadPool(2)) {
            var a = pool.submit(() -> { gate.await(); service.result(id, request, execution, "COMPLETED", null); return null; });
            var b = pool.submit(() -> { gate.await(); service.result(id, request, execution, "COMPLETED", null); return null; });
            gate.countDown();
            a.get(10, TimeUnit.SECONDS);
            b.get(10, TimeUnit.SECONDS);
        }
        var completed = worklogState(id);
        service.result(id, request, execution, "FAILED", "INDEX_FAILED");
        service.result(id, request, execution, "COMPLETED", null);
        assertThat(worklogState(id)).isEqualTo(completed);
        assertThat(completed.get("light_index_status")).isEqualTo("COMPLETED");
        assertThat(notifications(request, "COMPLETED")).isEqualTo(1);
        assertThat(notifications(request, "FAILED")).isZero();
        assertThat(outboxCount(id)).isZero();
    }

    @Test
    @DisplayName("이전 요청 결과가 교체 전후 어느 순서로 도착해도 최신 요청 상태와 Outbox를 바꾸지 않는다")
    void oldResultsInAnyOrderAreStaleAcksForReplacement() {
        Worklog worklog = seed();
        Long id = worklog.getId();
        outbox.enqueue(id, "INDEX");
        UUID requestA = currentRequest(id);
        UUID executionA = UUID.randomUUID();
        assertThat(start(id, requestA, executionA)).isTrue();
        outbox.enqueue(id, "REINDEX");
        UUID requestB = currentRequest(id);
        var pendingB = outboxState(id);

        service.result(id, requestA, executionA, "FAILED", "INDEX_FAILED");
        assertThat(outboxState(id)).isEqualTo(pendingB);
        assertThat(worklogState(id).get("light_index_status")).isEqualTo("PENDING");

        UUID executionB = UUID.randomUUID();

        assertThat(start(id, requestB, executionB)).isTrue();
        var runningB = worklogState(id);
        service.result(id, requestA, executionA, "COMPLETED", null);
        assertThat(service.attemptFailed(id, requestA, executionA)).isFalse();
        assertThat(worklogState(id)).isEqualTo(runningB);

        service.result(id, requestB, executionB, "COMPLETED", null);
        var completedB = worklogState(id);
        // B 성공으로 Outbox 가 삭제된 뒤의 지연 콜백이 행을 되살리거나 알림을 만들면 안 된다.
        service.result(id, requestA, executionA, "COMPLETED", null);
        service.result(id, requestA, executionA, "FAILED", "INDEX_FAILED");
        assertThat(start(id, requestA, UUID.randomUUID())).isFalse();
        assertThat(worklogState(id)).isEqualTo(completedB);
        assertThat(completedB.get("light_index_request_id")).isEqualTo(requestB);
        assertThat(outboxCount(id)).isZero();
        assertThat(jdbc.queryForObject("SELECT count(*) FROM tb_notification WHERE ai_request_id = ?", Integer.class, requestA)).isZero();
        assertThat(notifications(requestB, "COMPLETED")).isEqualTo(1);
    }

    @Test
    @DisplayName("알림 저장이 실패하면 결과·상태·Outbox를 함께 되돌리고 재전달에서 한 번만 반영한다")
    void notificationFailureRollsBackResultUntilRedelivery() {
        Worklog worklog = seed();
        Long id = worklog.getId();
        outbox.enqueue(id, "INDEX");
        UUID request = currentRequest(id);
        UUID execution = UUID.randomUUID();
        assertThat(start(id, request, execution)).isTrue();
        var running = worklogState(id);
        var outboxRunning = outboxState(id);
        String suffix = UUID.randomUUID().toString().replace("-", "").substring(0, 12);
        String function = "test_fail_notification_" + suffix;
        String trigger = "test_fail_notification_trigger_" + suffix;
        jdbc.execute("CREATE FUNCTION " + function + "() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
                + "RAISE EXCEPTION 'TEST_NOTIFICATION_FAILURE'; END $$");
        try {
            jdbc.execute("CREATE TRIGGER " + trigger + " BEFORE INSERT ON tb_notification FOR EACH ROW WHEN (NEW.ai_request_id = '"
                    + request + "'::uuid) EXECUTE FUNCTION " + function + "()");
            assertThatThrownBy(() -> service.result(id, request, execution, "COMPLETED", null))
                    .hasMessageContaining("TEST_NOTIFICATION_FAILURE");
            // 업무 상태 UPDATE 는 알림 INSERT 보다 먼저 실행되므로 이 비교가 앞선 변경의 rollback 을 증명한다.
            assertThat(worklogState(id)).isEqualTo(running);
            assertThat(outboxState(id)).isEqualTo(outboxRunning);
            assertThat(jdbc.queryForObject("SELECT count(*) FROM tb_notification WHERE ai_request_id = ?", Integer.class, request)).isZero();
        } finally {
            jdbc.execute("DROP TRIGGER IF EXISTS " + trigger + " ON tb_notification");
            jdbc.execute("DROP FUNCTION IF EXISTS " + function + "()");
        }
        service.result(id, request, execution, "COMPLETED", null);
        service.result(id, request, execution, "COMPLETED", null);
        assertThat(worklogState(id).get("light_index_status")).isEqualTo("COMPLETED");
        assertThat(notifications(request, "COMPLETED")).isEqualTo(1);
        assertThat(outboxCount(id)).isZero();
    }

    @Test
    @DisplayName("실시간 전파가 실패해도 커밋된 결과 알림은 DB에서 조회된다")
    void streamFailureKeepsCommittedNotification() {
        Worklog worklog = seed();
        Long id = worklog.getId();
        outbox.enqueue(id, "INDEX");
        UUID request = currentRequest(id);
        UUID execution = UUID.randomUUID();
        assertThat(start(id, request, execution)).isTrue();
        doThrow(new IllegalStateException("stream down")).when(streamPublisher).publish(any());
        service.result(id, request, execution, "FAILED", "INDEX_FAILED");
        verify(streamPublisher).publish(any());
        assertThat(notifications(request, "FAILED")).isEqualTo(1);
        assertThat(worklogState(id).get("light_index_status")).isEqualTo("FAILED");
        assertThat(retryable(request)).isTrue();
    }

    @Test
    @DisplayName("이전 실패 알림은 최신 요청의 대기·실행·성공 어느 단계에서도 재시도 불가로 표시되고 거절된다")
    void retryablePredicateMatchesEndpointWhileReplacementProgresses() throws Exception {
        var author = (java.util.function.Function<Worklog, CustomUserPrincipal>) worklog ->
                new CustomUserPrincipal(worklog.getAuthorId(), "author@test.invalid", "MEMBER");
        for (String stage : List.of("PENDING", "RUNNING", "COMPLETED")) {
            Worklog worklog = seed();
            Long id = worklog.getId();
            UUID requestA = failCurrentRequest(id, "INDEX");
            assertThat(retryable(requestA)).isTrue();
            outbox.enqueue(id, "REINDEX");
            UUID requestB = currentRequest(id);
            UUID executionB = UUID.randomUUID();
            if (!"PENDING".equals(stage)) {
                assertThat(start(id, requestB, executionB)).isTrue();
            }
            if ("COMPLETED".equals(stage)) {
                service.result(id, requestB, executionB, "COMPLETED", null);
            }
            var before = outboxState(id);
            var worklogBefore = worklogState(id);
            int postProcessRowsBefore = postProcessRowCount(id);
            assertThat(retryable(requestA)).as(stage).isFalse();
            assertThatThrownBy(() -> retry.retry(author.apply(worklog), id, requestA))
                    .isInstanceOf(BusinessException.class)
                    .extracting(error -> ((BusinessException) error).getErrorCode())
                    .isEqualTo(ErrorCode.WORKLOG_AI_RETRY_STATUS_INVALID);
            // 동시 재클릭도 각 트랜잭션이 같은 판정으로 거절돼야 하며 어느 쪽도 새 요청을 만들면 안 된다.
            var gate = new CountDownLatch(1);
            try (var pool = Executors.newFixedThreadPool(2)) {
                var clicks = List.of(
                        pool.submit(() -> { gate.await(); return retryErrorCode(author.apply(worklog), id, requestA); }),
                        pool.submit(() -> { gate.await(); return retryErrorCode(author.apply(worklog), id, requestA); }));
                gate.countDown();
                for (var click : clicks) {
                    assertThat(click.get(10, TimeUnit.SECONDS)).as(stage).isEqualTo(ErrorCode.WORKLOG_AI_RETRY_STATUS_INVALID);
                }
            }
            assertThat(outboxState(id)).as(stage).isEqualTo(before);
            assertThat(worklogState(id)).as(stage).isEqualTo(worklogBefore);
            assertThat(postProcessRowCount(id)).as(stage).isEqualTo(postProcessRowsBefore);
        }
        Worklog failed = seed();
        UUID current = failCurrentRequest(failed.getId(), "INDEX");
        var gate = new CountDownLatch(1);
        UUID next;
        try (var pool = Executors.newFixedThreadPool(2)) {
            var first = pool.submit(() -> { gate.await(); return retry.retry(author.apply(failed), failed.getId(), current); });
            var second = pool.submit(() -> { gate.await(); return retry.retry(author.apply(failed), failed.getId(), current); });
            gate.countDown();
            next = first.get(10, TimeUnit.SECONDS);
            assertThat(second.get(10, TimeUnit.SECONDS)).isEqualTo(next);
        }
        assertThat(next).isNotEqualTo(current);
        assertThat(retryable(current)).isFalse();
        assertThat(retry.retry(author.apply(failed), failed.getId(), current)).isEqualTo(next);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM tb_worklog_light_outbox WHERE worklog_id = ? AND request_id = ?",
                Integer.class, failed.getId(), next)).isEqualTo(1);
        assertThat(postProcessRowCount(failed.getId())).isZero();
    }

    /** 재시도 거절을 스레드 경계 밖에서 비교할 수 있도록 비즈니스 오류 코드로 바꾸며, 성공하면 null 이다. */
    private ErrorCode retryErrorCode(CustomUserPrincipal principal, Long id, UUID request) {
        try {
            retry.retry(principal, id, request);
            return null;
        } catch (BusinessException error) {
            return error.getErrorCode();
        }
    }

    /** 재시도가 일반 AI 작업·등록 집계 Outbox 에 새 행을 만들었는지 업무 단위로 센다. */
    private int postProcessRowCount(Long id) {
        return jdbc.queryForObject("SELECT count(*) FROM tb_worklog_ai_post_process_outbox WHERE worklog_id = ?", Integer.class, id);
    }

    @Test
    void repeatedAndContradictoryResultCannotChangeTerminalState() throws Exception {
        Worklog worklog = seed();
        Long id = worklog.getId();
        outbox.enqueue(id, "INDEX");
        UUID request = jdbc.queryForObject("SELECT request_id FROM tb_worklog_light_outbox WHERE worklog_id = ?", UUID.class, id);
        UUID execution = UUID.randomUUID();
        assertThat(start(id, request, execution)).isTrue();
        var gate = new CountDownLatch(1);
        try (var pool = Executors.newFixedThreadPool(2)) {
            var a = pool.submit(() -> { gate.await(); service.result(id, request, execution, "FAILED", "INDEX_FAILED"); return null; });
            var b = pool.submit(() -> { gate.await(); service.result(id, request, execution, "FAILED", "INDEX_FAILED"); return null; });
            gate.countDown();
            a.get(10, TimeUnit.SECONDS);
            b.get(10, TimeUnit.SECONDS);
        }
        service.result(id, request, execution, "COMPLETED", null);
        assertThat(jdbc.queryForObject("SELECT light_index_status FROM tb_worklog WHERE worklog_id = ?", String.class, id)).isEqualTo("FAILED");
        assertThat(jdbc.queryForObject("SELECT count(*) FROM tb_notification WHERE ai_request_id = ?", Integer.class, request)).isEqualTo(1);
    }

    @Test
    void fourAutomaticFailuresNotifyOnceThenAllowManualRetry() {
        var worklog = seed();
        Long id = worklog.getId();
        outbox.enqueue(id, "INDEX");
        UUID request = currentRequest(id);
        for (int attempt = 1; attempt <= 4; attempt++) {
            jdbc.update("UPDATE tb_worklog_light_outbox SET next_attempt_at = CURRENT_TIMESTAMP WHERE worklog_id = ?", id);
            assertThat(outbox.claim(id, UUID.randomUUID())).hasSize(1);
            UUID execution = UUID.randomUUID();
            assertThat(start(id, request, execution)).isTrue();
            assertThat(start(id, request, execution)).isTrue();
            boolean scheduled = service.attemptFailed(id, request, execution);
            assertThat(scheduled).isEqualTo(attempt < 4);
            if (scheduled) {
                assertThat(service.attemptFailed(id, request, execution)).isTrue();
                assertThat(notifications(request, "FAILED")).isZero();
            } else service.result(id, request, execution, "FAILED", "INDEX_FAILED");
        }
        assertThat(outbox.claim(id, UUID.randomUUID())).isEmpty();
        assertThat(notifications(request, "FAILED")).isEqualTo(1);
        assertThat(retryable(request)).isTrue();
        UUID next = retry.retry(new CustomUserPrincipal(worklog.getAuthorId(), "author@test.invalid", "MEMBER"), id, request);
        assertThat(next).isNotEqualTo(request);
        assertThat(jdbc.queryForObject("SELECT attempts FROM tb_worklog_light_outbox WHERE worklog_id = ?", Integer.class, id)).isZero();
    }

    @ParameterizedTest
    @ValueSource(strings = {"PENDING", "RUNNING"})
    void singleLeaseTimeoutFailsAndNotifiesOnce(String state) {
        var worklog = seed();
        Long id = worklog.getId();
        outbox.enqueue(id, "INDEX");
        UUID request = currentRequest(id), execution = UUID.randomUUID();
        var claim = outbox.claim(id, UUID.randomUUID()).getFirst();
        if ("RUNNING".equals(state)) assertThat(start(id, request, execution)).isTrue();
        jdbc.update("UPDATE tb_worklog_light_outbox SET lease_until = CURRENT_TIMESTAMP - INTERVAL '1 second' WHERE worklog_id = ?", id);
        assertThat(service.reconcile(claim)).isTrue();
        assertThat(service.reconcile(claim)).isFalse();
        service.result(id, request, execution, "COMPLETED", null);
        assertThat(worklogState(id).get("light_index_status")).isEqualTo("FAILED");
        assertThat(notifications(request, "FAILED")).isEqualTo(1);
        assertThat(retryable(request)).isTrue();
    }

    @Test
    void startBeforeClaimOrBetweenAutomaticAttemptsIsRejected() {
        var worklog = seed();
        Long id = worklog.getId();
        outbox.enqueue(id, "INDEX");
        UUID request = currentRequest(id), execution = UUID.randomUUID();
        assertThat(service.start(id, request, execution)).isFalse();
        outbox.claim(id, UUID.randomUUID());
        assertThat(service.start(id, request, execution)).isTrue();
        assertThat(service.attemptFailed(id, request, execution)).isTrue();
        assertThat(service.start(id, request, UUID.randomUUID())).isFalse();
        assertThat(jdbc.queryForObject("SELECT attempts FROM tb_worklog_light_outbox WHERE worklog_id = ?", Integer.class, id)).isEqualTo(1);
    }

    /** 테스트도 실제 발행 선점을 거쳐 시작해 시도 횟수와 단일 lease 계약을 지킨다. */
    private boolean start(Long id, UUID request, UUID execution) {
        if (request.equals(currentRequest(id))) {
            jdbc.update("UPDATE tb_worklog_light_outbox SET next_attempt_at = CURRENT_TIMESTAMP WHERE worklog_id = ?", id);
            outbox.claim(id, UUID.randomUUID());
        }
        return service.start(id, request, execution);
    }

    /** 실제 FK를 충족하는 업무와 작성자를 테스트 전용 DB에 생성한다. */
    private Worklog seed() {
        return seed(false);
    }

    /** 현재 요청을 실제 승인·최종 실패 경로로 종료해 실패 알림을 만든다. */
    private UUID failCurrentRequest(Long id, String operation) {
        outbox.enqueue(id, operation);
        UUID request = currentRequest(id);
        UUID execution = UUID.randomUUID();
        assertThat(start(id, request, execution)).isTrue();
        service.result(id, request, execution, "FAILED", "INDEX_FAILED");

        return request;
    }

    /** 업무 행이 가리키는 최신 색인 요청을 읽는다. */
    /** 결과 통보와 별도로 승인 소유자의 물리 종료를 통보한다. */


    private UUID currentRequest(Long id) {
        return jdbc.queryForObject("SELECT light_index_request_id FROM tb_worklog WHERE worklog_id = ?", UUID.class, id);
    }

    /** 최신성 판정에 쓰이는 업무 색인 권위 컬럼만 비교용으로 읽는다. */
    private Map<String, Object> worklogState(Long id) {
        return jdbc.queryForMap("""
                SELECT light_index_request_id, light_index_execution_id, light_index_status, light_index_error
                FROM tb_worklog WHERE worklog_id = ?
                """, id);
    }

    /** 교체·재시도·예산 판정에 쓰이는 Outbox 컬럼만 비교용으로 읽으며 완료 삭제된 경우 빈 목록이다. */
    private List<Map<String, Object>> outboxState(Long id) {
        return jdbc.queryForList("""
                SELECT request_id, revision, operation, attempts, terminal_failed, retried_from_request_id
                FROM tb_worklog_light_outbox WHERE worklog_id = ?
                """, id);
    }

    /** 업무별 Outbox 행 수로 완료 삭제와 부활 여부를 판정한다. */
    private int outboxCount(Long id) {
        return jdbc.queryForObject("SELECT count(*) FROM tb_worklog_light_outbox WHERE worklog_id = ?", Integer.class, id);
    }

    /** 요청·결과별 알림 수로 중복·모순 알림 여부를 판정한다. */
    private int notifications(UUID request, String result) {
        return jdbc.queryForObject("SELECT count(*) FROM tb_notification WHERE ai_request_id = ? AND ai_result = ?",
                Integer.class, request, result);
    }

    /** 알림 목록이 실제로 사용하는 retryable 식으로 해당 요청 알림의 표시값을 계산한다. */
    private boolean retryable(UUID request) {
        return Boolean.TRUE.equals(dsl.select(NotificationSearchProjection.RETRYABLE).from("tb_notification")
                .where("ai_request_id = ?", request).fetchOne(0, Boolean.class));
    }

    /** 등록 집계 깨우기 rollback 을 관찰할 수 있도록 기한 미도래 LEGACY 집계 행을 만든다. */


    /** 결과 처리가 함께 바꾸는 등록 집계 행의 대기 시각을 비교용으로 읽는다. */
    private Map<String, Object> registrationState(Long id) {
        return jdbc.queryForMap("""
                SELECT next_attempt_at, terminal_failed FROM tb_worklog_ai_post_process_outbox
                WHERE worklog_id = ? AND task_kind = 'LEGACY'
                """, id);
    }

    /** 등록 집계가 즉시 처리 가능한 상태로 깨워졌는지 판정한다. */


    /** reconcile 의 terminal 기록(retryResult)이 바꾸는 Light Outbox 컬럼만 비교용으로 읽는다. */
    private Map<String, Object> lightOutboxResultState(Long id) {
        return jdbc.queryForMap("SELECT terminal_failed, last_error, attempts FROM tb_worklog_light_outbox WHERE worklog_id = ?", id);
    }

    /** 테스트 전용 trigger 로 해당 요청의 알림 INSERT 만 실패시키고, 끝나면 반드시 제거한다. */
    private void withFailingNotification(UUID request, Runnable action) {
        String suffix = UUID.randomUUID().toString().replace("-", "").substring(0, 12);
        String function = "test_fail_notification_" + suffix;
        String trigger = "test_fail_notification_trigger_" + suffix;
        jdbc.execute("CREATE FUNCTION " + function + "() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
                + "RAISE EXCEPTION 'TEST_NOTIFICATION_FAILURE'; END $$");
        try {
            jdbc.execute("CREATE TRIGGER " + trigger + " BEFORE INSERT ON tb_notification FOR EACH ROW WHEN (NEW.ai_request_id = '"
                    + request + "'::uuid) EXECUTE FUNCTION " + function + "()");
            action.run();
        } finally {
            jdbc.execute("DROP TRIGGER IF EXISTS " + trigger + " ON tb_notification");
            jdbc.execute("DROP FUNCTION IF EXISTS " + function + "()");
        }
    }

    /** 사용자에게 보이는 알림 제목·본문을 요청 단위로 읽는다. */
    private Map<String, Object> notificationText(UUID request) {
        return jdbc.queryForMap("SELECT title, content FROM tb_notification WHERE ai_request_id = ?", request);
    }

    /** 실제 내부 컨트롤러의 MVC 바인딩·토큰 검증·응답 봉투 위에 실제 서비스와 PostgreSQL 을 연결한다. */
    private MockMvc internalLightIndexMvc() {
        return MockMvcBuilders.standaloneSetup(new InternalWorklogLightIndexController(service,
                        new AiWorklogLightIndexProperties("http://ai/ai", Duration.ofSeconds(1), Duration.ofSeconds(1), INTERNAL_TOKEN)))
                .setControllerAdvice(new GlobalExceptionHandler(), new GlobalResponseAdvice()).build();
    }

    /** 부서 없는 팀도 유효한 알림 대상이므로 별도 경계 사례로 만든다. */
    private Worklog seedWithoutTeamDepartment() {
        return seed(true);
    }

    /** 테스트 fixture의 팀 소속 유무를 선택한다. */
    private Worklog seed(boolean noTeamDepartment) {
        String suffix = UUID.randomUUID().toString().substring(0, 8);
        LocalDate today = LocalDate.of(2026, 11, 1);
        var department = Department.create("색인-" + suffix, "test");
        department.changeStatus(DepartmentStatus.ACTIVE);
        department = departments.saveAndFlush(department);
        var user = users.saveAndFlush(User.create(department.getId(), "작성자", suffix + "@test.invalid",
                "$2a$10$abcdefghijklmnopqrstuv", UserRole.MEMBER, EmploymentStatus.ACTIVE, "사원", null, today, null, null));
        var team = teams.saveAndFlush(Team.create(noTeamDepartment ? null : department.getId(), "색인팀-" + suffix, TeamStatus.ACTIVE, "test", today, null));
        return worklogs.saveAndFlush(Worklog.create(user.getId(), team.getId(), "업무", "요청", "본문",
                WorklogStatus.PENDING, WorklogImportance.NORMAL, BigDecimal.ZERO, today, today.plusDays(1)));
    }
}
