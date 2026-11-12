package com.ibank.axwms.domain.worklog.service;

import static org.assertj.core.api.Assertions.*;

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
import com.ibank.axwms.domain.notification.repository.NotificationRepository;
import com.ibank.axwms.domain.notification.repository.jooq.query.NotificationSearchQuery;
import com.ibank.axwms.domain.file.entity.File;
import com.ibank.axwms.domain.file.repository.FileRepository;
import com.ibank.axwms.domain.file.external.FileSummaryClient;
import com.ibank.axwms.domain.file.event.WorklogFileAiSummaryRequestedEvent;
import com.ibank.axwms.domain.worklog.external.WorklogPipelineClient;
import com.ibank.axwms.domain.worklog.dto.TriggerWorklogPipelineDto;
import com.ibank.axwms.domain.worklog.WorklogImportance;
import com.ibank.axwms.domain.worklog.WorklogStatus;
import com.ibank.axwms.domain.worklog.dto.AiTaskApiDto;
import com.ibank.axwms.domain.worklog.dto.ApplyWorklogAiTagsApiDto;
import com.ibank.axwms.domain.worklog.entity.Worklog;
import com.ibank.axwms.domain.worklog.event.WorklogAiPostProcessRequestedEvent;
import com.ibank.axwms.domain.worklog.repository.WorklogAiTaskRepository;
import com.ibank.axwms.domain.worklog.repository.WorklogLightOutboxRepository;
import com.ibank.axwms.domain.worklog.repository.WorklogRepository;
import com.ibank.axwms.testsupport.IntegrationTestSupport;
import com.ibank.axwms.global.security.CustomUserPrincipal;
import com.ibank.axwms.global.error.BusinessException;
import java.math.BigDecimal;
import java.time.LocalDate;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.transaction.support.TransactionTemplate;
import org.springframework.transaction.support.TransactionSynchronization;
import org.springframework.transaction.support.TransactionSynchronizationManager;
import org.springframework.mock.web.MockMultipartFile;
import org.springframework.context.ApplicationEventPublisher;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.mockito.ArgumentCaptor;
import org.springframework.test.context.TestPropertySource;
import static org.mockito.Mockito.*;

@TestPropertySource(properties = "ai.worklog-pipeline.enabled=true")
class InternalWorklogAiTaskResultIntegrationTest extends IntegrationTestSupport {
    @Autowired private DepartmentRepository departments;
    @Autowired private UserRepository users;
    @Autowired private TeamRepository teams;
    @Autowired private WorklogRepository worklogs;
    @Autowired private WorklogAiTaskRepository tasks;
    @Autowired private WorklogLightOutboxRepository lightOutbox;
    @Autowired private InternalWorklogAiTaskResultService service;
    @Autowired private JdbcTemplate jdbc;
    @Autowired private TransactionTemplate transaction;
    @Autowired private NotificationRepository notifications;
    @Autowired private WorklogAiTaskRetryService retryService;
    @Autowired private FileRepository files;
    @Autowired private WorklogAiTaskDeliveryOutcomeService deliveryOutcome;
    @Autowired private WorklogAiTaskOutboxWorker taskWorker;
    @Autowired private ApplicationEventPublisher publisher;
    @MockitoBean private WorklogPipelineClient pipelineClient;
    @MockitoBean private FileSummaryClient fileSummaryClient;

    @Test
    void summaryAndNotificationCommitOnceAndConflictingResultIsRejected() {
        var fixture = seed();
        assertThat(service.apply(fixture.worklogId(), fixture.result())).isTrue();
        assertThat(service.apply(fixture.worklogId(), fixture.result())).isTrue();
        assertThat(summary(fixture.worklogId())).isEqualTo("AI 요약");
        assertThat(jdbc.queryForObject("SELECT ai_processing_status FROM tb_worklog WHERE worklog_id = ?", String.class, fixture.worklogId())).isEqualTo("COMPLETED");
        assertThat(notificationCount(fixture.result().requestId())).isEqualTo(1);
        var result = fixture.result();
        var conflicting = new AiTaskApiDto.Result(result.requestId(), result.executionId(), result.inputHash(), "SUMMARY", fixture.worklogId(), "COMPLETED", null, "다른 요약", null, null);
        assertThat(service.apply(fixture.worklogId(), conflicting)).isFalse();
        assertThat(summary(fixture.worklogId())).isEqualTo("AI 요약");
        assertThat(notificationCount(result.requestId())).isEqualTo(1);
    }

    @Test
    void registrationEventCreatesTaskOutboxAndScheduledWorkerUsesIt() {
        var fixture = seed();
        jdbc.update("DELETE FROM tb_worklog_ai_post_process_outbox WHERE worklog_id = ?", fixture.worklogId());
        var worklog = worklogs.findById(fixture.worklogId()).orElseThrow();
        var event = new WorklogAiPostProcessRequestedEvent(fixture.worklogId(), worklog.getTitle(), worklog.getRequestContent(),
                worklog.getWorkContent(), fixture.authorId(), worklog.getTeamId(), 1L, List.of());
        transaction.executeWithoutResult(ignored -> publisher.publishEvent(event));
        assertThat(jdbc.queryForObject("SELECT count(*) FROM tb_worklog_ai_post_process_outbox WHERE worklog_id = ? AND task_kind IN ('SUMMARY', 'TAG')", Integer.class, fixture.worklogId())).isEqualTo(2);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM tb_worklog_ai_post_process_outbox WHERE worklog_id = ? AND task_kind = 'LEGACY'", Integer.class, fixture.worklogId())).isZero();
        assertThat(jdbc.queryForObject("SELECT count(*) FROM tb_worklog_light_outbox WHERE worklog_id = ? AND operation = 'INDEX'", Integer.class, fixture.worklogId())).isEqualTo(1);
        assertThat(taskWorker.processOne(fixture.worklogId())).isTrue();
        var sent = ArgumentCaptor.forClass(TriggerWorklogPipelineDto.Request.class);
        verify(pipelineClient).requestPipeline(sent.capture());
        assertThat(sent.getValue().requestId()).isNotNull();
        assertThat(sent.getValue().taskKind()).isIn("SUMMARY", "TAG");
    }

    @Test
    void addedFileEventIsPersistedAsSingleTaskBeforeCommit() {
        var fixture = seed();
        File file = files.saveAndFlush(File.create(fixture.worklogId(), fixture.authorId(), "added-key",
                new MockMultipartFile("file", "added.pdf", "application/pdf", new byte[]{1})));
        var event = new WorklogFileAiSummaryRequestedEvent(file.getId(), fixture.worklogId(), "added-key", "added.pdf", "pdf");
        transaction.executeWithoutResult(ignored -> publisher.publishEvent(event));
        assertThat(jdbc.queryForObject("SELECT count(*) FROM tb_worklog_ai_post_process_outbox WHERE worklog_id = ? AND task_kind = 'FILE_SUMMARY' AND target_id = ?",
                Integer.class, fixture.worklogId(), file.getId())).isEqualTo(1);
        verifyNoInteractions(fileSummaryClient);
    }

    @Test
    void domainStateTaskResultAndNotificationRollBackTogether() {
        var fixture = seed();
        assertThatThrownBy(() -> transaction.executeWithoutResult(status -> {
            assertThat(service.apply(fixture.worklogId(), fixture.result())).isTrue();
            throw new IllegalStateException("force rollback");
        })).isInstanceOf(IllegalStateException.class);
        assertThat(summary(fixture.worklogId())).isNull();
        assertThat(notificationCount(fixture.result().requestId())).isZero();
        assertThat(jdbc.queryForObject("SELECT execution_state FROM tb_worklog_ai_post_process_outbox WHERE request_id = ?",
                String.class, fixture.result().requestId())).isEqualTo("RUNNING");
    }

    @Test
    void changedInputRejectsOlderResultWithoutNotificationOrContentChange() {
        var fixture = seed();
        jdbc.update("UPDATE tb_worklog SET work_content = '수정된 내용' WHERE worklog_id = ?", fixture.worklogId());
        assertThat(service.apply(fixture.worklogId(), fixture.result())).isFalse();
        assertThat(summary(fixture.worklogId())).isNull();
        assertThat(notificationCount(fixture.result().requestId())).isZero();
        assertThat(jdbc.queryForObject("SELECT execution_state FROM tb_worklog_ai_post_process_outbox WHERE request_id = ?",
                String.class, fixture.result().requestId())).isEqualTo("RUNNING");
    }

    @Test
    void notificationListExposesRetryOnlyForCurrentFailedTask() {
        var fixture = seed();
        var original = fixture.result();
        var failed = new AiTaskApiDto.Result(original.requestId(), original.executionId(), original.inputHash(), "SUMMARY", fixture.worklogId(), "FAILED", "TimeoutError", null, null, null);
        assertThat(service.apply(fixture.worklogId(), failed)).isTrue();
        var query = new NotificationSearchQuery(null, null, null, 1, 20);
        var notification = notifications.searchNotifications(fixture.authorId(), query).getContent().stream()
                .filter(item -> failed.requestId().equals(item.retryRequestId())).findFirst().orElseThrow();
        assertThat(notification.retryable()).isTrue();
        assertThat(notification.targetDeleted()).isFalse();
        jdbc.update("UPDATE tb_worklog SET work_content = '수정됨' WHERE worklog_id = ?", fixture.worklogId());
        var changed = notifications.searchNotifications(fixture.authorId(), query).getContent().stream()
                .filter(item -> failed.requestId().equals(item.retryRequestId())).findFirst().orElseThrow();
        assertThat(changed.retryable()).isFalse();
        jdbc.update("UPDATE tb_worklog_ai_post_process_outbox SET request_id = ? WHERE request_id = ?",
                UUID.randomUUID(), failed.requestId());
        var stale = notifications.searchNotifications(fixture.authorId(), query).getContent().stream()
                .filter(item -> failed.requestId().equals(item.retryRequestId())).findFirst().orElseThrow();
        assertThat(stale.retryable()).isFalse();
    }

    @Test
    void tagResultCreatesAssociationAndNotificationOnlyOnce() {
        var fixture = seed();
        var result = tagResult(fixture, List.of(),
                List.of(new ApplyWorklogAiTagsApiDto.NewTag("태그" + UUID.randomUUID().toString().substring(0, 8), "설명")));
        assertThat(service.apply(fixture.worklogId(), result)).isTrue();
        var indexRequest = jdbc.queryForObject("SELECT request_id FROM tb_worklog_light_outbox WHERE worklog_id = ? AND operation = 'REINDEX'",
                UUID.class, fixture.worklogId());
        assertThat(indexRequest).isNotNull();
        assertThat(service.apply(fixture.worklogId(), result)).isTrue();
        assertThat(jdbc.queryForObject("SELECT request_id FROM tb_worklog_light_outbox WHERE worklog_id = ?",
                UUID.class, fixture.worklogId())).isEqualTo(indexRequest);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM tb_worklog_tag WHERE worklog_id = ?", Integer.class, fixture.worklogId())).isEqualTo(1);
        assertThat(notificationCount(result.requestId())).isEqualTo(1);
    }

    @Test
    void alreadyLinkedTagDoesNotCreateAnotherIndexRequest() {
        var fixture = seed();
        lightOutbox.enqueue(fixture.worklogId(), "INDEX");
        UUID originalRequest = jdbc.queryForObject("SELECT request_id FROM tb_worklog_light_outbox WHERE worklog_id = ?",
                UUID.class, fixture.worklogId());
        Long tagId = jdbc.queryForObject("INSERT INTO tb_meta_tag (tag_name) VALUES (?) RETURNING tag_id", Long.class,
                "tag-" + UUID.randomUUID());
        jdbc.update("INSERT INTO tb_worklog_tag (worklog_id, tag_id) VALUES (?, ?)", fixture.worklogId(), tagId);
        var result = tagResult(fixture, List.of(tagId), List.of());

        assertThat(service.apply(fixture.worklogId(), result)).isTrue();
        assertThat(jdbc.queryForObject("SELECT request_id FROM tb_worklog_light_outbox WHERE worklog_id = ?",
                UUID.class, fixture.worklogId())).isEqualTo(originalRequest);
        assertThat(jdbc.queryForObject("SELECT usage_count FROM tb_meta_tag WHERE tag_id = ?", Integer.class, tagId)).isZero();
    }

    @Test
    void newTagReplacesPendingInitialIndexWithNewRequest() {
        var fixture = seed();
        lightOutbox.enqueue(fixture.worklogId(), "INDEX");
        UUID originalRequest = jdbc.queryForObject("SELECT request_id FROM tb_worklog_light_outbox WHERE worklog_id = ?",
                UUID.class, fixture.worklogId());
        var result = tagResult(fixture, List.of(),
                List.of(new ApplyWorklogAiTagsApiDto.NewTag("tag-" + UUID.randomUUID(), "설명")));

        assertThat(service.apply(fixture.worklogId(), result)).isTrue();
        var replacement = jdbc.queryForMap("SELECT request_id, revision, operation FROM tb_worklog_light_outbox WHERE worklog_id = ?",
                fixture.worklogId());
        assertThat(replacement.get("request_id")).isNotEqualTo(originalRequest);
        assertThat(replacement.get("revision")).isEqualTo(2L);
        assertThat(replacement.get("operation")).isEqualTo("REINDEX");
        assertThat(jdbc.queryForObject("SELECT light_index_request_id FROM tb_worklog WHERE worklog_id = ?",
                UUID.class, fixture.worklogId())).isEqualTo(replacement.get("request_id"));
    }

    @Test
    void tagLinkResultAndIndexIntentRollBackTogether() {
        var fixture = seed();
        var result = tagResult(fixture, List.of(),
                List.of(new ApplyWorklogAiTagsApiDto.NewTag("tag-" + UUID.randomUUID(), "설명")));

        assertThatThrownBy(() -> transaction.executeWithoutResult(status -> {
            assertThat(service.apply(fixture.worklogId(), result)).isTrue();
            TransactionSynchronizationManager.registerSynchronization(new TransactionSynchronization() {
                @Override
                public void beforeCommit(boolean readOnly) {
                    assertThat(jdbc.queryForObject("SELECT count(*) FROM tb_worklog_light_outbox WHERE worklog_id = ?",
                            Integer.class, fixture.worklogId())).isEqualTo(1);
                    throw new IllegalStateException("force rollback after outbox insertion");
                }
            });
        })).isInstanceOf(IllegalStateException.class);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM tb_worklog_tag WHERE worklog_id = ?", Integer.class,
                fixture.worklogId())).isZero();
        assertThat(jdbc.queryForObject("SELECT count(*) FROM tb_worklog_light_outbox WHERE worklog_id = ?", Integer.class,
                fixture.worklogId())).isZero();
        assertThat(notificationCount(result.requestId())).isZero();
        assertThat(jdbc.queryForObject("SELECT execution_state FROM tb_worklog_ai_post_process_outbox WHERE request_id = ?",
                String.class, result.requestId())).isEqualTo("RUNNING");
    }

    @Test
    void staleTagResultCannotCreateIndexIntent() {
        var fixture = seed();
        var result = tagResult(fixture, List.of(),
                List.of(new ApplyWorklogAiTagsApiDto.NewTag("tag-" + UUID.randomUUID(), "설명")));
        jdbc.update("UPDATE tb_worklog SET work_content = '변경된 내용' WHERE worklog_id = ?", fixture.worklogId());

        assertThat(service.apply(fixture.worklogId(), result)).isFalse();
        assertThat(jdbc.queryForObject("SELECT count(*) FROM tb_worklog_light_outbox WHERE worklog_id = ?",
                Integer.class, fixture.worklogId())).isZero();
        assertThat(notificationCount(result.requestId())).isZero();
    }

    @Test
    void deletedWorklogCannotReceiveTagIndexIntent() {
        var fixture = seed();
        var result = tagResult(fixture, List.of(),
                List.of(new ApplyWorklogAiTagsApiDto.NewTag("tag-" + UUID.randomUUID(), "설명")));
        jdbc.update("UPDATE tb_worklog SET is_deleted = true WHERE worklog_id = ?", fixture.worklogId());

        assertThat(service.apply(fixture.worklogId(), result)).isFalse();
        assertThat(jdbc.queryForObject("SELECT count(*) FROM tb_worklog_light_outbox WHERE worklog_id = ?",
                Integer.class, fixture.worklogId())).isZero();
        assertThat(jdbc.queryForObject("SELECT count(*) FROM tb_worklog_tag WHERE worklog_id = ?",
                Integer.class, fixture.worklogId())).isZero();
        assertThat(notificationCount(result.requestId())).isZero();
    }

    @Test
    void failedTagResultDoesNotCreateIndexIntent() {
        var fixture = seed();
        var success = tagResult(fixture, List.of(),
                List.of(new ApplyWorklogAiTagsApiDto.NewTag("tag-" + UUID.randomUUID(), "설명")));
        var failed = new AiTaskApiDto.Result(success.requestId(), success.executionId(), success.inputHash(), "TAG", fixture.worklogId(), "FAILED", "ERROR", null, null, null);

        assertThat(service.apply(fixture.worklogId(), failed)).isTrue();
        assertThat(jdbc.queryForObject("SELECT count(*) FROM tb_worklog_light_outbox WHERE worklog_id = ?",
                Integer.class, fixture.worklogId())).isZero();
        assertThat(jdbc.queryForObject("SELECT count(*) FROM tb_worklog_tag WHERE worklog_id = ?",
                Integer.class, fixture.worklogId())).isZero();
    }

    @Test
    void fileSummaryResultIsAppliedOnlyToTheCurrentActiveFile() {
        var fixture = seed();
        File file = files.saveAndFlush(File.create(fixture.worklogId(), fixture.authorId(), "key",
                new MockMultipartFile("file", "x.pdf", "application/pdf", new byte[]{1, 2})));
        var event = new WorklogAiPostProcessRequestedEvent(fixture.worklogId(), "업무 제목", "요청", "내용",
                fixture.authorId(), 1L, 1L, List.of(new WorklogAiPostProcessRequestedEvent.FileSummaryTarget(
                file.getId(), "key", "x.pdf", "pdf")));
        tasks.enqueueInitialTasks(event);
        jdbc.update("UPDATE tb_worklog_ai_post_process_outbox SET next_attempt_at = CURRENT_TIMESTAMP + INTERVAL '1 hour' WHERE worklog_id = ? AND task_kind = 'TAG'", fixture.worklogId());
        var claim = tasks.claimDelivery(fixture.worklogId(), UUID.randomUUID(), true, true).orElseThrow();
        assertThat(claim.taskKind()).isEqualTo("FILE_SUMMARY");
        UUID execution = UUID.randomUUID();
        var start = new AiTaskApiDto.Identity(claim.requestId(), execution, claim.inputHash(), "FILE_SUMMARY", file.getId());
        UUID token = tasks.start(fixture.worklogId(), start).orElseThrow();
        var result = new AiTaskApiDto.Result(claim.requestId(), execution, claim.inputHash(), "FILE_SUMMARY", file.getId(), "COMPLETED", null, "파일 요약", null, null);
        assertThat(service.apply(fixture.worklogId(), result)).isTrue();
        assertThat(service.apply(fixture.worklogId(), result)).isTrue();
        assertThat(jdbc.queryForObject("SELECT ai_summary FROM tb_file WHERE file_id = ?", String.class, file.getId())).isEqualTo("파일 요약");
        assertThat(notificationCount(result.requestId())).isEqualTo(1);
    }

    @Test
    void failedFileCanBeRetriedIndependentlyByAuthor() {
        var fixture = seed();
        File file = files.saveAndFlush(File.create(fixture.worklogId(), fixture.authorId(), "key",
                new MockMultipartFile("file", "x.pdf", "application/pdf", new byte[]{1, 2})));
        tasks.enqueueInitialTasks(new WorklogAiPostProcessRequestedEvent(fixture.worklogId(), "업무 제목", "요청", "내용",
                fixture.authorId(), 1L, 1L, List.of(new WorklogAiPostProcessRequestedEvent.FileSummaryTarget(file.getId(), "key", "x.pdf", "pdf"))));
        jdbc.update("UPDATE tb_worklog_ai_post_process_outbox SET next_attempt_at = CURRENT_TIMESTAMP + INTERVAL '1 hour' WHERE worklog_id = ? AND task_kind = 'TAG'", fixture.worklogId());
        var claim = tasks.claimDelivery(fixture.worklogId(), UUID.randomUUID(), true, true).orElseThrow();
        assertThat(claim.taskKind()).isEqualTo("FILE_SUMMARY");
        UUID execution = UUID.randomUUID();
        var start = new AiTaskApiDto.Identity(claim.requestId(), execution, claim.inputHash(), "FILE_SUMMARY", file.getId());
        UUID token = tasks.start(fixture.worklogId(), start).orElseThrow();
        var result = new AiTaskApiDto.Result(claim.requestId(), execution, claim.inputHash(), "FILE_SUMMARY", file.getId(), "FAILED", "TimeoutError", null, null, null);
        assertThat(service.apply(fixture.worklogId(), result)).isTrue();
        var author = new CustomUserPrincipal(fixture.authorId(), "author@test.invalid", "MEMBER");
        var query = new NotificationSearchQuery(null, null, null, 1, 20);
        assertThat(notifications.searchNotifications(fixture.authorId(), query).getContent().stream()
                .filter(item -> result.requestId().equals(item.retryRequestId())).findFirst().orElseThrow().retryable()).isTrue();
        UUID retry = retryService.retry(author, fixture.worklogId(), result.requestId());
        assertThat(retry).isNotEqualTo(result.requestId());
        assertThat(jdbc.queryForObject("SELECT ai_processing_status FROM tb_file WHERE file_id = ?", String.class, file.getId())).isEqualTo("PROCESSING");
        assertThat(jdbc.queryForObject("SELECT count(*) FROM tb_worklog_ai_post_process_outbox WHERE worklog_id = ? AND task_kind = 'FILE_SUMMARY'", Integer.class, fixture.worklogId())).isEqualTo(1);
    }

    @Test
    void deletedFileCannotReceiveOldAiResult() {
        var fixture = seed();
        File file = files.saveAndFlush(File.create(fixture.worklogId(), fixture.authorId(), "key",
                new MockMultipartFile("file", "x.pdf", "application/pdf", new byte[]{1, 2})));
        tasks.enqueueInitialTasks(new WorklogAiPostProcessRequestedEvent(fixture.worklogId(), "업무 제목", "요청", "내용",
                fixture.authorId(), 1L, 1L, List.of(new WorklogAiPostProcessRequestedEvent.FileSummaryTarget(file.getId(), "key", "x.pdf", "pdf"))));
        jdbc.update("UPDATE tb_worklog_ai_post_process_outbox SET next_attempt_at = CURRENT_TIMESTAMP + INTERVAL '1 hour' WHERE worklog_id = ? AND task_kind = 'TAG'", fixture.worklogId());
        var claim = tasks.claimDelivery(fixture.worklogId(), UUID.randomUUID(), true, true).orElseThrow();
        UUID execution = UUID.randomUUID();
        var start = new AiTaskApiDto.Identity(claim.requestId(), execution, claim.inputHash(), "FILE_SUMMARY", file.getId());
        UUID token = tasks.start(fixture.worklogId(), start).orElseThrow();
        var result = new AiTaskApiDto.Result(claim.requestId(), execution, claim.inputHash(), "FILE_SUMMARY", file.getId(), "COMPLETED", null, "파일 요약", null, null);
        jdbc.update("UPDATE tb_file SET is_deleted = true WHERE file_id = ?", file.getId());
        assertThat(service.apply(fixture.worklogId(), result)).isFalse();
        assertThat(jdbc.queryForObject("SELECT ai_summary FROM tb_file WHERE file_id = ?", String.class, file.getId())).isNull();
        assertThat(notificationCount(result.requestId())).isZero();
    }

    @Test
    void exhaustedDeliveryCreatesOneUnconfirmedNotificationAndCanBeRetried() {
        var fixture = seed();
        jdbc.update("UPDATE tb_worklog_ai_post_process_outbox SET attempts = 3 WHERE worklog_id = ? AND task_kind = 'TAG'", fixture.worklogId());
        var claim = tasks.claimDelivery(fixture.worklogId(), UUID.randomUUID(), true, true).orElseThrow();
        assertThat(claim.taskKind()).isEqualTo("TAG");
        assertThat(deliveryOutcome.fail(claim, "TimeoutError")).isTrue();
        assertThat(deliveryOutcome.fail(claim, "TimeoutError")).isFalse();
        assertThat(notificationCount(claim.requestId())).isEqualTo(1);
        assertThat(jdbc.queryForObject("SELECT ai_result FROM tb_notification WHERE ai_request_id = ?", String.class, claim.requestId()))
                .isEqualTo("FAILED");
        var query = new NotificationSearchQuery(null, null, null, 1, 20);
        assertThat(notifications.searchNotifications(fixture.authorId(), query).getContent().stream()
                .filter(item -> claim.requestId().equals(item.retryRequestId())).findFirst().orElseThrow().retryable()).isTrue();
        var author = new CustomUserPrincipal(fixture.authorId(), "author@test.invalid", "MEMBER");
        assertThat(retryService.retry(author, fixture.worklogId(), claim.requestId())).isNotEqualTo(claim.requestId());
    }

    @Test
    void exhaustedSummaryDeliveryMarksSummaryFailed() {
        var fixture = seed();
        jdbc.update("UPDATE tb_worklog_ai_post_process_outbox SET execution_state = 'RETRY_READY', delivery_state = 'PENDING', attempts = 3, claim_token = NULL, lease_until = NULL WHERE request_id = ?", fixture.result().requestId());
        var claim = tasks.claimDelivery(fixture.worklogId(), UUID.randomUUID(), true, true).orElseThrow();
        assertThat(claim.taskKind()).isEqualTo("SUMMARY");
        assertThat(deliveryOutcome.fail(claim, "TimeoutError")).isTrue();
        assertThat(jdbc.queryForObject("SELECT ai_processing_status FROM tb_worklog WHERE worklog_id = ?", String.class, fixture.worklogId())).isEqualTo("FAILED");
    }

    @Test
    void crashedFinalDeliveryIsRecoveredWithNotificationExactlyOnce() {
        var fixture = seed();
        jdbc.update("UPDATE tb_worklog_ai_post_process_outbox SET attempts = 3 WHERE worklog_id = ? AND task_kind = 'TAG'", fixture.worklogId());
        var claim = tasks.claimDelivery(fixture.worklogId(), UUID.randomUUID(), true, true).orElseThrow();
        jdbc.update("UPDATE tb_worklog_ai_post_process_outbox SET lease_until = CURRENT_TIMESTAMP - INTERVAL '1 second' WHERE request_id = ?", claim.requestId());
        assertThat(tasks.recoveryCandidates(20)).extracting(item -> item.requestId()).contains(claim.requestId());
        assertThat(deliveryOutcome.fail(claim, "DELIVERY_UNCONFIRMED")).isTrue();
        assertThat(deliveryOutcome.fail(claim, "DELIVERY_UNCONFIRMED")).isFalse();
        assertThat(notificationCount(claim.requestId())).isEqualTo(1);
    }

    @Test
    void manualRetryIsAuthorOnlyAndRepeatedClickReturnsSameNewRequest() {
        var fixture = seed();
        var original = fixture.result();
        var failed = new AiTaskApiDto.Result(original.requestId(), original.executionId(), original.inputHash(), "SUMMARY", fixture.worklogId(), "FAILED", "TimeoutError", null, null, null);
        assertThat(service.apply(fixture.worklogId(), failed)).isTrue();
        var other = new CustomUserPrincipal(fixture.authorId() + 99999, "other@test.invalid", "MEMBER");
        assertThatThrownBy(() -> retryService.retry(other, fixture.worklogId(), failed.requestId()))
                .isInstanceOf(BusinessException.class);
        var author = new CustomUserPrincipal(fixture.authorId(), "author@test.invalid", "MEMBER");
        UUID retry = retryService.retry(author, fixture.worklogId(), failed.requestId());
        assertThat(retry).isNotEqualTo(failed.requestId());
        assertThat(retryService.retry(author, fixture.worklogId(), failed.requestId())).isEqualTo(retry);
        assertThat(jdbc.queryForObject("SELECT execution_state FROM tb_worklog_ai_post_process_outbox WHERE request_id = ?",
                String.class, retry)).isEqualTo("UNKNOWN");
        assertThat(jdbc.queryForObject("SELECT attempts FROM tb_worklog_ai_post_process_outbox WHERE request_id = ?",
                Integer.class, retry)).isZero();
        assertThat(jdbc.queryForObject("SELECT ai_processing_status FROM tb_worklog WHERE worklog_id = ?", String.class, fixture.worklogId())).isEqualTo("PROCESSING");
    }

    @Test
    void detailSummaryRetryUsesCurrentFailedTask() {
        var fixture = seed();
        var original = fixture.result();
        var failed = new AiTaskApiDto.Result(original.requestId(), original.executionId(), original.inputHash(), "SUMMARY", fixture.worklogId(), "FAILED", "TimeoutError", null, null, null);
        assertThat(service.apply(fixture.worklogId(), failed)).isTrue();
        var author = new CustomUserPrincipal(fixture.authorId(), "author@test.invalid", "MEMBER");
        UUID retry = retryService.retryCurrentSummary(author, fixture.worklogId());
        assertThat(retry).isNotEqualTo(failed.requestId());
        assertThat(jdbc.queryForObject("SELECT execution_state FROM tb_worklog_ai_post_process_outbox WHERE request_id = ?",
                String.class, retry)).isEqualTo("UNKNOWN");
    }

    @Test
    void concurrentRetryClicksConvergeOnOneNewRequest() throws Exception {
        var fixture = seed();
        var original = fixture.result();
        var failed = new AiTaskApiDto.Result(original.requestId(), original.executionId(), original.inputHash(), "SUMMARY", fixture.worklogId(), "FAILED", "TimeoutError", null, null, null);
        assertThat(service.apply(fixture.worklogId(), failed)).isTrue();
        var author = new CustomUserPrincipal(fixture.authorId(), "author@test.invalid", "MEMBER");
        var gate = new CountDownLatch(1);
        try (var pool = Executors.newFixedThreadPool(2)) {
            var first = pool.submit(() -> { gate.await(); return retryService.retry(author, fixture.worklogId(), failed.requestId()); });
            var second = pool.submit(() -> { gate.await(); return retryService.retry(author, fixture.worklogId(), failed.requestId()); });
            gate.countDown();
            assertThat(first.get(10, TimeUnit.SECONDS)).isEqualTo(second.get(10, TimeUnit.SECONDS));
        }
    }

    @Test
    void retryRejectsChangedInputOrSuccessfulAndUnrelatedRequests() {
        var first = seed();
        var author = new CustomUserPrincipal(first.authorId(), "author@test.invalid", "MEMBER");
        assertThat(service.apply(first.worklogId(), first.result())).isTrue();
        assertThatThrownBy(() -> retryService.retry(author, first.worklogId(), first.result().requestId()))
                .isInstanceOf(BusinessException.class);
        assertThatThrownBy(() -> retryService.retry(author, first.worklogId(), UUID.randomUUID()))
                .isInstanceOf(BusinessException.class);

        var second = seed();
        var original = second.result();
        var failed = new AiTaskApiDto.Result(original.requestId(), original.executionId(), original.inputHash(), "SUMMARY", second.worklogId(), "FAILED", "TimeoutError", null, null, null);
        assertThat(service.apply(second.worklogId(), failed)).isTrue();
        jdbc.update("UPDATE tb_worklog SET work_content = '수정됨' WHERE worklog_id = ?", second.worklogId());
        var secondAuthor = new CustomUserPrincipal(second.authorId(), "author@test.invalid", "MEMBER");
        assertThatThrownBy(() -> retryService.retry(secondAuthor, second.worklogId(), failed.requestId()))
                .isInstanceOf(BusinessException.class);
    }

    /** 현재 TAG 결과의 실행 권위를 준비해 최종 콜백 경로를 검증한다. */
    private AiTaskApiDto.Result tagResult(Fixture fixture, List<Long> existingTagIds,
                                           List<ApplyWorklogAiTagsApiDto.NewTag> newTags) {
        var claim = tasks.claimDelivery(fixture.worklogId(), UUID.randomUUID(), true, true).orElseThrow();
        assertThat(claim.taskKind()).isEqualTo("TAG");
        UUID execution = UUID.randomUUID();
        var start = new AiTaskApiDto.Identity(claim.requestId(), execution, claim.inputHash(), "TAG", fixture.worklogId());
        UUID token = tasks.start(fixture.worklogId(), start).orElseThrow();
        return new AiTaskApiDto.Result(claim.requestId(), execution, claim.inputHash(), "TAG", fixture.worklogId(), "COMPLETED", null, null, existingTagIds, newTags);
    }

    /** 테스트 전용 DB에 실제 FK와 같은 스냅샷을 가진 실행을 준비한다. */
    private Fixture seed() {
        String suffix = UUID.randomUUID().toString().substring(0, 8);
        LocalDate today = LocalDate.of(2026, 11, 1);
        Department department = Department.create("결과부서-" + suffix, "test");
        department.changeStatus(DepartmentStatus.ACTIVE);
        department = departments.saveAndFlush(department);
        User user = users.saveAndFlush(User.create(department.getId(), "작성자", suffix + "@test.invalid",
                "$2a$10$abcdefghijklmnopqrstuv", UserRole.MEMBER, EmploymentStatus.ACTIVE, "사원", null, today, null, null));
        Team team = teams.saveAndFlush(Team.create(department.getId(), "결과팀-" + suffix, TeamStatus.ACTIVE, "test", today, null));
        Worklog worklog = worklogs.saveAndFlush(Worklog.create(user.getId(), team.getId(), "업무 제목", "요청", "내용",
                WorklogStatus.PENDING, WorklogImportance.NORMAL, BigDecimal.ZERO, today, today.plusDays(1)));
        tasks.enqueueInitialTasks(new WorklogAiPostProcessRequestedEvent(worklog.getId(), worklog.getTitle(), "요청", "내용",
                user.getId(), team.getId(), department.getId(), List.of()));
        var claim = tasks.claimDelivery(worklog.getId(), UUID.randomUUID(), true, true).orElseThrow();
        assertThat(claim.taskKind()).isEqualTo("SUMMARY");
        UUID execution = UUID.randomUUID();
        var identity = new AiTaskApiDto.Identity(claim.requestId(), execution, claim.inputHash(), claim.taskKind(), claim.targetId());
        UUID token = tasks.start(worklog.getId(), identity).orElseThrow();
        var result = new AiTaskApiDto.Result(claim.requestId(), execution, claim.inputHash(), claim.taskKind(), claim.targetId(), "COMPLETED", null, "AI 요약", null, null);
        return new Fixture(worklog.getId(), user.getId(), result);
    }

    /** 영속성 컨텍스트가 아닌 커밋된 DB 값으로 결과를 확인한다. */
    private String summary(Long id) {
        return jdbc.queryForObject("SELECT ai_summary FROM tb_worklog WHERE worklog_id = ?", String.class, id);
    }

    /** 요청별 유일 알림이 실제로 저장됐는지 확인한다. */
    private int notificationCount(UUID requestId) {
        return jdbc.queryForObject("SELECT count(*) FROM tb_notification WHERE ai_request_id = ?", Integer.class, requestId);
    }

    private record Fixture(Long worklogId, Long authorId, AiTaskApiDto.Result result) {}
}
