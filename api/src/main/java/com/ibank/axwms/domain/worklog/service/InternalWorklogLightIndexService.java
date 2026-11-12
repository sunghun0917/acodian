package com.ibank.axwms.domain.worklog.service;

import com.ibank.axwms.domain.notification.service.NotificationService;
import com.ibank.axwms.domain.worklog.config.AiTaskExecutionBudgetProperties;
import com.ibank.axwms.domain.worklog.repository.WorklogLightOutboxRepository;
import java.util.Map;
import java.util.UUID;
import lombok.RequiredArgsConstructor;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/** 최신 요청·실행의 첫 결과와 작업별 알림을 같은 트랜잭션에서 처리한다. */
@Service
@RequiredArgsConstructor
public class InternalWorklogLightIndexService {
    private final JdbcTemplate jdbcTemplate;
    private final WorklogLightOutboxRepository lightOutboxRepository;
    private final NotificationService notifications;
    private final AiTaskExecutionBudgetProperties budget;

    /** 현재 요청의 단일 lease 만료만 실패 처리하며 이미 교체된 요청은 건드리지 않는다. */
    @Transactional
    public boolean reconcile(WorklogLightOutboxRepository.Claim claim) {
        if (lockCurrent(claim.worklogId(), claim.requestId()).isEmpty()) return false;
        var rows = jdbcTemplate.queryForList("""
                SELECT worklog_id FROM tb_worklog_light_outbox WHERE worklog_id = ? AND request_id = ?
                    AND revision = ? AND NOT terminal_failed AND lease_until <= CURRENT_TIMESTAMP FOR UPDATE
                """, Long.class, claim.worklogId(), claim.requestId(), claim.revision());
        if (rows.isEmpty()) return false;
        finish(claim.worklogId(), claim.requestId(), "FAILED", "TASK_TIMEOUT");
        return true;
    }

    /** 전달 오류도 업무 잠금 아래 기록해 최종 실패와 알림을 원자적으로 반영한다. */
    @Transactional
    public void deliveryFailed(WorklogLightOutboxRepository.Claim claim, String error) {
        if (lockCurrent(claim.worklogId(), claim.requestId()).isEmpty()) return;
        lightOutboxRepository.fail(claim, error);
        if (lightOutboxRepository.isTerminalFailure(claim.worklogId())) {
            finish(claim.worklogId(), claim.requestId(), "FAILED", error);
        }
    }

    /** 현재 요청의 시작만 RUNNING으로 표시하며 같은 실행의 재전송은 멱등 허용한다. */
    @Transactional
    public boolean start(Long worklogId, UUID requestId, UUID executionId) {
        var rows = lockCurrent(worklogId, requestId);
        if (rows.isEmpty()) return false;
        var current = rows.getFirst();
        String state = (String) current.get("light_index_status");
        if ("RUNNING".equals(state)) {
            return executionId.equals(current.get("light_index_execution_id")) && Boolean.TRUE.equals(jdbcTemplate.queryForObject("""
                    SELECT EXISTS (SELECT 1 FROM tb_worklog_light_outbox WHERE worklog_id = ? AND request_id = ?
                        AND NOT terminal_failed AND lease_until > CURRENT_TIMESTAMP)
                    """, Boolean.class, worklogId, requestId));
        }
        if (!"PENDING".equals(state) || executionId.equals(current.get("light_index_execution_id"))) return false;
        int changed = jdbcTemplate.update("""
                UPDATE tb_worklog_light_outbox SET claim_token = NULL
                WHERE worklog_id = ? AND request_id = ? AND NOT terminal_failed AND attempts BETWEEN 1 AND ?
                  AND lease_until > CURRENT_TIMESTAMP
                  AND ((operation = 'DELETE') = ?)
                """, worklogId, requestId, budget.maxAttempts(), current.get("is_deleted"));
        if (changed == 0) return false;
        jdbcTemplate.update("""
                UPDATE tb_worklog SET light_index_execution_id = ?, light_index_status = 'RUNNING',
                    light_index_error = NULL, light_index_completed_at = NULL WHERE worklog_id = ?
                """, executionId, worklogId);
        return true;
    }

    /** 실패한 현재 실행만 남은 시도 횟수 안에서 backoff 후 다시 발행한다. */
    @Transactional
    public boolean attemptFailed(Long worklogId, UUID requestId, UUID executionId) {
        var rows = lockCurrent(worklogId, requestId);
        if (rows.isEmpty() || !executionId.equals(rows.getFirst().get("light_index_execution_id"))) return false;
        String state = (String) rows.getFirst().get("light_index_status");
        if ("PENDING".equals(state)) return true;
        if (!"RUNNING".equals(state)) return false;
        int changed = jdbcTemplate.update("""
                UPDATE tb_worklog_light_outbox SET lease_until = NULL, claim_token = NULL,
                    next_attempt_at = CURRENT_TIMESTAMP + (POWER(2, LEAST(attempts - 1, 8)) * INTERVAL '1 second')
                WHERE worklog_id = ? AND request_id = ? AND attempts < ? AND NOT terminal_failed
                    AND lease_until > CURRENT_TIMESTAMP
                """, worklogId, requestId, budget.maxAttempts());
        if (changed == 0) return false;
        jdbcTemplate.update("UPDATE tb_worklog SET light_index_status = 'PENDING' WHERE worklog_id = ?", worklogId);
        return true;
    }

    /** 교체된 요청·실행, timeout 뒤 결과, 중복·모순된 최종 통보는 모두 무시한다. */
    @Transactional
    public void result(Long worklogId, UUID requestId, UUID executionId, String status, String error) {
        var rows = lockCurrent(worklogId, requestId);
        if (rows.isEmpty() || !"RUNNING".equals(rows.getFirst().get("light_index_status"))
                || !executionId.equals(rows.getFirst().get("light_index_execution_id"))) return;
        if (!Boolean.TRUE.equals(jdbcTemplate.queryForObject("""
                SELECT EXISTS (SELECT 1 FROM tb_worklog_light_outbox WHERE worklog_id = ? AND request_id = ?
                    AND NOT terminal_failed AND lease_until > CURRENT_TIMESTAMP)
                """, Boolean.class, worklogId, requestId))) return;
        finish(worklogId, requestId, status, error);
    }

    /** 업무→Outbox 순서를 모든 콜백·재시도 경로에서 공유한다. */
    private java.util.List<Map<String, Object>> lockCurrent(Long worklogId, UUID requestId) {
        return jdbcTemplate.queryForList("""
                SELECT light_index_status, light_index_execution_id, is_deleted FROM tb_worklog
                WHERE worklog_id = ? AND light_index_request_id = ? FOR UPDATE
                """, worklogId, requestId);
    }

    /** 호출자가 업무 잠금을 가진 상태에서만 상태·알림·최신 의도를 함께 종결한다. */
    private void finish(Long worklogId, UUID requestId, String status, String error) {
        var snapshot = jdbcTemplate.queryForMap("""
                SELECT o.operation, w.author_id, w.team_id, w.title, t.department_id
                FROM tb_worklog_light_outbox o JOIN tb_worklog w ON w.worklog_id = o.worklog_id
                LEFT JOIN tb_team t ON t.team_id = w.team_id WHERE o.worklog_id = ? AND o.request_id = ?
                """, worklogId, requestId);
        jdbcTemplate.update("""
                UPDATE tb_worklog SET light_index_status = ?, light_index_error = LEFT(?, 1000),
                    light_index_completed_at = CURRENT_TIMESTAMP WHERE worklog_id = ? AND light_index_request_id = ?
                """, status, error, worklogId, requestId);
        Number departmentId = (Number) snapshot.get("department_id");
        notifications.createAiTaskResultNotification(((Number) snapshot.get("author_id")).longValue(),
                departmentId == null ? null : departmentId.longValue(), ((Number) snapshot.get("team_id")).longValue(),
                worklogId, (String) snapshot.get("title"), requestId, (String) snapshot.get("operation"), worklogId, status);
        if ("COMPLETED".equals(status)) lightOutboxRepository.completeResult(worklogId, requestId);
        else lightOutboxRepository.retryResult(worklogId, requestId, error);
    }
}
