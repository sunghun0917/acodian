package com.ibank.axwms.domain.worklog.service;

import static org.assertj.core.api.Assertions.*;

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
import com.ibank.axwms.domain.worklog.entity.Worklog;
import com.ibank.axwms.domain.worklog.event.WorklogAiPostProcessRequestedEvent;
import com.ibank.axwms.domain.worklog.repository.WorklogAiTaskRepository;
import com.ibank.axwms.domain.worklog.repository.WorklogRepository;
import com.ibank.axwms.testsupport.IntegrationTestSupport;
import java.math.BigDecimal;
import java.time.LocalDate;
import java.util.List;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.transaction.support.TransactionTemplate;

class WorklogAiTaskRecoveryIntegrationTest extends IntegrationTestSupport {
    @Autowired private DepartmentRepository departments;
    @Autowired private UserRepository users;
    @Autowired private TeamRepository teams;
    @Autowired private WorklogRepository worklogs;
    @Autowired private WorklogAiTaskRepository tasks;
    @Autowired private WorklogAiTaskRecoveryService recovery;
    @Autowired private JdbcTemplate jdbc;
    @Autowired private TransactionTemplate transaction;
    @Autowired private NotificationRepository notifications;

    @Test
    void timeoutFailsOnceAndOldExecutionCannotCommit() {
        var fixture = seed();
        jdbc.update("UPDATE tb_worklog_ai_post_process_outbox SET lease_until = CURRENT_TIMESTAMP - INTERVAL '1 second' WHERE request_id = ?", fixture.identity().requestId());
        assertThat(recovery.reconcile(fixture.candidate())).isTrue();
        assertThat(recovery.reconcile(fixture.candidate())).isFalse();
        assertThat(state(fixture.identity().requestId())).isEqualTo("FAILED");
        assertThat(countNotifications(fixture.identity().requestId())).isEqualTo(1);
        assertThat(jdbc.queryForObject("SELECT ai_result FROM tb_notification WHERE ai_request_id = ?", String.class, fixture.identity().requestId())).isEqualTo("FAILED");
        transaction.executeWithoutResult(ignored -> assertThat(tasks.lockResult(fixture.worklogId(), fixture.identity(), "COMPLETED", "a".repeat(64))).isEqualTo(WorklogAiTaskRepository.ResultAuthority.REJECTED));
        var items = notifications.searchNotifications(fixture.authorId(), new NotificationSearchQuery(null, null, null, 1, 20));
        assertThat(items.getContent().stream().filter(item -> fixture.identity().requestId().equals(item.retryRequestId())).findFirst().orElseThrow().retryable()).isTrue();
    }

    @Test
    void liveLeaseIsNotRevoked() {
        var fixture = seed();
        assertThat(recovery.reconcile(fixture.candidate())).isFalse();
        assertThat(state(fixture.identity().requestId())).isEqualTo("RUNNING");
        assertThat(countNotifications(fixture.identity().requestId())).isZero();
    }

    @Test
    void acceptedButNeverStartedTimesOut() {
        var fixture = seed();
        jdbc.update("UPDATE tb_worklog_ai_post_process_outbox SET execution_state = 'QUEUED', execution_id = NULL, lease_until = CURRENT_TIMESTAMP - INTERVAL '1 second' WHERE request_id = ?", fixture.identity().requestId());
        assertThat(recovery.reconcile(fixture.candidate())).isTrue();
        assertThat(state(fixture.identity().requestId())).isEqualTo("FAILED");
        assertThat(countNotifications(fixture.identity().requestId())).isEqualTo(1);
    }

    @Test
    void notificationAndTimeoutRollbackTogether() {
        var fixture = seed();
        jdbc.update("UPDATE tb_worklog_ai_post_process_outbox SET lease_until = CURRENT_TIMESTAMP - INTERVAL '1 second' WHERE request_id = ?", fixture.identity().requestId());
        assertThatThrownBy(() -> transaction.executeWithoutResult(ignored -> {
            assertThat(recovery.reconcile(fixture.candidate())).isTrue();
            throw new IllegalStateException("rollback");
        })).isInstanceOf(IllegalStateException.class);
        assertThat(state(fixture.identity().requestId())).isEqualTo("RUNNING");
        assertThat(countNotifications(fixture.identity().requestId())).isZero();
    }

    /** 테스트 전용 DB에서 실제 FK와 현재 실행 권한을 생성한다. */
    private Fixture seed() {
        String suffix = UUID.randomUUID().toString().substring(0, 8);
        LocalDate today = LocalDate.of(2026, 11, 1);
        var department = Department.create("복구부서-" + suffix, "test");
        department.changeStatus(DepartmentStatus.ACTIVE);
        department = departments.saveAndFlush(department);
        var user = users.saveAndFlush(User.create(department.getId(), "작성자", suffix + "@recovery.invalid", "$2a$10$abcdefghijklmnopqrstuv", UserRole.MEMBER, EmploymentStatus.ACTIVE, "사원", null, today, null, null));
        var team = teams.saveAndFlush(Team.create(department.getId(), "복구팀-" + suffix, TeamStatus.ACTIVE, "test", today, null));
        var worklog = worklogs.saveAndFlush(Worklog.create(user.getId(), team.getId(), "복구 업무", "요청", "내용", WorklogStatus.PENDING, WorklogImportance.NORMAL, BigDecimal.ZERO, today, today.plusDays(1)));
        tasks.enqueueInitialTasks(new WorklogAiPostProcessRequestedEvent(worklog.getId(), worklog.getTitle(), "요청", "내용", user.getId(), team.getId(), department.getId(), List.of()));
        var claim = tasks.claimDelivery(worklog.getId(), UUID.randomUUID(), true, true).orElseThrow();
        UUID execution = UUID.randomUUID();
        var start = new AiTaskApiDto.Identity(claim.requestId(), execution, claim.inputHash(), claim.taskKind(), claim.targetId());
        UUID token = tasks.start(worklog.getId(), start).orElseThrow();
        var identity = new AiTaskApiDto.Identity(claim.requestId(), execution, claim.inputHash(), claim.taskKind(), claim.targetId());
        return new Fixture(worklog.getId(), user.getId(), identity, new WorklogAiTaskRepository.RecoveryCandidate(worklog.getId(), claim.taskKind(), claim.targetId(), claim.requestId()));
    }

    /** 결과 확인에 필요한 현재 상태만 직접 조회한다. */
    private String state(UUID id) {
        return jdbc.queryForObject("SELECT execution_state FROM tb_worklog_ai_post_process_outbox WHERE request_id = ?", String.class, id);
    }

    /** 특정 요청의 알림 중복을 확인한다. */
    private int countNotifications(UUID id) {
        return jdbc.queryForObject("SELECT COUNT(*) FROM tb_notification WHERE ai_request_id = ?", Integer.class, id);
    }

    private record Fixture(Long worklogId, Long authorId, AiTaskApiDto.Identity identity, WorklogAiTaskRepository.RecoveryCandidate candidate) {}
}
