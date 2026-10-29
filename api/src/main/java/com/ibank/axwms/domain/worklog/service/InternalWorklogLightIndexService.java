package com.ibank.axwms.domain.worklog.service;

import com.ibank.axwms.domain.worklog.repository.WorklogLightOutboxRepository;
import java.util.UUID;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/** 실행 식별자로 결과 권위를 구분하고 색인 Outbox를 완료하거나 재전달한다. */
@Slf4j
@Service
@RequiredArgsConstructor
public class InternalWorklogLightIndexService {
    private final JdbcTemplate jdbcTemplate;
    private final WorklogLightOutboxRepository lightOutboxRepository;

    /** 삭제된 원본도 최신 삭제 요청은 실행하되 완료 요청과 과거 요청은 차단한다. */
    @Transactional
    public boolean start(Long worklogId, UUID requestId, UUID executionId) {
        int changed = jdbcTemplate.update("""
                UPDATE tb_worklog SET light_index_execution_id = ?, light_index_status = 'RUNNING',
                    light_index_error = NULL, light_index_completed_at = NULL
                WHERE worklog_id = ? AND light_index_request_id = ?
                  AND light_index_status <> 'COMPLETED'
                  AND EXISTS (
                      SELECT 1 FROM tb_worklog_light_outbox o
                      WHERE o.worklog_id = tb_worklog.worklog_id AND o.request_id = tb_worklog.light_index_request_id
                        AND NOT o.terminal_failed)
                  AND (light_index_execution_id IS DISTINCT FROM ? OR light_index_status = 'RUNNING')
                """, executionId, worklogId, requestId, executionId);
        log.info("Light index start worklogId={} requestId={} executionId={} execute={}",
                worklogId, requestId, executionId, changed == 1);
        return changed == 1;
    }

    /** 동일 실행의 최초 최종 결과만 반영하고 이전 요청·실행 및 모순된 재통보는 무시한다. */
    @Transactional
    public void result(Long worklogId, UUID requestId, UUID executionId, String status, String error) {
        var current = jdbcTemplate.queryForList("""
                SELECT light_index_status FROM tb_worklog
                WHERE worklog_id = ? AND light_index_request_id = ? AND light_index_execution_id = ?
                  AND EXISTS (SELECT 1 FROM tb_worklog_light_outbox o
                      WHERE o.worklog_id = tb_worklog.worklog_id AND o.request_id = tb_worklog.light_index_request_id)
                FOR UPDATE
                """, String.class, worklogId, requestId, executionId);
        if (current.isEmpty()) {
            return;
        }
        if (!"RUNNING".equals(current.getFirst())) {
            if (!status.equals(current.getFirst())) {
                log.warn("Contradictory Light result worklogId={} requestId={} executionId={}", worklogId, requestId, executionId);
            }
            return;
        }
        jdbcTemplate.queryForList("""
                SELECT worklog_id FROM tb_worklog_ai_post_process_outbox
                WHERE worklog_id = ? FOR UPDATE
                """, Long.class, worklogId);
        jdbcTemplate.update("""
                UPDATE tb_worklog SET light_index_status = ?, light_index_error = LEFT(?, 1000),
                    light_index_completed_at = CURRENT_TIMESTAMP WHERE worklog_id = ?
                """, status, error, worklogId);
        if ("COMPLETED".equals(status)) {
            lightOutboxRepository.completeResult(worklogId, requestId);
        } else {
            lightOutboxRepository.retryResult(worklogId, requestId, error);
        }
        jdbcTemplate.update("""
                UPDATE tb_worklog_ai_post_process_outbox SET next_attempt_at = CURRENT_TIMESTAMP
                WHERE worklog_id = ?
                """, worklogId);
        log.info("Light index result worklogId={} requestId={} executionId={} status={}", worklogId, requestId, executionId, status);
    }
}
