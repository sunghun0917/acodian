package com.ibank.axwms.domain.worklog.service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.ibank.axwms.domain.file.entity.File;
import com.ibank.axwms.domain.file.external.FileSummaryClient;
import com.ibank.axwms.domain.file.repository.FileRepository;
import com.ibank.axwms.domain.notification.repository.NotificationRepository;
import com.ibank.axwms.domain.notification.repository.jooq.query.NotificationSearchQuery;
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
import com.ibank.axwms.domain.worklog.dto.AiTaskApiDto;
import com.ibank.axwms.domain.worklog.dto.UpdateWorklogApiDto;
import com.ibank.axwms.domain.worklog.entity.Worklog;
import com.ibank.axwms.domain.worklog.event.WorklogAiPostProcessRequestedEvent;
import com.ibank.axwms.domain.worklog.external.WorklogLightIndexClient;
import com.ibank.axwms.domain.worklog.external.WorklogPipelineClient;
import com.ibank.axwms.domain.worklog.repository.WorklogAiTaskRepository;
import com.ibank.axwms.domain.worklog.repository.WorklogRepository;
import com.ibank.axwms.global.error.BusinessException;
import com.ibank.axwms.global.error.ErrorCode;
import com.ibank.axwms.global.security.CustomUserPrincipal;
import com.ibank.axwms.testsupport.IntegrationTestSupport;
import java.math.BigDecimal;
import java.time.LocalDate;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.mock.web.MockMultipartFile;
import org.springframework.test.context.TestPropertySource;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.transaction.support.TransactionTemplate;

/** 업무 수정·삭제가 진행 중 일반 AI 작업을 실제 PostgreSQL 에서 최신 입력으로 교체하는지 검증한다(R15). */
@TestPropertySource(properties = {
        "ai.worklog-pipeline.enabled=true",
        "ai.file-summary.enabled=true",
        "ai.worklog-light-index.internal-token=test-internal-token"
})
class WorklogAiTaskSupersedeIntegrationTest extends IntegrationTestSupport {
    @Autowired private DepartmentRepository departments;
    @Autowired private UserRepository users;
    @Autowired private TeamRepository teams;
    @Autowired private WorklogRepository worklogs;
    @Autowired private FileRepository files;
    @Autowired private WorklogAiTaskRepository tasks;
    @Autowired private WorklogService worklogService;
    @Autowired private InternalWorklogAiTaskResultService results;
    @Autowired private WorklogAiTaskRetryService retries;
    @Autowired private NotificationRepository notifications;
    @Autowired private JdbcTemplate jdbc;
    @Autowired private TransactionTemplate transaction;
    // 수정·삭제가 커밋 뒤 외부 AI 를 직접 호출하지 않는지만 확인하므로 실제 HTTP 는 막는다.
    @MockitoBean private WorklogPipelineClient pipelineClient;
    @MockitoBean private FileSummaryClient fileSummaryClient;
    @MockitoBean private WorklogLightIndexClient lightIndexClient;

    @Test
    @DisplayName("요약·태그 입력 본문이 바뀌면 진행 중 옛 요청을 최신 입력의 새 요청으로 교체하고 옛 실행 권위를 모두 거절한다")
    void bodyChangeSupersedesInFlightSummaryAndTag() {
        var fixture = seedRunningSummary();
        var oldSummary = row(fixture.worklogId(), "SUMMARY", fixture.worklogId());
        var oldTag = row(fixture.worklogId(), "TAG", fixture.worklogId());

        worklogService.updateWorklog(fixture.author(), fixture.worklogId(), update(null, null, "새 내용", null), List.of());

        var summary = row(fixture.worklogId(), "SUMMARY", fixture.worklogId());
        assertReplacedWithFreshBudget(summary, oldSummary);
        assertThat(summary.get("work_content")).isEqualTo("새 내용");
        assertThat(tasks.isCurrentWorklogInput("SUMMARY", (String) summary.get("input_hash"), "요청", "새 내용")).isTrue();
        var tag = row(fixture.worklogId(), "TAG", fixture.worklogId());
        assertReplacedWithFreshBudget(tag, oldTag);
        assertThat(tasks.isCurrentWorklogInput("TAG", (String) tag.get("input_hash"), "요청", "새 내용")).isTrue();

        var old = fixture.result();
        assertThat(tasks.start(fixture.worklogId(), identity(old, UUID.randomUUID(), null))).isEmpty();
        assertThat(results.apply(fixture.worklogId(), old)).isFalse();
        assertThat(summary(fixture.worklogId())).isNull();
        assertThat(notificationCount(old.requestId())).isZero();
        assertThat(aiProcessingStatus(fixture.worklogId())).isEqualTo("PROCESSING");
    }

    @Test
    @DisplayName("요청 내용만 바뀌면 요약만 교체하고 업무 내용만 입력으로 쓰는 태그는 유지한다")
    void requestContentChangeSupersedesOnlySummary() {
        var fixture = seedRunningSummary();
        var oldSummary = row(fixture.worklogId(), "SUMMARY", fixture.worklogId());
        var oldTag = row(fixture.worklogId(), "TAG", fixture.worklogId());

        worklogService.updateWorklog(fixture.author(), fixture.worklogId(), update(null, "새 요청", null, null), List.of());

        assertReplacedWithFreshBudget(row(fixture.worklogId(), "SUMMARY", fixture.worklogId()), oldSummary);
        assertThat(row(fixture.worklogId(), "TAG", fixture.worklogId())).isEqualTo(oldTag);
    }

    @Test
    @DisplayName("요약·태그 입력과 무관한 제목·중요도 수정은 진행 중 작업을 교체하지 않는다")
    void unrelatedChangeKeepsInFlightTasks() {
        var fixture = seedRunningSummary();
        var oldSummary = row(fixture.worklogId(), "SUMMARY", fixture.worklogId());
        var oldTag = row(fixture.worklogId(), "TAG", fixture.worklogId());
        var request = new UpdateWorklogApiDto.Request("바뀐 제목", null, null, null, null, WorklogImportance.HIGH,
                null, null, null, null, null, null, null, null);

        worklogService.updateWorklog(fixture.author(), fixture.worklogId(), request, List.of());

        assertThat(row(fixture.worklogId(), "SUMMARY", fixture.worklogId())).isEqualTo(oldSummary);
        assertThat(row(fixture.worklogId(), "TAG", fixture.worklogId())).isEqualTo(oldTag);
        assertThat(results.apply(fixture.worklogId(), fixture.result())).isTrue();
        assertThat(summary(fixture.worklogId())).isEqualTo("AI 요약");
    }

    @Test
    @DisplayName("업무 수정이 롤백되면 본문과 작업 교체가 함께 롤백된다")
    void rollbackKeepsOldRequestAndContent() {
        var fixture = seedRunningSummary();
        var oldSummary = row(fixture.worklogId(), "SUMMARY", fixture.worklogId());
        var oldTag = row(fixture.worklogId(), "TAG", fixture.worklogId());

        assertThatThrownBy(() -> transaction.executeWithoutResult(status -> {
            worklogService.updateWorklog(fixture.author(), fixture.worklogId(), update(null, null, "새 내용", null), List.of());
            throw new IllegalStateException("force rollback");
        })).isInstanceOf(IllegalStateException.class);

        assertThat(row(fixture.worklogId(), "SUMMARY", fixture.worklogId())).isEqualTo(oldSummary);
        assertThat(row(fixture.worklogId(), "TAG", fixture.worklogId())).isEqualTo(oldTag);
        assertThat(results.apply(fixture.worklogId(), fixture.result())).isTrue();
    }

    @Test
    @DisplayName("수정 뒤 도착한 옛 실행의 실패 결과는 업무 상태와 알림을 바꾸지 않는다")
    void lateFailureOfSupersededExecutionIsStale() {
        var fixture = seedRunningSummary();
        worklogService.updateWorklog(fixture.author(), fixture.worklogId(), update(null, null, "새 내용", null), List.of());

        assertThat(results.apply(fixture.worklogId(), failed(fixture.result()))).isFalse();

        assertThat(aiProcessingStatus(fixture.worklogId())).isEqualTo("PROCESSING");
        assertThat(notificationCount(fixture.result().requestId())).isZero();
    }

    @Test
    @DisplayName("수동 재시도 요청이 실행 전 본문 수정으로 교체되면 옛 실패 알림은 재시도 불가가 되고 다시 눌러도 거절된다")
    void retryThenEditMakesOldFailureNotificationNotRetryable() {
        var fixture = seedRunningSummary();
        var failure = failed(fixture.result());
        assertThat(results.apply(fixture.worklogId(), failure)).isTrue();
        assertThat(retryable(fixture, failure.requestId())).isTrue();
        UUID retried = retries.retry(fixture.author(), fixture.worklogId(), failure.requestId());

        worklogService.updateWorklog(fixture.author(), fixture.worklogId(), update(null, null, "새 내용", null), List.of());

        var summary = row(fixture.worklogId(), "SUMMARY", fixture.worklogId());
        assertThat(summary.get("request_id")).isNotIn(failure.requestId(), retried);
        assertThat(summary.get("retried_from_request_id")).isNull();
        assertThat(summary.get("work_content")).isEqualTo("새 내용");
        assertThat(retryable(fixture, failure.requestId())).isFalse();
        assertRetryRejected(fixture, failure.requestId());
        assertRetryRejected(fixture, retried);
        assertThat(notificationCount(failure.requestId())).isEqualTo(1);
    }

    @Test
    @DisplayName("최종 실패한 요약의 입력이 수정되면 최신 입력의 새 요청을 만들고 옛 실패 알림은 재시도 불가가 된다")
    void editRerequestsFailedSummaryWithLatestInput() {
        var fixture = seedRunningSummary();
        var failure = failed(fixture.result());
        assertThat(results.apply(fixture.worklogId(), failure)).isTrue();
        var oldSummary = row(fixture.worklogId(), "SUMMARY", fixture.worklogId());
        assertThat(aiProcessingStatus(fixture.worklogId())).isEqualTo("FAILED");
        assertThat(retryable(fixture, failure.requestId())).isTrue();

        worklogService.updateWorklog(fixture.author(), fixture.worklogId(), update(null, null, "새 내용", null), List.of());

        var summary = row(fixture.worklogId(), "SUMMARY", fixture.worklogId());
        assertReplacedWithFreshBudget(summary, oldSummary);
        assertThat(summary.get("work_content")).isEqualTo("새 내용");
        assertThat(summary.get("retried_from_request_id")).isNull();
        assertThat(aiProcessingStatus(fixture.worklogId())).isEqualTo("PROCESSING");
        assertThat(retryable(fixture, failure.requestId())).isFalse();
        assertRetryRejected(fixture, failure.requestId());
        assertThat(notificationCount(failure.requestId())).isEqualTo(1);
        assertThat(tasks.claimDelivery(fixture.worklogId(), UUID.randomUUID(), true, true).orElseThrow().requestId())
                .isIn(summary.get("request_id"), row(fixture.worklogId(), "TAG", fixture.worklogId()).get("request_id"));
    }

    @Test
    @DisplayName("전달 확인 실패로 종결된 태그도 입력이 수정되면 최신 입력의 새 요청으로 다시 요청한다")
    void editRerequestsDeliveryExhaustedTag() {
        var fixture = seedRunningSummary();
        // 네 번의 전달 실패 경로는 별도 테스트가 검증하므로 여기서는 그 종결 상태(UNKNOWN+EXHAUSTED)만 재현한다.
        jdbc.update("""
                UPDATE tb_worklog_ai_post_process_outbox SET delivery_state = 'EXHAUSTED', attempts = 4,
                    result_error_code = 'RESULT_UNCONFIRMED'
                WHERE worklog_id = ? AND task_kind = 'TAG'
                """, fixture.worklogId());
        var oldTag = row(fixture.worklogId(), "TAG", fixture.worklogId());

        worklogService.updateWorklog(fixture.author(), fixture.worklogId(), update(null, null, "새 내용", null), List.of());

        var tag = row(fixture.worklogId(), "TAG", fixture.worklogId());
        assertReplacedWithFreshBudget(tag, oldTag);
        assertThat(jdbc.queryForObject("SELECT result_error_code FROM tb_worklog_ai_post_process_outbox WHERE request_id = ?",
                String.class, tag.get("request_id"))).isNull();
    }

    @Test
    @DisplayName("최종 실패한 요약의 재요청도 업무 수정이 롤백되면 함께 롤백된다")
    void rollbackKeepsFailedSummary() {
        var fixture = seedRunningSummary();
        assertThat(results.apply(fixture.worklogId(), failed(fixture.result()))).isTrue();
        var failedSummary = row(fixture.worklogId(), "SUMMARY", fixture.worklogId());

        assertThatThrownBy(() -> transaction.executeWithoutResult(status -> {
            worklogService.updateWorklog(fixture.author(), fixture.worklogId(), update(null, null, "새 내용", null), List.of());
            throw new IllegalStateException("force rollback");
        })).isInstanceOf(IllegalStateException.class);

        assertThat(row(fixture.worklogId(), "SUMMARY", fixture.worklogId())).isEqualTo(failedSummary);
        assertThat(aiProcessingStatus(fixture.worklogId())).isEqualTo("FAILED");
        assertThat(retryable(fixture, fixture.result().requestId())).isTrue();
    }

    @Test
    @DisplayName("최종 실패한 요약도 입력과 무관한 수정에서는 다시 요청하지 않는다")
    void unrelatedEditKeepsFailedSummary() {
        var fixture = seedRunningSummary();
        assertThat(results.apply(fixture.worklogId(), failed(fixture.result()))).isTrue();
        var failedSummary = row(fixture.worklogId(), "SUMMARY", fixture.worklogId());

        worklogService.updateWorklog(fixture.author(), fixture.worklogId(), update("바뀐 제목", null, null, null), List.of());

        assertThat(row(fixture.worklogId(), "SUMMARY", fixture.worklogId())).isEqualTo(failedSummary);
        assertThat(retryable(fixture, fixture.result().requestId())).isTrue();
    }

    @Test
    @DisplayName("이미 성공한 요약은 본문이 바뀌어도 자동으로 다시 요청하지 않는다")
    void succeededSummaryIsNotRerunAfterEdit() {
        var fixture = seedRunningSummary();
        assertThat(results.apply(fixture.worklogId(), fixture.result())).isTrue();
        var succeeded = row(fixture.worklogId(), "SUMMARY", fixture.worklogId());

        worklogService.updateWorklog(fixture.author(), fixture.worklogId(), update(null, null, "새 내용", null), List.of());

        assertThat(row(fixture.worklogId(), "SUMMARY", fixture.worklogId())).isEqualTo(succeeded);
        assertThat(succeeded.get("execution_state")).isEqualTo("SUCCEEDED");
    }

    @Test
    @DisplayName("파일을 제거하면 그 파일 작업만 무효화하고 다른 파일과 본문 작업은 유지한다")
    void removingFileSupersedesOnlyThatFileTask() {
        var fixture = seedWorklogWithoutTasks();
        File removed = saveFile(fixture.worklogId(), fixture.authorId(), "removed");
        File kept = saveFile(fixture.worklogId(), fixture.authorId(), "kept");
        enqueueFile(fixture, removed);
        var running = startOnly(fixture.worklogId());
        enqueueFile(fixture, kept);
        var keptBefore = row(fixture.worklogId(), "FILE_SUMMARY", kept.getId());

        worklogService.updateWorklog(fixture.author(), fixture.worklogId(),
                update(null, null, null, List.of(removed.getId())), List.of());

        var removedRow = row(fixture.worklogId(), "FILE_SUMMARY", removed.getId());
        assertThat(removedRow.get("execution_state")).isEqualTo("SUPERSEDED");
        assertThat(row(fixture.worklogId(), "FILE_SUMMARY", kept.getId())).isEqualTo(keptBefore);
        var late = new AiTaskApiDto.Result(running.claim().requestId(), running.identity().executionId(), running.claim().inputHash(), "FILE_SUMMARY", removed.getId(), "COMPLETED", null, "파일 요약", null, null);
        assertThat(results.apply(fixture.worklogId(), late)).isFalse();
        assertThat(notificationCount(running.claim().requestId())).isZero();
        var next = tasks.claimDelivery(fixture.worklogId(), UUID.randomUUID(), true, true).orElseThrow();
        assertThat(next.targetId()).isEqualTo(kept.getId());
        assertThat(tasks.claimDelivery(fixture.worklogId(), UUID.randomUUID(), true, true)).isEmpty();
    }

    @Test
    @DisplayName("본문만 수정하면 진행 중 파일 요약 작업을 취소하지 않는다")
    void bodyChangeKeepsFileTasks() {
        var fixture = seedWorklogWithoutTasks();
        File file = saveFile(fixture.worklogId(), fixture.authorId(), "body");
        enqueueFile(fixture, file);
        var running = startOnly(fixture.worklogId());
        var before = row(fixture.worklogId(), "FILE_SUMMARY", file.getId());

        worklogService.updateWorklog(fixture.author(), fixture.worklogId(), update(null, "새 요청", "새 내용", null), List.of());

        assertThat(row(fixture.worklogId(), "FILE_SUMMARY", file.getId())).isEqualTo(before);
    }

    @Test
    @DisplayName("업무를 삭제하면 진행 중 요약·태그 작업을 종결해 더 발행하거나 결과를 반영하지 않는다")
    void deletingWorklogSupersedesInFlightTasks() {
        var fixture = seedRunningSummary();

        worklogService.deleteWorklog(fixture.author(), fixture.worklogId());

        assertThat(row(fixture.worklogId(), "SUMMARY", fixture.worklogId()).get("execution_state")).isEqualTo("SUPERSEDED");
        assertThat(row(fixture.worklogId(), "TAG", fixture.worklogId()).get("execution_state")).isEqualTo("SUPERSEDED");
        assertThat(tasks.claimDelivery(fixture.worklogId(), UUID.randomUUID(), true, true)).isEmpty();
        assertThat(results.apply(fixture.worklogId(), fixture.result())).isFalse();
        assertThat(notificationCount(fixture.result().requestId())).isZero();
    }

    /** 교체된 옛 요청 재시도는 다른 거절 사유가 아니라 재시도 상태 불일치(409)로 거절돼야 한다. */
    private void assertRetryRejected(Fixture fixture, UUID requestId) {
        assertThatThrownBy(() -> retries.retry(fixture.author(), fixture.worklogId(), requestId))
                .isInstanceOfSatisfying(BusinessException.class, exception -> {
                    assertThat(exception.getErrorCode()).isEqualTo(ErrorCode.WORKLOG_AI_RETRY_STATUS_INVALID);
                    assertThat(exception.getErrorCode().getStatus()).isEqualTo(HttpStatus.CONFLICT);
                });
    }

    /** 교체된 행은 새 요청·발행 세대 1·전달/실행 예산 0에서 다시 시작해야 한다. */
    private void assertReplacedWithFreshBudget(Map<String, Object> current, Map<String, Object> old) {
        assertThat(current.get("request_id")).isNotEqualTo(old.get("request_id"));
        assertThat(current.get("input_hash")).isNotEqualTo(old.get("input_hash"));
        assertThat(current.get("execution_state")).isEqualTo("UNKNOWN");
        assertThat(current.get("delivery_state")).isEqualTo("PENDING");
        assertThat(current.get("attempts")).isEqualTo(0);
        assertThat(current.get("claim_token")).isNull();
    }

    /** 커밋된 DB 기준으로 작업 행의 식별·예산·입력 snapshot 을 비교 가능한 값으로 읽는다. */
    private Map<String, Object> row(Long worklogId, String kind, Long targetId) {
        return jdbc.queryForMap("""
                SELECT request_id, retried_from_request_id, input_hash, execution_state, delivery_state,
                       attempts, claim_token, payload->>'workContent' AS work_content
                FROM tb_worklog_ai_post_process_outbox WHERE worklog_id = ? AND task_kind = ? AND target_id = ?
                """, worklogId, kind, targetId);
    }

    /** 본문 입력만 바꾸는 부분 수정 요청이다. null 필드는 변경하지 않는다. */
    private UpdateWorklogApiDto.Request update(String title, String requestContent, String workContent, List<Long> removeFileIds) {
        return new UpdateWorklogApiDto.Request(title, requestContent, workContent, null, null, null, null, null, null,
                null, null, null, null, removeFileIds);
    }

    /** 같은 실행의 실패 최종 결과이며 성공 fixture 와 실행 권위만 공유한다. */
    private AiTaskApiDto.Result failed(AiTaskApiDto.Result result) {
        return new AiTaskApiDto.Result(result.requestId(), result.executionId(), result.inputHash(), result.taskKind(), result.targetId(), "FAILED", "TimeoutError", null, null, null);
    }

    /** 옛 결과의 식별자로 다른 실행 시작이나 heartbeat 를 시도하기 위한 identity 다. */
    private AiTaskApiDto.Identity identity(AiTaskApiDto.Result result, UUID executionId, UUID token) {
        return new AiTaskApiDto.Identity(result.requestId(), executionId, result.inputHash(), result.taskKind(), result.targetId());
    }

    /** 사용자 알림 목록에 노출되는 재시도 가능 여부를 공통 predicate 로 확인한다. */
    private boolean retryable(Fixture fixture, UUID requestId) {
        return notifications.searchNotifications(fixture.authorId(), new NotificationSearchQuery(null, null, null, 1, 20))
                .getContent().stream().filter(item -> requestId.equals(item.retryRequestId()))
                .findFirst().orElseThrow().retryable();
    }

    /** 영속성 컨텍스트가 아닌 커밋된 DB 값으로 요약 반영 여부를 확인한다. */
    private String summary(Long worklogId) {
        return jdbc.queryForObject("SELECT ai_summary FROM tb_worklog WHERE worklog_id = ?", String.class, worklogId);
    }

    /** 옛 실패 결과가 업무 처리 상태를 덮었는지 커밋된 값으로 확인한다. */
    private String aiProcessingStatus(Long worklogId) {
        return jdbc.queryForObject("SELECT ai_processing_status FROM tb_worklog WHERE worklog_id = ?", String.class, worklogId);
    }

    /** 요청별 알림이 실제로 저장됐는지 확인한다. */
    private int notificationCount(UUID requestId) {
        return jdbc.queryForObject("SELECT count(*) FROM tb_notification WHERE ai_request_id = ?", Integer.class, requestId);
    }

    /** 요약은 실행 중(RUNNING), 태그는 발행 전(UNKNOWN)인 등록 직후 상태를 만든다. */
    private Fixture seedRunningSummary() {
        var fixture = seedWorklogWithoutTasks();
        tasks.enqueueInitialTasks(new WorklogAiPostProcessRequestedEvent(fixture.worklogId(), "업무 제목", "요청", "내용",
                fixture.authorId(), fixture.teamId(), fixture.departmentId(), List.of()));
        jdbc.update("UPDATE tb_worklog SET ai_processing_status = 'PROCESSING' WHERE worklog_id = ?", fixture.worklogId());
        var running = startOnly(fixture.worklogId());
        assertThat(running.claim().taskKind()).isEqualTo("SUMMARY");
        var result = new AiTaskApiDto.Result(running.claim().requestId(), running.identity().executionId(), running.claim().inputHash(), "SUMMARY", fixture.worklogId(), "COMPLETED", null, "AI 요약", null, null);
        return new Fixture(fixture.worklogId(), fixture.authorId(), fixture.teamId(), fixture.departmentId(), result);
    }

    /** 다음 발행 대상 하나를 선점하고 실행 승인까지 받아 진행 중 실행을 만든다. */
    private Running startOnly(Long worklogId) {
        var claim = tasks.claimDelivery(worklogId, UUID.randomUUID(), true, true).orElseThrow();
        var identity = new AiTaskApiDto.Identity(claim.requestId(), UUID.randomUUID(), claim.inputHash(), claim.taskKind(), claim.targetId());
        UUID token = tasks.start(worklogId, identity).orElseThrow();
        return new Running(claim, new AiTaskApiDto.Identity(claim.requestId(), identity.executionId(), claim.inputHash(), claim.taskKind(), claim.targetId()));
    }

    /** 파일마다 다른 저장 key 를 써서 작업 입력 hash 가 서로 구분되게 한다. */
    private File saveFile(Long worklogId, Long authorId, String name) {
        return files.saveAndFlush(File.create(worklogId, authorId, name + "-key",
                new MockMultipartFile("file", name + ".pdf", "application/pdf", new byte[]{1})));
    }

    /** 수정 중 파일 추가 경로와 같은 단일 파일 작업 행을 만든다. */
    private void enqueueFile(Fixture fixture, File file) {
        var target = new WorklogAiPostProcessRequestedEvent.FileSummaryTarget(file.getId(), file.getStoredPath(),
                file.getOriginalName(), file.getFileExtension());
        tasks.enqueueFileTask(new WorklogAiPostProcessRequestedEvent(fixture.worklogId(), "업무 제목", "요청", "내용",
                fixture.authorId(), fixture.teamId(), fixture.departmentId(), List.of(target)), target);
    }

    /** 작업 행 없이 업무만 만들어 각 테스트가 필요한 작업 상태를 직접 구성하게 한다. */
    private Fixture seedWorklogWithoutTasks() {
        String suffix = UUID.randomUUID().toString().substring(0, 8);
        LocalDate today = LocalDate.of(2026, 11, 10);
        Department department = Department.create("교체부서-" + suffix, "test");
        department.changeStatus(DepartmentStatus.ACTIVE);
        department = departments.saveAndFlush(department);
        User user = users.saveAndFlush(User.create(department.getId(), "작성자", suffix + "@test.invalid",
                "$2a$10$abcdefghijklmnopqrstuv", UserRole.MEMBER, EmploymentStatus.ACTIVE, "사원", null, today, null, null));
        Team team = teams.saveAndFlush(Team.create(department.getId(), "교체팀-" + suffix, TeamStatus.ACTIVE, "test", today, null));
        Worklog worklog = worklogs.saveAndFlush(Worklog.create(user.getId(), team.getId(), "업무 제목", "요청", "내용",
                WorklogStatus.PENDING, WorklogImportance.NORMAL, BigDecimal.ZERO, today, today.plusDays(1)));
        return new Fixture(worklog.getId(), user.getId(), team.getId(), department.getId(), null);
    }

    private record Fixture(Long worklogId, Long authorId, Long teamId, Long departmentId, AiTaskApiDto.Result result) {
        CustomUserPrincipal author() {
            return new CustomUserPrincipal(authorId, "author@test.invalid", "MEMBER");
        }
    }

    private record Running(WorklogAiTaskRepository.DeliveryClaim claim, AiTaskApiDto.Identity identity) {}
}
