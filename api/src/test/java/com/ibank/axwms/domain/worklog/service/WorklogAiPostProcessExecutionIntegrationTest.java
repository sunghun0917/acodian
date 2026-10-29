package com.ibank.axwms.domain.worklog.service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyBoolean;
import static org.mockito.ArgumentMatchers.anyList;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.doAnswer;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;

import com.ibank.axwms.domain.file.external.FileSummaryClient;
import com.ibank.axwms.domain.notification.service.NotificationService;
import com.ibank.axwms.domain.organization.department.DepartmentStatus;
import com.ibank.axwms.domain.organization.department.entity.Department;
import com.ibank.axwms.domain.organization.department.repository.DepartmentRepository;
import com.ibank.axwms.domain.organization.team.TeamStatus;
import com.ibank.axwms.domain.organization.team.UserTeamStatus;
import com.ibank.axwms.domain.organization.team.entity.Team;
import com.ibank.axwms.domain.organization.team.entity.UserTeam;
import com.ibank.axwms.domain.organization.team.repository.TeamRepository;
import com.ibank.axwms.domain.organization.team.repository.UserTeamRepository;
import com.ibank.axwms.domain.organization.user.EmploymentStatus;
import com.ibank.axwms.domain.organization.user.UserRole;
import com.ibank.axwms.domain.organization.user.entity.User;
import com.ibank.axwms.domain.organization.user.repository.UserRepository;
import com.ibank.axwms.domain.worklog.WorklogImportance;
import com.ibank.axwms.domain.worklog.WorklogStatus;
import com.ibank.axwms.domain.worklog.dto.CreateWorklogApiDto;
import com.ibank.axwms.domain.worklog.dto.TriggerWorklogLightIndexDto;
import com.ibank.axwms.domain.worklog.dto.TriggerWorklogPipelineDto;
import com.ibank.axwms.domain.worklog.entity.Worklog;
import com.ibank.axwms.domain.worklog.event.WorklogAiPostProcessRequestedEvent;
import com.ibank.axwms.domain.worklog.external.WorklogLightIndexClient;
import com.ibank.axwms.domain.worklog.external.WorklogPipelineClient;
import com.ibank.axwms.domain.worklog.repository.WorklogAiPostProcessOutboxRepository;
import com.ibank.axwms.domain.worklog.repository.WorklogAiPostProcessOutboxRepository.Claim;
import com.ibank.axwms.domain.worklog.repository.WorklogRepository;
import com.ibank.axwms.global.enums.AiProcessingStatus;
import com.ibank.axwms.global.security.CustomUserPrincipal;
import com.ibank.axwms.testsupport.IntegrationTestSupport;
import java.math.BigDecimal;
import java.time.LocalDate;
import java.util.List;
import java.util.UUID;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.dao.DataAccessResourceFailureException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.TestPropertySource;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.context.bean.override.mockito.MockitoSpyBean;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;

@TestPropertySource(properties = {
        "ai.worklog-post-process.outbox-poll-delay-ms=3600000",
        "ai.worklog-light-index.outbox-poll-delay-ms=3600000",
        "ai.worklog-pipeline.enabled=true"
})
class WorklogAiPostProcessExecutionIntegrationTest extends IntegrationTestSupport {

    private static final LocalDate TODAY = LocalDate.of(2026, 10, 17);

    @Autowired
    private WorklogAiPostProcessOutboxWorker worker;

    @Autowired
    private WorklogAiPostProcessOutboxRepository outboxRepository;

    @Autowired
    private WorklogService worklogService;

    @Autowired
    private WorklogRepository worklogRepository;

    @Autowired
    private DepartmentRepository departmentRepository;

    @Autowired
    private UserRepository userRepository;

    @Autowired
    private TeamRepository teamRepository;

    @Autowired
    private UserTeamRepository userTeamRepository;

    @Autowired
    private JdbcTemplate jdbcTemplate;

    @Autowired
    private PlatformTransactionManager transactionManager;

    @MockitoBean
    private FileSummaryClient fileSummaryClient;

    @MockitoBean
    private WorklogPipelineClient pipelineClient;

    @MockitoBean
    private WorklogLightIndexClient indexClient;

    @MockitoSpyBean
    private NotificationService notificationService;

    @BeforeEach
    void clearPendingJobs() {
        jdbcTemplate.execute("TRUNCATE TABLE tb_worklog_ai_post_process_outbox");
        jdbcTemplate.execute("TRUNCATE TABLE tb_worklog_light_outbox");
        doAnswer(invocation -> {
            TriggerWorklogLightIndexDto.Request request = invocation.getArgument(0);
            completeIndex(request);
            return null;
        }).when(indexClient).requestIndex(any());
    }

    @Autowired
    private InternalWorklogLightIndexService lightIndexService;

    @MockitoSpyBean
    private com.ibank.axwms.domain.worklog.repository.WorklogLightOutboxRepository lightOutbox;

    @Autowired
    private WorklogAiPostProcessService postProcessService;

    @Autowired
    private WorklogLightOutboxWorker lightWorker;

    @Autowired
    private InternalWorklogAiCallbackService summaryCallbackService;

    @ParameterizedTest
    @ValueSource(booleans = {true, false})
    @DisplayName("등록 최종 실패 집계와 새 재색인 요청은 어느 순서로 경합해도 최신 요청을 재개한다")
    void index_failure_finish_and_reindex_enqueue_serialize_in_both_orders(boolean finishFirst) throws Exception {
        Fixture fixture = seedFixture();
        Worklog worklog = saveProcessingWorklog(fixture);
        enqueueRegistration(fixture, worklog);
        UUID original = currentRequest(worklog.getId());
        UUID execution = UUID.randomUUID();
        assertThat(lightIndexService.start(worklog.getId(), original, execution)).isTrue();
        lightIndexService.result(worklog.getId(), original, execution, "FAILED", "INDEX_FAILED");
        Claim claim = outboxRepository.claim(worklog.getId(), UUID.randomUUID()).getFirst();
        assertThat(outboxRepository.checkpoint(claim, "WORKLOG_PIPELINE")).isTrue();

        String applicationName = "i20-" + UUID.randomUUID();
        var winnerReady = new java.util.concurrent.CountDownLatch(1);
        var loserStarted = new java.util.concurrent.CountDownLatch(1);
        var releaseWinner = new java.util.concurrent.CountDownLatch(1);
        try (var executor = java.util.concurrent.Executors.newFixedThreadPool(2)) {
            var winner = executor.submit(() -> {
                new TransactionTemplate(transactionManager).executeWithoutResult(status -> {
                    jdbcTemplate.queryForObject("SELECT worklog_id FROM tb_worklog WHERE worklog_id = ? FOR UPDATE",
                            Long.class, worklog.getId());
                    if (finishFirst) {
                        postProcessService.finish(claim, new WorklogAiPostProcessService.ProcessResult(List.of()));
                    } else {
                        lightOutbox.enqueue(worklog.getId(), "REINDEX");
                    }
                    winnerReady.countDown();
                    try {
                        if (!releaseWinner.await(10, java.util.concurrent.TimeUnit.SECONDS)) {
                            throw new AssertionError("선행 트랜잭션 해제 시간 초과");
                        }
                    } catch (InterruptedException ex) {
                        Thread.currentThread().interrupt();
                        throw new AssertionError(ex);
                    }
                });
                return true;
            });
            try {
                assertThat(winnerReady.await(10, java.util.concurrent.TimeUnit.SECONDS)).isTrue();
                var loser = executor.submit(() -> {
                    new TransactionTemplate(transactionManager).executeWithoutResult(status -> {
                        jdbcTemplate.queryForObject("SELECT set_config('application_name', ?, true)",
                                String.class, applicationName);
                        loserStarted.countDown();
                        if (finishFirst) {
                            lightOutbox.enqueue(worklog.getId(), "REINDEX");
                        } else {
                            postProcessService.finish(claim, new WorklogAiPostProcessService.ProcessResult(List.of()));
                        }
                    });
                    return true;
                });
                assertThat(loserStarted.await(10, java.util.concurrent.TimeUnit.SECONDS)).isTrue();
                assertThat(awaitPostgresLock(applicationName)).isTrue();
                releaseWinner.countDown();
                assertThat(winner.get(10, java.util.concurrent.TimeUnit.SECONDS)).isTrue();
                assertThat(loser.get(10, java.util.concurrent.TimeUnit.SECONDS)).isTrue();
            } finally {
                releaseWinner.countDown();
            }
        }

        UUID latest = currentRequest(worklog.getId());
        assertThat(latest).isNotEqualTo(original);
        assertThat(jdbcTemplate.queryForObject("""
                SELECT NOT terminal_failed FROM tb_worklog_ai_post_process_outbox WHERE worklog_id = ?
                """, Boolean.class, worklog.getId())).isTrue();
        assertThat(hasCheckpoint(worklog.getId(), "WORKLOG_PIPELINE")).isTrue();
        lightIndexService.result(worklog.getId(), original, execution, "FAILED", "INDEX_FAILED");
        UUID latestExecution = UUID.randomUUID();
        assertThat(lightIndexService.start(worklog.getId(), latest, latestExecution)).isTrue();
        lightIndexService.result(worklog.getId(), latest, latestExecution, "COMPLETED", null);
        makeDue(worklog.getId());
        assertThat(worker.processOne(worklog.getId())).isTrue();
        assertThat(processingStatus(worklog.getId())).isEqualTo(AiProcessingStatus.COMPLETED);
        assertThat(notificationCount(worklog.getId())).isEqualTo(1);
        verifyNoInteractions(pipelineClient);
    }

    /** 경쟁 트랜잭션이 실제 PostgreSQL 행 잠금을 기다리는지 확인한 뒤 순서를 진행한다. */
    private boolean awaitPostgresLock(String applicationName) {
        long deadline = System.nanoTime() + java.util.concurrent.TimeUnit.SECONDS.toNanos(10);
        while (System.nanoTime() < deadline) {
            if (Boolean.TRUE.equals(jdbcTemplate.queryForObject("""
                    SELECT EXISTS (SELECT 1 FROM pg_stat_activity
                                   WHERE application_name = ? AND wait_event_type = 'Lock')
                    """, Boolean.class, applicationName))) {
                return true;
            }
            java.util.concurrent.locks.LockSupport.parkNanos(java.util.concurrent.TimeUnit.MILLISECONDS.toNanos(10));
        }
        return false;
    }

    @Test
    @DisplayName("Outbox가 제거된 과거 작업은 기존 요청 ID를 알아도 시작하거나 결과를 반영하지 못한다")
    void abandoned_outbox_request_cannot_start_or_apply_result() {
        Fixture fixture = seedFixture();
        Worklog worklog = saveProcessingWorklog(fixture);
        enqueueRegistration(fixture, worklog);
        UUID request = currentRequest(worklog.getId());
        UUID execution = UUID.randomUUID();
        assertThat(lightIndexService.start(worklog.getId(), request, execution)).isTrue();
        jdbcTemplate.update("DELETE FROM tb_worklog_light_outbox WHERE worklog_id = ?", worklog.getId());

        assertThat(lightIndexService.start(worklog.getId(), request, UUID.randomUUID())).isFalse();
        lightIndexService.result(worklog.getId(), request, execution, "COMPLETED", null);

        assertThat(jdbcTemplate.queryForObject("SELECT light_index_status FROM tb_worklog WHERE worklog_id = ?",
                String.class, worklog.getId())).isEqualTo("RUNNING");
        assertThat(notificationCount(worklog.getId())).isZero();
    }

    @Test
    @DisplayName("색인 대기 중 일반 AI 실패는 선행 재접수 성공에도 첫 실패 알림으로 보존한다")
    void general_failure_waiting_for_index_is_not_lost_when_retry_succeeds() {
        Fixture fixture = seedFixture();
        Worklog worklog = saveProcessingWorklog(fixture);
        enqueueRegistration(fixture, worklog);
        doThrow(new IllegalStateException("general unavailable")).doNothing()
                .when(pipelineClient).requestPipeline(any());

        assertThat(worker.processOne(worklog.getId())).isTrue();
        assertThat(notificationCount(worklog.getId())).isZero();
        makeDue(worklog.getId());
        assertThat(worker.processOne(worklog.getId())).isTrue();
        assertThat(notificationCount(worklog.getId())).isZero();
        assertThat(hasCheckpoint(worklog.getId(), "WORKLOG_PIPELINE")).isTrue();

        UUID request = currentRequest(worklog.getId());
        UUID execution = UUID.randomUUID();
        assertThat(lightIndexService.start(worklog.getId(), request, execution)).isTrue();
        lightIndexService.result(worklog.getId(), request, execution, "COMPLETED", null);
        assertThat(worker.processOne(worklog.getId())).isTrue();
        assertThat(processingStatus(worklog.getId())).isEqualTo(AiProcessingStatus.FAILED);
        assertThat(notificationCount(worklog.getId())).isEqualTo(1);
        assertThat(jdbcTemplate.queryForObject("""
                SELECT title FROM tb_notification WHERE reference_id = ?
                AND notification_type = 'WORKLOG_AI_POST_PROCESS_RESULT'
                """, String.class, worklog.getId())).contains("실패");

        makeDue(worklog.getId());
        assertThat(worker.processOne(worklog.getId())).isTrue();
        assertThat(processingStatus(worklog.getId())).isEqualTo(AiProcessingStatus.COMPLETED);
        assertThat(notificationCount(worklog.getId())).isEqualTo(1);
        verify(pipelineClient, times(2)).requestPipeline(pipelineRequest(fixture, worklog));
    }

    @Test
    @DisplayName("최초 색인 최종 실패 후 수정 재색인은 등록 집계를 재개하고 이전 콜백을 무시한다")
    void failed_index_then_new_reindex_reactivates_registration_without_duplicate_notification() {
        Fixture fixture = seedFixture();
        Worklog worklog = saveProcessingWorklog(fixture);
        enqueueRegistration(fixture, worklog);
        UUID original = currentRequest(worklog.getId());
        UUID originalExecution = UUID.randomUUID();
        assertThat(lightIndexService.start(worklog.getId(), original, originalExecution)).isTrue();
        lightIndexService.result(worklog.getId(), original, originalExecution, "FAILED", "INDEX_FAILED");
        worker.processOne(worklog.getId());
        assertThat(processingStatus(worklog.getId())).isEqualTo(AiProcessingStatus.FAILED);
        assertThat(notificationCount(worklog.getId())).isEqualTo(1);
        assertThat(jdbcTemplate.queryForObject("""
                SELECT terminal_failed FROM tb_worklog_ai_post_process_outbox WHERE worklog_id = ?
                """, Boolean.class, worklog.getId())).isTrue();

        lightOutbox.enqueue(worklog.getId(), "REINDEX");
        UUID latest = currentRequest(worklog.getId());
        assertThat(latest).isNotEqualTo(original);
        lightIndexService.result(worklog.getId(), original, originalExecution, "COMPLETED", null);
        assertThat(jdbcTemplate.queryForObject("""
                SELECT NOT terminal_failed FROM tb_worklog_ai_post_process_outbox WHERE worklog_id = ?
                """, Boolean.class, worklog.getId())).isTrue();
        worker.processOne(worklog.getId());
        assertThat(processingStatus(worklog.getId())).isEqualTo(AiProcessingStatus.FAILED);

        UUID latestExecution = UUID.randomUUID();
        assertThat(lightIndexService.start(worklog.getId(), latest, latestExecution)).isTrue();
        lightIndexService.result(worklog.getId(), latest, latestExecution, "COMPLETED", null);
        makeDue(worklog.getId());
        worker.processOne(worklog.getId());

        assertThat(processingStatus(worklog.getId())).isEqualTo(AiProcessingStatus.COMPLETED);
        assertThat(notificationCount(worklog.getId())).isEqualTo(1);
        verify(pipelineClient, times(1)).requestPipeline(pipelineRequest(fixture, worklog));
    }

    @Test
    @DisplayName("등록 색인 최종 실패는 자동 재발행 없이 수동 재요청으로만 복구한다")
    void terminal_registration_failure_waits_for_manual_retry() {
        Fixture fixture = seedFixture();
        Worklog worklog = saveProcessingWorklog(fixture);
        org.mockito.Mockito.doNothing().when(indexClient).requestIndex(any());
        enqueueRegistration(fixture, worklog);
        worker.processOne(worklog.getId());
        lightWorker.processOne(worklog.getId());
        UUID oldRequest = currentRequest(worklog.getId());
        UUID execution = UUID.randomUUID();
        lightIndexService.start(worklog.getId(), oldRequest, execution);
        lightIndexService.result(worklog.getId(), oldRequest, execution, "FAILED", "INDEX_FAILED");
        worker.processOne(worklog.getId());

        assertThat(processingStatus(worklog.getId())).isEqualTo(AiProcessingStatus.FAILED);
        assertThat(notificationCount(worklog.getId())).isEqualTo(1);
        summaryCallbackService.updateAiResult(worklog.getId(),
                new com.ibank.axwms.domain.worklog.dto.UpdateWorklogAiApiDto.Request("늦게 도착한 요약", AiProcessingStatus.COMPLETED));
        assertThat(processingStatus(worklog.getId())).isEqualTo(AiProcessingStatus.FAILED);
        makeDue(worklog.getId());
        assertThat(worker.processOne(worklog.getId())).isFalse();
        assertThat(jobCount(worklog.getId())).isEqualTo(1);
        assertThat(lightIndexService.start(worklog.getId(), oldRequest, UUID.randomUUID())).isFalse();
        verify(indexClient, times(1)).requestIndex(any());

        worklogService.retryAiSummary(new CustomUserPrincipal(fixture.user().getId(), fixture.user().getEmail(), UserRole.MEMBER.name()), worklog.getId());
        assertThat(processingStatus(worklog.getId())).isEqualTo(AiProcessingStatus.PROCESSING);
        assertThat(worker.processOne(worklog.getId())).isTrue();
        UUID request = currentRequest(worklog.getId());
        assertThat(request).isNotEqualTo(oldRequest);
        doAnswer(invocation -> { completeIndex(invocation.getArgument(0)); return null; }).when(indexClient).requestIndex(any());
        lightWorker.processOne(worklog.getId());
        makeDue(worklog.getId());
        assertThat(worker.processOne(worklog.getId())).isTrue();
        assertThat(processingStatus(worklog.getId())).isEqualTo(AiProcessingStatus.COMPLETED);
        verify(indexClient, times(2)).requestIndex(any());
    }

    @Test
    @DisplayName("Light Outbox 접수 후 시작·결과가 없으면 같은 요청을 재발행한다")
    void registration_timeout_redelivers_only_unfinished_index() {
        for (boolean started : List.of(false, true)) {
            Fixture fixture = seedFixture();
            Worklog worklog = saveProcessingWorklog(fixture);
            org.mockito.Mockito.doNothing().when(indexClient).requestIndex(any());
            enqueueRegistration(fixture, worklog);
            assertThat(worker.processOne(worklog.getId())).isTrue();
            UUID request = currentRequest(worklog.getId());
            assertThat(lightWorker.processOne(worklog.getId())).isTrue();
            if (started) lightIndexService.start(worklog.getId(), request, UUID.randomUUID());
            makeDue(worklog.getId());
            worker.processOne(worklog.getId());
            verify(indexClient, times(1)).requestIndex(TriggerWorklogLightIndexDto.Request.of(worklog.getId(), request));
            assertThat(processingStatus(worklog.getId())).isEqualTo(AiProcessingStatus.PROCESSING);
            assertThat(notificationCount(worklog.getId())).isZero();
            doAnswer(invocation -> { completeIndex(invocation.getArgument(0)); return null; }).when(indexClient).requestIndex(any());
            makeLightDue(worklog.getId());
            assertThat(lightWorker.processOne(worklog.getId())).isTrue();
            makeDue(worklog.getId());
            assertThat(worker.processOne(worklog.getId())).isTrue();
            verify(indexClient, times(2)).requestIndex(TriggerWorklogLightIndexDto.Request.of(worklog.getId(), request));
            verify(pipelineClient, times(1)).requestPipeline(pipelineRequest(fixture, worklog));
            assertThat(processingStatus(worklog.getId())).isEqualTo(AiProcessingStatus.COMPLETED);
            assertThat(jobCount(worklog.getId())).isZero();
        }
    }

    @Test
    @DisplayName("등록 폴러는 수정 Outbox가 소유한 요청을 만료 재발행하거나 최종 중단하지 않는다")
    void registration_defers_recovery_of_light_outbox_owned_request() {
        Fixture fixture = seedFixture();
        Worklog worklog = saveProcessingWorklog(fixture);
        org.mockito.Mockito.doNothing().when(indexClient).requestIndex(any());
        enqueueRegistration(fixture, worklog);
        worker.processOne(worklog.getId());
        lightWorker.processOne(worklog.getId());
        lightOutbox.enqueue(worklog.getId(), "REINDEX");
        lightWorker.processOne(worklog.getId());
        UUID request = currentRequest(worklog.getId());
        makeLightDue(worklog.getId());
        makeDue(worklog.getId());
        worker.processOne(worklog.getId());
        UUID execution = UUID.randomUUID();
        lightIndexService.start(worklog.getId(), request, execution);
        lightIndexService.result(worklog.getId(), request, execution, "FAILED", "REINDEX_FAILED");
        worker.processOne(worklog.getId());
        assertThat(jdbcTemplate.queryForObject("SELECT terminal_failed FROM tb_worklog_ai_post_process_outbox WHERE worklog_id = ?", Boolean.class, worklog.getId())).isFalse();
        verify(indexClient, times(1)).requestIndex(any());
        assertThat(lightIndexService.start(worklog.getId(), request, UUID.randomUUID())).isTrue();
    }

    @Test
    @DisplayName("최종 실패 뒤 수동 재요청은 이전 재색인 의도를 대체하고 과거 콜백을 차단한다")
    void manual_registration_retry_supersedes_retained_reindex() {
        Fixture fixture = seedFixture();
        Worklog worklog = saveProcessingWorklog(fixture);
        org.mockito.Mockito.doNothing().when(indexClient).requestIndex(any());
        enqueueRegistration(fixture, worklog);
        worker.processOne(worklog.getId());
        lightWorker.processOne(worklog.getId());
        UUID first = currentRequest(worklog.getId());
        UUID execution = UUID.randomUUID();
        lightIndexService.start(worklog.getId(), first, execution);
        lightIndexService.result(worklog.getId(), first, execution, "FAILED", "INDEX_FAILED");
        worker.processOne(worklog.getId());
        lightOutbox.enqueue(worklog.getId(), "REINDEX");
        UUID obsolete = currentRequest(worklog.getId());
        lightWorker.processOne(worklog.getId());
        worklogService.retryAiSummary(new CustomUserPrincipal(fixture.user().getId(), fixture.user().getEmail(), UserRole.MEMBER.name()), worklog.getId());
        worker.processOne(worklog.getId());
        assertThat(currentRequest(worklog.getId())).isNotEqualTo(obsolete);
        assertThat(lightJobCount(worklog.getId())).isEqualTo(1);
        assertThat(lightIndexService.start(worklog.getId(), obsolete, UUID.randomUUID())).isFalse();
    }

    @Test
    @DisplayName("접수 응답보다 빠른 최종 실패 콜백은 결과 대기 기한을 다시 열지 않는다")
    void failed_registration_callback_before_acceptance_stays_terminal() {
        Fixture fixture = seedFixture();
        Worklog worklog = saveProcessingWorklog(fixture);
        enqueueRegistration(fixture, worklog);
        doAnswer(invocation -> {
            TriggerWorklogLightIndexDto.Request request = invocation.getArgument(0);
            UUID execution = UUID.randomUUID();
            lightIndexService.start(worklog.getId(), request.requestId(), execution);
            lightIndexService.result(worklog.getId(), request.requestId(), execution, "FAILED", "INDEX_FAILED");
            return null;
        }).when(indexClient).requestIndex(any());

        worker.processOne(worklog.getId());
        lightWorker.processOne(worklog.getId());
        makeDue(worklog.getId());
        worker.processOne(worklog.getId());

        assertThat(jdbcTemplate.queryForObject("""
                SELECT terminal_failed FROM tb_worklog_light_outbox WHERE worklog_id = ?
                """, Boolean.class, worklog.getId())).isTrue();
        assertThat(processingStatus(worklog.getId())).isEqualTo(AiProcessingStatus.FAILED);
        assertThat(worker.processOne(worklog.getId())).isFalse();
    }

    @Test
    @DisplayName("수동 복구 대기 중 원본이 삭제되면 AI를 재실행하지 않고 등록 Outbox를 정리한다")
    void deleted_source_cleans_suspended_registration_without_retry() {
        Fixture fixture = seedFixture();
        Worklog worklog = saveProcessingWorklog(fixture);
        enqueueRegistration(fixture, worklog);
        doAnswer(invocation -> {
            TriggerWorklogLightIndexDto.Request request = invocation.getArgument(0);
            UUID execution = UUID.randomUUID();
            lightIndexService.start(worklog.getId(), request.requestId(), execution);
            lightIndexService.result(worklog.getId(), request.requestId(), execution, "FAILED", "INDEX_FAILED");
            return null;
        }).when(indexClient).requestIndex(any());
        worker.processOne(worklog.getId());
        lightWorker.processOne(worklog.getId());
        makeDue(worklog.getId());
        worker.processOne(worklog.getId());
        Worklog latest = worklogRepository.findById(worklog.getId()).orElseThrow();
        latest.softDelete();
        worklogRepository.saveAndFlush(latest);

        assertThat(worker.processOne(worklog.getId())).isTrue();

        assertThat(jobCount(worklog.getId())).isZero();
        verify(indexClient, times(1)).requestIndex(any());
        assertThat(notificationCount(worklog.getId())).isEqualTo(1);
    }

    @Test
    @DisplayName("재색인·삭제는 접수 후 보존하고 최신 실행의 완료 콜백에서만 제거한다")
    void light_outbox_waits_for_reindex_and_delete_completion() {
        for (String operation : List.of("REINDEX", "DELETE")) {
            Worklog worklog = saveProcessingWorklog(seedFixture());
            if ("DELETE".equals(operation)) {
                worklog.softDelete();
                worklogRepository.saveAndFlush(worklog);
            }
            lightOutbox.enqueue(worklog.getId(), operation);
            UUID request = currentRequest(worklog.getId());
            assertThat(lightWorker.processOne(worklog.getId())).isTrue();
            if ("DELETE".equals(operation)) {
                verify(indexClient).requestDelete(com.ibank.axwms.domain.worklog.dto.TriggerWorklogLightDeleteDto.Request.of(worklog.getId(), request));
            } else {
                verify(indexClient).requestReindex(TriggerWorklogLightIndexDto.Request.of(worklog.getId(), request));
            }
            assertThat(lightJobCount(worklog.getId())).isEqualTo(1);
            assertThat(lightWorker.processOne(worklog.getId())).isFalse();

            UUID execution = UUID.randomUUID();
            assertThat(lightIndexService.start(worklog.getId(), request, execution)).isTrue();
            lightIndexService.result(worklog.getId(), request, execution, "COMPLETED", null);
            assertThat(lightJobCount(worklog.getId())).isZero();
            lightIndexService.result(worklog.getId(), request, execution, "COMPLETED", null);
            assertThat(lightJobCount(worklog.getId())).isZero();
        }
    }

    @Test
    @DisplayName("최종 실패 콜백은 Outbox를 보존하고 백오프 후 같은 요청으로 재전달한다")
    void light_outbox_retries_failed_results_until_completed() {
        for (String operation : List.of("REINDEX", "DELETE")) {
            Worklog worklog = saveProcessingWorklog(seedFixture());
            lightOutbox.enqueue(worklog.getId(), operation);
            UUID request = currentRequest(worklog.getId());
            lightWorker.processOne(worklog.getId());
            UUID execution = UUID.randomUUID();
            lightIndexService.start(worklog.getId(), request, execution);
            lightIndexService.result(worklog.getId(), request, execution, "FAILED", "AI failed");
            assertThat(jdbcTemplate.queryForObject("""
                    SELECT attempts = 1 AND last_error = 'AI failed' AND next_attempt_at > updated_at
                        AND claim_token IS NULL AND lease_until IS NULL
                    FROM tb_worklog_light_outbox WHERE worklog_id = ?
                    """, Boolean.class, worklog.getId())).isTrue();
            // 중복 실패 통보는 백오프나 재시도 횟수를 다시 변경하지 않는다.
            lightIndexService.result(worklog.getId(), request, execution, "FAILED", "AI failed");
            assertThat(jdbcTemplate.queryForObject("SELECT attempts FROM tb_worklog_light_outbox WHERE worklog_id = ?", Integer.class, worklog.getId())).isEqualTo(1);
            makeLightDue(worklog.getId());
            assertThat(lightWorker.processOne(worklog.getId())).isTrue();
            assertThat(currentRequest(worklog.getId())).isEqualTo(request);
            UUID retryExecution = UUID.randomUUID();
            assertThat(lightIndexService.start(worklog.getId(), request, retryExecution)).isTrue();
            lightIndexService.result(worklog.getId(), request, execution, "COMPLETED", null);
            assertThat(lightJobCount(worklog.getId())).isEqualTo(1);
            lightIndexService.result(worklog.getId(), request, retryExecution, "COMPLETED", null);
            assertThat(lightJobCount(worklog.getId())).isZero();
        }
    }

    @Test
    @DisplayName("접수 후 시작 또는 결과 통보가 유실되면 대기 기한 이후 같은 요청을 재전달한다")
    void light_outbox_redelivers_when_start_or_result_callback_is_missing() {
        for (boolean started : List.of(false, true)) {
            Worklog worklog = saveProcessingWorklog(seedFixture());
            lightOutbox.enqueue(worklog.getId(), "REINDEX");
            UUID request = currentRequest(worklog.getId());
            lightWorker.processOne(worklog.getId());
            if (started) {
                lightIndexService.start(worklog.getId(), request, UUID.randomUUID());
            }
            assertThat(jdbcTemplate.queryForObject("""
                    SELECT next_attempt_at > CURRENT_TIMESTAMP + INTERVAL '9 minutes'
                    FROM tb_worklog_light_outbox WHERE worklog_id = ?
                    """, Boolean.class, worklog.getId())).isTrue();
            makeLightDue(worklog.getId());
            assertThat(lightWorker.processOne(worklog.getId())).isTrue();
            verify(indexClient, times(2)).requestReindex(TriggerWorklogLightIndexDto.Request.of(worklog.getId(), request));
            assertThat(lightJobCount(worklog.getId())).isEqualTo(1);
            UUID execution = UUID.randomUUID();
            lightIndexService.start(worklog.getId(), request, execution);
            lightIndexService.result(worklog.getId(), request, execution, "COMPLETED", null);
            assertThat(lightJobCount(worklog.getId())).isZero();
        }
    }

    @Test
    @DisplayName("과거 요청이나 과거 실행의 완료 통보는 최신 삭제 Outbox를 제거하지 않는다")
    void light_outbox_ignores_obsolete_request_and_execution_results() {
        Worklog worklog = saveProcessingWorklog(seedFixture());
        lightOutbox.enqueue(worklog.getId(), "REINDEX");
        UUID oldRequest = currentRequest(worklog.getId());
        UUID oldExecution = UUID.randomUUID();
        lightIndexService.start(worklog.getId(), oldRequest, oldExecution);
        lightOutbox.enqueue(worklog.getId(), "DELETE");
        UUID request = currentRequest(worklog.getId());
        lightIndexService.result(worklog.getId(), oldRequest, oldExecution, "COMPLETED", null);
        assertThat(lightJobCount(worklog.getId())).isEqualTo(1);
        UUID execution = UUID.randomUUID();
        lightIndexService.start(worklog.getId(), request, execution);
        lightIndexService.result(worklog.getId(), request, oldExecution, "COMPLETED", null);
        assertThat(lightJobCount(worklog.getId())).isEqualTo(1);
        lightIndexService.result(worklog.getId(), request, execution, "COMPLETED", null);
        assertThat(lightJobCount(worklog.getId())).isZero();
    }

    @Test
    @DisplayName("HTTP 응답보다 콜백이 먼저 와도 완료 삭제나 실패 백오프를 접수 처리로 덮지 않는다")
    void light_outbox_preserves_results_received_before_acceptance_response() {
        for (String status : List.of("COMPLETED", "FAILED")) {
            Worklog worklog = saveProcessingWorklog(seedFixture());
            lightOutbox.enqueue(worklog.getId(), "REINDEX");
            doAnswer(invocation -> {
                TriggerWorklogLightIndexDto.Request request = invocation.getArgument(0);
                UUID execution = UUID.randomUUID();
                lightIndexService.start(worklog.getId(), request.requestId(), execution);
                lightIndexService.result(worklog.getId(), request.requestId(), execution, status, "AI result");
                return null;
            }).when(indexClient).requestReindex(TriggerWorklogLightIndexDto.Request.of(worklog.getId(), currentRequest(worklog.getId())));
            lightWorker.processOne(worklog.getId());
            if ("COMPLETED".equals(status)) {
                assertThat(lightJobCount(worklog.getId())).isZero();
            } else {
                assertThat(jdbcTemplate.queryForObject("""
                        SELECT attempts = 1 AND last_error = 'AI result'
                            AND next_attempt_at < CURRENT_TIMESTAMP + INTERVAL '1 minute'
                        FROM tb_worklog_light_outbox WHERE worklog_id = ?
                        """, Boolean.class, worklog.getId())).isTrue();
            }
        }
    }

    @Test
    @DisplayName("이전 요청 전달 중 새 삭제가 등록되면 최신 revision을 지연시키지 않는다")
    void light_outbox_does_not_delay_replacement_during_dispatch() {
        Worklog worklog = saveProcessingWorklog(seedFixture());
        lightOutbox.enqueue(worklog.getId(), "REINDEX");
        doAnswer(invocation -> {
            lightOutbox.enqueue(worklog.getId(), "DELETE");
            return null;
        }).when(indexClient).requestReindex(any());
        lightWorker.processOne(worklog.getId());
        assertThat(lightWorker.processOne(worklog.getId())).isTrue();
        verify(indexClient).requestDelete(any());
        assertThat(lightJobCount(worklog.getId())).isEqualTo(1);
    }

    @Test
    @DisplayName("완료 콜백의 Outbox 삭제가 실패하면 업무일지 완료 상태도 함께 롤백된다")
    void light_outbox_completion_and_worklog_status_roll_back_together() {
        Worklog worklog = saveProcessingWorklog(seedFixture());
        lightOutbox.enqueue(worklog.getId(), "REINDEX");
        UUID request = currentRequest(worklog.getId());
        UUID execution = UUID.randomUUID();
        lightIndexService.start(worklog.getId(), request, execution);
        doThrow(new DataAccessResourceFailureException("Outbox DB failure"))
                .when(lightOutbox).completeResult(worklog.getId(), request);

        assertThatThrownBy(() -> lightIndexService.result(worklog.getId(), request, execution, "COMPLETED", null))
                .isInstanceOf(DataAccessResourceFailureException.class).hasMessage("Outbox DB failure");

        assertThat(jdbcTemplate.queryForObject("SELECT light_index_status FROM tb_worklog WHERE worklog_id = ?", String.class, worklog.getId()))
                .isEqualTo("RUNNING");
        assertThat(lightJobCount(worklog.getId())).isEqualTo(1);
    }

    /** 등록 후처리 Outbox와 수정·삭제 Outbox의 생명주기를 구분해 검증한다. */
    private int lightJobCount(Long worklogId) {
        return jdbcTemplate.queryForObject("SELECT COUNT(*) FROM tb_worklog_light_outbox WHERE worklog_id = ?", Integer.class, worklogId);
    }

    /** 실제 시간 대기 없이 접수 대기·실패 백오프 만료를 재현한다. */
    private void makeLightDue(Long worklogId) {
        jdbcTemplate.update("UPDATE tb_worklog_light_outbox SET next_attempt_at = CURRENT_TIMESTAMP - INTERVAL '1 second' WHERE worklog_id = ?", worklogId);
    }

    @Test
    void request_replacement_after_process_before_finish_ignores_old_delivery_failure() {
        Fixture fixture = seedFixture();
        Worklog worklog = saveProcessingWorklog(fixture);
        enqueueRegistration(fixture, worklog);
        Claim claim = outboxRepository.claim(worklog.getId(), UUID.randomUUID()).getFirst();
        var failures = postProcessService.process(claim);

        lightOutbox.enqueue(worklog.getId(), "REINDEX");
        postProcessService.finish(claim, failures);

        assertThat(processingStatus(worklog.getId())).isEqualTo(AiProcessingStatus.PROCESSING);
        assertThat(notificationCount(worklog.getId())).isZero();
        assertThat(jdbcTemplate.queryForObject("""
                SELECT claim_token IS NULL AND lease_until IS NULL
                FROM tb_worklog_ai_post_process_outbox WHERE worklog_id = ?
                """, Boolean.class, worklog.getId())).isTrue();
        UUID latest = currentRequest(worklog.getId());
        UUID execution = UUID.randomUUID();
        assertThat(lightIndexService.start(worklog.getId(), latest, execution)).isTrue();
        lightIndexService.result(worklog.getId(), latest, execution, "COMPLETED", null);
        assertThat(worker.processOne(worklog.getId())).isTrue();
        assertThat(processingStatus(worklog.getId())).isEqualTo(AiProcessingStatus.COMPLETED);
        assertThat(jobCount(worklog.getId())).isZero();
        assertThat(notificationCount(worklog.getId())).isEqualTo(1);
        assertThat(jdbcTemplate.queryForObject("""
                SELECT title FROM tb_notification WHERE reference_id = ?
                AND notification_type = 'WORKLOG_AI_POST_PROCESS_RESULT'
                """, String.class, worklog.getId())).contains("완료");
    }

    @Test
    void obsolete_http_failure_does_not_fail_replacement_request() {
        Fixture fixture = seedFixture();
        Worklog worklog = saveProcessingWorklog(fixture);
        enqueueRegistration(fixture, worklog);
        doAnswer(invocation -> {
            lightOutbox.enqueue(worklog.getId(), "REINDEX");
            throw new IllegalStateException("obsolete delivery failed");
        }).when(indexClient).requestIndex(any());
        assertThat(worker.processOne(worklog.getId())).isTrue();
        assertThat(lightWorker.processOne(worklog.getId())).isTrue();
        assertThat(processingStatus(worklog.getId())).isEqualTo(AiProcessingStatus.PROCESSING);
        assertThat(notificationCount(worklog.getId())).isZero();
        UUID execution = UUID.randomUUID();
        UUID latest = currentRequest(worklog.getId());
        assertThat(lightIndexService.start(worklog.getId(), latest, execution)).isTrue();
        lightIndexService.result(worklog.getId(), latest, execution, "COMPLETED", null);
        assertThat(worker.processOne(worklog.getId())).isTrue();
        assertThat(processingStatus(worklog.getId())).isEqualTo(AiProcessingStatus.COMPLETED);
        assertThat(notificationCount(worklog.getId())).isEqualTo(1);
    }

    @Test
    void concurrent_callback_and_worker_finish_converge_without_holding_claim() throws Exception {
        Fixture fixture = seedFixture();
        Worklog worklog = saveProcessingWorklog(fixture);
        org.mockito.Mockito.doNothing().when(indexClient).requestIndex(any());
        enqueueRegistration(fixture, worklog);
        worker.processOne(worklog.getId());
        lightWorker.processOne(worklog.getId());
        UUID request = currentRequest(worklog.getId());
        UUID execution = UUID.randomUUID();
        lightIndexService.start(worklog.getId(), request, execution);
        makeDue(worklog.getId());
        var start = new java.util.concurrent.CountDownLatch(1);
        try (var executor = java.util.concurrent.Executors.newFixedThreadPool(2)) {
            var callback = executor.submit(() -> {
                start.await();
                lightIndexService.result(worklog.getId(), request, execution, "COMPLETED", null);
                return true;
            });
            var finish = executor.submit(() -> {
                start.await();
                return worker.processOne(worklog.getId());
            });
            start.countDown();
            assertThat(callback.get(10, java.util.concurrent.TimeUnit.SECONDS)).isTrue();
            // callback이 행을 잠근 동안 SKIP LOCKED의 빈 선점도 정상이며 다음 poll에서 수렴한다.
            finish.get(10, java.util.concurrent.TimeUnit.SECONDS);
        }
        if (jobCount(worklog.getId()) != 0) {
            assertThat(worker.processOne(worklog.getId())).isTrue();
        }
        assertThat(processingStatus(worklog.getId())).isEqualTo(AiProcessingStatus.COMPLETED);
        assertThat(jobCount(worklog.getId())).isZero();
        assertThat(notificationCount(worklog.getId())).isEqualTo(1);
        verify(indexClient, times(1)).requestIndex(any());
    }

    @Test
    void successful_callback_before_lost_202_remains_successful() {
        Fixture fixture = seedFixture();
        Worklog worklog = saveProcessingWorklog(fixture);
        enqueueRegistration(fixture, worklog);
        doAnswer(invocation -> {
            completeIndex(invocation.getArgument(0));
            throw new IllegalStateException("202 response lost");
        }).when(indexClient).requestIndex(any());
        assertThat(lightWorker.processOne(worklog.getId())).isTrue();
        assertThat(worker.processOne(worklog.getId())).isTrue();
        assertThat(processingStatus(worklog.getId())).isEqualTo(AiProcessingStatus.COMPLETED);
        assertThat(jobCount(worklog.getId())).isZero();
        assertThat(notificationCount(worklog.getId())).isEqualTo(1);
    }

    @Test
    void source_replacement_moves_pending_generation_to_latest_request() {
        Fixture fixture = seedFixture();
        Worklog worklog = saveProcessingWorklog(fixture);
        org.mockito.Mockito.doNothing().when(indexClient).requestIndex(any());
        enqueueRegistration(fixture, worklog);
        UUID old = currentRequest(worklog.getId());
        worker.processOne(worklog.getId());
        lightWorker.processOne(worklog.getId());
        lightOutbox.enqueue(worklog.getId(), "REINDEX");
        UUID latest = currentRequest(worklog.getId());
        assertThat(latest).isNotEqualTo(old);
        assertThat(lightIndexService.start(worklog.getId(), old, UUID.randomUUID())).isFalse();
        UUID execution = UUID.randomUUID();
        assertThat(lightIndexService.start(worklog.getId(), latest, execution)).isTrue();
        lightIndexService.result(worklog.getId(), latest, execution, "COMPLETED", null);
        assertThat(worker.processOne(worklog.getId())).isTrue();
        assertThat(processingStatus(worklog.getId())).isEqualTo(AiProcessingStatus.COMPLETED);
        assertThat(jobCount(worklog.getId())).isZero();
        assertThat(notificationCount(worklog.getId())).isEqualTo(1);
        verify(indexClient, times(1)).requestIndex(any());
    }

    @Test
    void soft_deleted_source_still_allows_latest_delete_execution() {
        Fixture fixture = seedFixture();
        Worklog worklog = saveProcessingWorklog(fixture);
        enqueueRegistration(fixture, worklog);
        worklog.softDelete();
        worklogRepository.saveAndFlush(worklog);
        lightOutbox.enqueue(worklog.getId(), "DELETE");
        assertThat(lightIndexService.start(worklog.getId(), currentRequest(worklog.getId()), UUID.randomUUID())).isTrue();
    }

    /** JDBC가 소유한 최신 요청을 새 조회로 확인한다. */
    private UUID currentRequest(Long id) {
        return jdbcTemplate.queryForObject("SELECT light_index_request_id FROM tb_worklog WHERE worklog_id = ?", UUID.class, id);
    }

    @Test
    void acceptance_waits_for_current_execution_result_and_ignores_replays() {
        Fixture fixture = seedFixture();
        Worklog worklog = saveProcessingWorklog(fixture);
        org.mockito.Mockito.doNothing().when(indexClient).requestIndex(any());
        enqueueRegistration(fixture, worklog);
        UUID request = jdbcTemplate.queryForObject("SELECT light_index_request_id FROM tb_worklog WHERE worklog_id = ?", UUID.class, worklog.getId());
        assertThat(worker.processOne(worklog.getId())).isTrue();
        assertThat(lightWorker.processOne(worklog.getId())).isTrue();
        assertThat(processingStatus(worklog.getId())).isEqualTo(AiProcessingStatus.PROCESSING);
        assertThat(notificationCount(worklog.getId())).isZero();
        assertThat(hasCheckpoint(worklog.getId(), "LIGHT_INDEX")).isFalse();
        UUID a = UUID.randomUUID();
        UUID b = UUID.randomUUID();
        assertThat(lightIndexService.start(worklog.getId(), request, a)).isTrue();
        assertThat(lightIndexService.start(worklog.getId(), request, b)).isTrue();
        lightIndexService.result(worklog.getId(), request, b, "FAILED", "INDEX_FAILED");
        lightIndexService.result(worklog.getId(), request, a, "COMPLETED", null);
        assertThat(jdbcTemplate.queryForObject("SELECT light_index_status FROM tb_worklog WHERE worklog_id = ?", String.class, worklog.getId())).isEqualTo("FAILED");
        lightIndexService.result(worklog.getId(), request, b, "COMPLETED", null);
        assertThat(jdbcTemplate.queryForObject("SELECT light_index_status FROM tb_worklog WHERE worklog_id = ?", String.class, worklog.getId())).isEqualTo("FAILED");
        assertThat(worker.processOne(worklog.getId())).isTrue();
        assertThat(processingStatus(worklog.getId())).isEqualTo(AiProcessingStatus.FAILED);
        verify(indexClient, times(1)).requestIndex(any());
    }

    @Test
    void completed_request_rejects_duplicate_start_and_late_old_request() {
        Fixture fixture = seedFixture();
        Worklog worklog = saveProcessingWorklog(fixture);
        org.mockito.Mockito.doNothing().when(indexClient).requestIndex(any());
        enqueueRegistration(fixture, worklog);
        UUID request = jdbcTemplate.queryForObject("SELECT light_index_request_id FROM tb_worklog WHERE worklog_id = ?", UUID.class, worklog.getId());
        UUID execution = UUID.randomUUID();
        assertThat(lightIndexService.start(worklog.getId(), request, execution)).isTrue();
        lightIndexService.result(worklog.getId(), request, execution, "COMPLETED", null);
        assertThat(lightIndexService.start(worklog.getId(), request, UUID.randomUUID())).isFalse();
        assertThat(lightIndexService.start(worklog.getId(), UUID.randomUUID(), execution)).isFalse();
        assertThat(worker.processOne(worklog.getId())).isTrue();
        assertThat(processingStatus(worklog.getId())).isEqualTo(AiProcessingStatus.COMPLETED);
        assertThat(notificationCount(worklog.getId())).isEqualTo(1);
        lightIndexService.result(worklog.getId(), request, execution, "COMPLETED", null);
        assertThat(notificationCount(worklog.getId())).isEqualTo(1);
    }

    @Test
    @DisplayName("색인 실패 후 재시도하면 성공한 요약·태그 접수를 반복하지 않고 완료한다")
    void index_retry_skips_previously_accepted_pipeline() {
        Fixture fixture = seedFixture();
        Worklog worklog = saveProcessingWorklog(fixture);
        enqueueRegistration(fixture, worklog);
        doThrow(new IllegalStateException("index unavailable")).doAnswer(invocation -> {
            completeIndex(invocation.getArgument(0)); return null;
        }).when(indexClient).requestIndex(any());

        assertThat(worker.processOne(worklog.getId())).isTrue();
        assertThat(lightWorker.processOne(worklog.getId())).isTrue();
        assertThat(processingStatus(worklog.getId())).isEqualTo(AiProcessingStatus.PROCESSING);
        assertThat(hasCheckpoint(worklog.getId(), "WORKLOG_PIPELINE")).isTrue();
        assertThat(notificationCount(worklog.getId())).isZero();
        makeLightDue(worklog.getId());
        assertThat(lightWorker.processOne(worklog.getId())).isTrue();
        makeDue(worklog.getId());
        assertThat(worker.processOne(worklog.getId())).isTrue();
        assertThat(processingStatus(worklog.getId())).isEqualTo(AiProcessingStatus.COMPLETED);
        assertThat(jobCount(worklog.getId())).isZero();
        verify(pipelineClient, times(1)).requestPipeline(pipelineRequest(fixture, worklog));
        verify(indexClient, times(2)).requestIndex(any());
        verifyNoInteractions(fileSummaryClient);
        assertThat(notificationCount(worklog.getId())).isEqualTo(1);
    }

    @Test
    @DisplayName("색인 최종 실패 뒤 명시적 요약 재요청은 새 색인과 별도로 실행된다")
    void manual_summary_retry_survives_completion_of_existing_index_retry() {
        Fixture fixture = seedFixture();
        Worklog worklog = saveProcessingWorklog(fixture);
        enqueueRegistration(fixture, worklog);
        doAnswer(invocation -> {
            TriggerWorklogLightIndexDto.Request request = invocation.getArgument(0);
            UUID execution = UUID.randomUUID();
            assertThat(lightIndexService.start(worklog.getId(), request.requestId(), execution)).isTrue();
            lightIndexService.result(worklog.getId(), request.requestId(), execution, "FAILED", "INDEX_FAILED");
            return null;
        }).doAnswer(invocation -> {
            completeIndex(invocation.getArgument(0)); return null;
        }).when(indexClient).requestIndex(any());

        assertThat(worker.processOne(worklog.getId())).isTrue();
        assertThat(lightWorker.processOne(worklog.getId())).isTrue();
        assertThat(worker.processOne(worklog.getId())).isTrue();
        assertThat(processingStatus(worklog.getId())).isEqualTo(AiProcessingStatus.FAILED);
        assertThat(hasCheckpoint(worklog.getId(), "WORKLOG_PIPELINE")).isTrue();
        worklogService.retryAiSummary(new CustomUserPrincipal(
                fixture.user().getId(), fixture.user().getEmail(), "MEMBER"), worklog.getId());
        assertThat(worker.processOne(worklog.getId())).isTrue();
        assertThat(jobCount(worklog.getId())).isEqualTo(1);
        assertThat(worker.processOne(worklog.getId())).isTrue();
        assertThat(lightWorker.processOne(worklog.getId())).isTrue();
        assertThat(worker.processOne(worklog.getId())).isTrue();

        verify(pipelineClient, times(2)).requestPipeline(pipelineRequest(fixture, worklog));
        assertThat(processingStatus(worklog.getId())).isEqualTo(AiProcessingStatus.COMPLETED);
        assertThat(jobCount(worklog.getId())).isZero();
    }

    @Test
    @DisplayName("결과 알림 저장 뒤 오류가 나면 상태와 알림을 롤백하고 성공 단계를 유지해 완료만 재시도한다")
    void notification_failure_rolls_back_completion_without_repeating_remote_requests() {
        Fixture fixture = seedFixture();
        Worklog worklog = saveProcessingWorklog(fixture);
        enqueueRegistration(fixture, worklog);
        doAnswer(invocation -> {
            invocation.callRealMethod();
            throw new IllegalStateException("notification transaction failed");
        }).doCallRealMethod().when(notificationService).createWorklogAiPostProcessResultNotification(
                anyLong(), anyLong(), anyLong(), anyLong(), anyString(), anyBoolean(), anyList());

        assertThat(lightWorker.processOne(worklog.getId())).isTrue();
        assertThat(worker.processOne(worklog.getId())).isTrue();

        assertThat(processingStatus(worklog.getId())).isEqualTo(AiProcessingStatus.PROCESSING);
        assertThat(notificationCount(worklog.getId())).isZero();
        assertThat(jobCount(worklog.getId())).isEqualTo(1);
        assertThat(hasCheckpoint(worklog.getId(), "WORKLOG_PIPELINE")).isTrue();
        assertThat(hasCheckpoint(worklog.getId(), "LIGHT_INDEX")).isFalse();
        assertThat(jdbcTemplate.queryForObject("""
                SELECT attempts = 1 AND claim_token IS NULL AND last_error = 'IllegalStateException'
                FROM tb_worklog_ai_post_process_outbox WHERE worklog_id = ?
                """, Boolean.class, worklog.getId())).isTrue();
        makeDue(worklog.getId());

        assertThat(worker.processOne(worklog.getId())).isTrue();

        assertThat(processingStatus(worklog.getId())).isEqualTo(AiProcessingStatus.COMPLETED);
        assertThat(jobCount(worklog.getId())).isZero();
        assertThat(notificationCount(worklog.getId())).isEqualTo(1);
        verify(pipelineClient, times(1)).requestPipeline(pipelineRequest(fixture, worklog));
        verify(indexClient, times(1)).requestIndex(any());
    }

    @Test
    @DisplayName("선점 전에 삭제된 원본의 등록 작업은 외부 요청이나 결과 알림 없이 취소한다")
    void deleted_source_cancels_job_without_remote_requests() {
        Fixture fixture = seedFixture();
        Worklog worklog = saveProcessingWorklog(fixture);
        enqueueRegistration(fixture, worklog);
        enqueueRegistration(fixture, worklog);
        worklog.softDelete();
        worklogRepository.saveAndFlush(worklog);

        assertThat(worker.processOne(worklog.getId())).isTrue();

        assertThat(jobCount(worklog.getId())).isZero();
        assertThat(notificationCount(worklog.getId())).isZero();
        verifyNoInteractions(fileSummaryClient, pipelineClient, indexClient);
        assertThat(worklogRepository.findById(worklog.getId()).orElseThrow().getIsDeleted()).isTrue();
    }

    @Test
    @DisplayName("살아 있는 업무의 현재 선점자는 외부 요청 실행 기한을 갱신할 수 있다")
    void active_source_allows_current_owner_to_renew_lease() {
        Fixture fixture = seedFixture();
        Worklog worklog = saveProcessingWorklog(fixture);
        enqueueRegistration(fixture, worklog);
        List<Claim> claims = outboxRepository.claim(worklog.getId(), UUID.randomUUID());
        assertThat(claims).hasSize(1);
        Claim claim = claims.getFirst();

        assertThat(outboxRepository.renewLease(claim)).isTrue();

        assertThat(jdbcTemplate.queryForObject("""
                SELECT claim_token = ? AND lease_until > CURRENT_TIMESTAMP
                FROM tb_worklog_ai_post_process_outbox WHERE worklog_id = ?
                """, Boolean.class, claim.token(), worklog.getId())).isTrue();
        verifyNoInteractions(fileSummaryClient, pipelineClient, indexClient);
    }

    @Test
    @DisplayName("실제 등록 서비스는 원본과 snapshot을 커밋하고 워커 실행 전에는 AI를 호출하지 않는다")
    void create_commits_source_and_snapshot_before_worker_dispatch() {
        Fixture fixture = seedFixture();
        userTeamRepository.saveAndFlush(UserTeam.create(fixture.user().getId(), fixture.team().getId(),
                false, "개발", null, true, UserTeamStatus.ACTIVE));
        var request = new CreateWorklogApiDto.Request(fixture.team().getId(), "등록 Outbox 검증",
                "요청 내용", "업무 내용", WorklogStatus.PENDING, WorklogImportance.NORMAL,
                BigDecimal.ZERO, TODAY, TODAY.plusDays(1), List.of(), List.of());

        Long worklogId = worklogService.createWorklog(new CustomUserPrincipal(
                fixture.user().getId(), fixture.user().getEmail(), "MEMBER"), request, List.of()).worklogId();

        assertThat(processingStatus(worklogId)).isEqualTo(AiProcessingStatus.PROCESSING);
        assertThat(jobCount(worklogId)).isEqualTo(1);
        assertThat(jdbcTemplate.queryForObject("""
                SELECT payload ->> 'workContent' FROM tb_worklog_ai_post_process_outbox WHERE worklog_id = ?
                """, String.class, worklogId)).isEqualTo("업무 내용");
        verifyNoInteractions(fileSummaryClient, pipelineClient, indexClient);

        assertThat(lightJobCount(worklogId)).isEqualTo(1);
        assertThat(worker.processOne(worklogId)).isTrue();
        assertThat(processingStatus(worklogId)).isEqualTo(AiProcessingStatus.PROCESSING);
        assertThat(lightWorker.processOne(worklogId)).isTrue();
        assertThat(worker.processOne(worklogId)).isTrue();

        assertThat(processingStatus(worklogId)).isEqualTo(AiProcessingStatus.COMPLETED);
        assertThat(jobCount(worklogId)).isZero();
        assertThat(notificationCount(worklogId)).isEqualTo(1);
        verify(pipelineClient).requestPipeline(new TriggerWorklogPipelineDto.Request(worklogId,
                request.requestContent(), request.workContent(), fixture.user().getId(),
                fixture.team().getId(), fixture.department().getId()));
        verify(indexClient).requestIndex(any());
    }

    /** 등록 이벤트와 같이 활성 일반 snapshot에만 최초 색인 의도를 기록한다. */
    private void enqueueRegistration(Fixture fixture, Worklog worklog) {
        if (outboxRepository.enqueue(event(fixture, worklog))) {
            lightOutbox.enqueue(worklog.getId(), "INDEX");
        }
    }

    /** 202보다 먼저 도착할 수 있는 실제 callback을 DB에 반영한다. */
    private void completeIndex(TriggerWorklogLightIndexDto.Request request) {
        UUID execution = UUID.randomUUID();
        if (lightIndexService.start(request.worklogIds().getFirst(), request.requestId(), execution)) {
            lightIndexService.result(request.worklogIds().getFirst(), request.requestId(), execution, "COMPLETED", null);
        }
    }

    /** 실제 FK와 활성 업무 검증을 통과할 최소 조직을 만들되 테스트 간 이름 충돌을 피한다. */
    private Fixture seedFixture() {
        String suffix = UUID.randomUUID().toString().substring(0, 8);
        Department department = Department.create("Outbox부서-" + suffix, "등록 후처리 검증");
        department.changeStatus(DepartmentStatus.ACTIVE);
        department = departmentRepository.saveAndFlush(department);
        User user = userRepository.saveAndFlush(User.create(department.getId(), "작성자", suffix + "@ibank.com",
                "$2a$10$abcdefghijklmnopqrstuv", UserRole.MEMBER, EmploymentStatus.ACTIVE,
                "사원", null, LocalDate.of(2025, 1, 1), null, null));
        Team team = teamRepository.saveAndFlush(Team.create(department.getId(), "Outbox팀-" + suffix,
                TeamStatus.ACTIVE, "등록 후처리 검증", TODAY, null));
        return new Fixture(department, user, team);
    }

    /** 큐에 대기 중인 등록 업무와 동일한 처리중 상태를 실제 원본에 저장한다. */
    private Worklog saveProcessingWorklog(Fixture fixture) {
        Worklog worklog = Worklog.create(fixture.user().getId(), fixture.team().getId(), "등록 Outbox 검증",
                "요청 내용", "업무 내용", WorklogStatus.PENDING, WorklogImportance.NORMAL,
                BigDecimal.ZERO, TODAY, TODAY.plusDays(1));
        worklog.startAiProcessing();
        return worklogRepository.saveAndFlush(worklog);
    }

    /** 파일 업로드 인프라와 분리하여 본문 접수·색인·완료 트랜잭션 경계를 검증한다. */
    private WorklogAiPostProcessRequestedEvent event(Fixture fixture, Worklog worklog) {
        return new WorklogAiPostProcessRequestedEvent(worklog.getId(), worklog.getTitle(),
                worklog.getRequestContent(), worklog.getWorkContent(), fixture.user().getId(),
                fixture.team().getId(), fixture.department().getId(), List.of());
    }

    /** 인자까지 검증하여 저장된 snapshot이 실제 AI 접수 요청에 사용되는지 확인한다. */
    private TriggerWorklogPipelineDto.Request pipelineRequest(Fixture fixture, Worklog worklog) {
        return new TriggerWorklogPipelineDto.Request(worklog.getId(), worklog.getRequestContent(),
                worklog.getWorkContent(), fixture.user().getId(), fixture.team().getId(), fixture.department().getId());
    }

    /** 서비스 호출 뒤 새 조회로 읽어 완료 트랜잭션이 실제 커밋됐는지 확인한다. */
    private AiProcessingStatus processingStatus(Long worklogId) {
        return worklogRepository.findById(worklogId).orElseThrow().getAiProcessingStatus();
    }

    /** 다른 업무 fixture의 이력과 분리하여 대상 업무의 대기 작업만 검증한다. */
    private int jobCount(Long worklogId) {
        return jdbcTemplate.queryForObject("""
                SELECT COUNT(*) FROM tb_worklog_ai_post_process_outbox WHERE worklog_id = ?
                """, Integer.class, worklogId);
    }

    /** 업무당 최초 결과 알림 정책을 검증하면서 다른 테스트의 알림은 제외한다. */
    private int notificationCount(Long worklogId) {
        return jdbcTemplate.queryForObject("""
                SELECT COUNT(*) FROM tb_notification
                WHERE reference_id = ? AND reference_type = 'WORKLOG'
                  AND notification_type = 'WORKLOG_AI_POST_PROCESS_RESULT'
                """, Integer.class, worklogId);
    }

    /** 프로세스 메모리가 아닌 DB에 성공 단계가 남았는지 확인한다. */
    private boolean hasCheckpoint(Long worklogId, String stage) {
        return Boolean.TRUE.equals(jdbcTemplate.queryForObject("""
                SELECT ? = ANY(completed_stages) FROM tb_worklog_ai_post_process_outbox WHERE worklog_id = ?
                """, Boolean.class, stage, worklogId));
    }

    /** sleep 없이 백오프 이후 상태를 재현하여 테스트 실행 속도와 결정성을 유지한다. */
    private void makeDue(Long worklogId) {
        jdbcTemplate.update("""
                UPDATE tb_worklog_ai_post_process_outbox
                SET next_attempt_at = CURRENT_TIMESTAMP - INTERVAL '1 second' WHERE worklog_id = ?
                """, worklogId);
    }

    private record Fixture(Department department, User user, Team team) {
    }
}
