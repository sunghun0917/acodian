package com.ibank.axwms.domain.worklog.service;

import static org.assertj.core.api.Assertions.assertThat;
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
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.TestPropertySource;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.context.bean.override.mockito.MockitoSpyBean;

@TestPropertySource(properties = {
        "ai.worklog-post-process.outbox-poll-delay-ms=3600000",
        "ai.worklog-light-index.outbox-poll-delay-ms=3600000",
        "ai.worklog-pipeline.enabled=true",
        "ai.worklog-light-index.enabled=true"
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
    }

    @Test
    @DisplayName("색인 실패 후 재시도하면 성공한 요약·태그 접수를 반복하지 않고 완료한다")
    void index_retry_skips_previously_accepted_pipeline() {
        Fixture fixture = seedFixture();
        Worklog worklog = saveProcessingWorklog(fixture);
        outboxRepository.enqueue(event(fixture, worklog));
        doThrow(new IllegalStateException("index unavailable")).doNothing()
                .when(indexClient).requestIndex(any());

        assertThat(worker.processOne(worklog.getId())).isTrue();

        assertThat(processingStatus(worklog.getId())).isEqualTo(AiProcessingStatus.FAILED);
        assertThat(jobCount(worklog.getId())).isEqualTo(1);
        assertThat(hasCheckpoint(worklog.getId(), "WORKLOG_PIPELINE")).isTrue();
        assertThat(hasCheckpoint(worklog.getId(), "LIGHT_INDEX")).isFalse();
        assertThat(notificationCount(worklog.getId())).isEqualTo(1);
        makeDue(worklog.getId());

        assertThat(worker.processOne(worklog.getId())).isTrue();

        assertThat(processingStatus(worklog.getId())).isEqualTo(AiProcessingStatus.COMPLETED);
        assertThat(jobCount(worklog.getId())).isZero();
        verify(pipelineClient, times(1)).requestPipeline(pipelineRequest(fixture, worklog));
        verify(indexClient, times(2)).requestIndex(TriggerWorklogLightIndexDto.Request.of(worklog.getId()));
        verifyNoInteractions(fileSummaryClient);
        assertThat(notificationCount(worklog.getId())).isEqualTo(1);
    }

    @Test
    @DisplayName("색인 복구 중 접수된 명시적 요약 재요청은 이전 작업 완료에 사라지지 않고 별도로 실행된다")
    void manual_summary_retry_survives_completion_of_existing_index_retry() {
        Fixture fixture = seedFixture();
        Worklog worklog = saveProcessingWorklog(fixture);
        outboxRepository.enqueue(event(fixture, worklog));
        doThrow(new IllegalStateException("index unavailable")).doNothing()
                .when(indexClient).requestIndex(any());

        assertThat(worker.processOne(worklog.getId())).isTrue();
        assertThat(processingStatus(worklog.getId())).isEqualTo(AiProcessingStatus.FAILED);
        assertThat(hasCheckpoint(worklog.getId(), "WORKLOG_PIPELINE")).isTrue();

        worklogService.retryAiSummary(new CustomUserPrincipal(
                fixture.user().getId(), fixture.user().getEmail(), "MEMBER"), worklog.getId());

        assertThat(processingStatus(worklog.getId())).isEqualTo(AiProcessingStatus.PROCESSING);
        assertThat(worker.processOne(worklog.getId())).isTrue();
        assertThat(jobCount(worklog.getId())).isEqualTo(1);
        assertThat(processingStatus(worklog.getId())).isEqualTo(AiProcessingStatus.PROCESSING);

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
        outboxRepository.enqueue(event(fixture, worklog));
        doAnswer(invocation -> {
            invocation.callRealMethod();
            throw new IllegalStateException("notification transaction failed");
        }).doCallRealMethod().when(notificationService).createWorklogAiPostProcessResultNotification(
                anyLong(), anyLong(), anyLong(), anyLong(), anyString(), anyBoolean(), anyList());

        assertThat(worker.processOne(worklog.getId())).isTrue();

        assertThat(processingStatus(worklog.getId())).isEqualTo(AiProcessingStatus.PROCESSING);
        assertThat(notificationCount(worklog.getId())).isZero();
        assertThat(jobCount(worklog.getId())).isEqualTo(1);
        assertThat(hasCheckpoint(worklog.getId(), "WORKLOG_PIPELINE")).isTrue();
        assertThat(hasCheckpoint(worklog.getId(), "LIGHT_INDEX")).isTrue();
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
        verify(indexClient, times(1)).requestIndex(TriggerWorklogLightIndexDto.Request.of(worklog.getId()));
    }

    @Test
    @DisplayName("선점 전에 삭제된 원본의 등록 작업은 외부 요청이나 결과 알림 없이 취소한다")
    void deleted_source_cancels_job_without_remote_requests() {
        Fixture fixture = seedFixture();
        Worklog worklog = saveProcessingWorklog(fixture);
        outboxRepository.enqueue(event(fixture, worklog));
        outboxRepository.enqueue(event(fixture, worklog));
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
        outboxRepository.enqueue(event(fixture, worklog));
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

        assertThat(worker.processOne(null)).isTrue();

        assertThat(processingStatus(worklogId)).isEqualTo(AiProcessingStatus.COMPLETED);
        assertThat(jobCount(worklogId)).isZero();
        assertThat(notificationCount(worklogId)).isEqualTo(1);
        verify(pipelineClient).requestPipeline(new TriggerWorklogPipelineDto.Request(worklogId,
                request.requestContent(), request.workContent(), fixture.user().getId(),
                fixture.team().getId(), fixture.department().getId()));
        verify(indexClient).requestIndex(TriggerWorklogLightIndexDto.Request.of(worklogId));
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
