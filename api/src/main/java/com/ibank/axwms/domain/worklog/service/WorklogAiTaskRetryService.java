package com.ibank.axwms.domain.worklog.service;

import com.ibank.axwms.domain.worklog.repository.WorklogAiPostProcessOutboxRepository;
import com.ibank.axwms.domain.worklog.repository.WorklogAiTaskRepository;
import com.ibank.axwms.domain.worklog.repository.WorklogRepository;
import com.ibank.axwms.domain.file.repository.FileRepository;
import com.ibank.axwms.global.error.BusinessException;
import com.ibank.axwms.global.error.ErrorCode;
import com.ibank.axwms.global.security.CustomUserPrincipal;
import java.util.UUID;
import lombok.RequiredArgsConstructor;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/** 작성자가 현재 실패한 일반 AI 작업만 별도 요청으로 재개한다. */
@Service
@RequiredArgsConstructor
public class WorklogAiTaskRetryService {
    private final WorklogAiPostProcessOutboxRepository locks;
    private final WorklogAiTaskRepository tasks;
    private final WorklogRepository worklogs;
    private final FileRepository files;
    private final JdbcTemplate jdbcTemplate;

    /** 기존 상세 화면도 통합 후처리 대신 현재 실패한 요약 작업만 재시도한다. */
    @Transactional
    public UUID retryCurrentSummary(CustomUserPrincipal principal, Long worklogId) {
        UUID requestId = tasks.currentSummaryRequest(worklogId)
                .orElseThrow(() -> new BusinessException(ErrorCode.WORKLOG_AI_RETRY_STATUS_INVALID));
        return retry(principal, worklogId, requestId);
    }

    /** 업무→작업 잠금으로 이중 클릭을 동일한 새 요청에 수렴시킨다. */
    @Transactional
    public UUID retry(CustomUserPrincipal principal, Long worklogId, UUID requestId) {
        if (principal == null || principal.userId() == null || worklogId == null || worklogId <= 0 || requestId == null) {
            throw new BusinessException(ErrorCode.COMMON_INVALID_REQUEST);
        }
        var deleted = locks.lockWorklog(worklogId);
        if (deleted.isEmpty()) {
            throw new BusinessException(ErrorCode.WORKLOG_NOT_FOUND);
        }
        UUID indexRetry = retryIndex(principal, worklogId, requestId, deleted.get());
        if (indexRetry != null) {
            return indexRetry;
        }
        if (deleted.get()) {
            throw new BusinessException(ErrorCode.WORKLOG_NOT_FOUND);
        }
        var worklog = worklogs.findById(worklogId).orElseThrow(() -> new BusinessException(ErrorCode.WORKLOG_NOT_FOUND));
        if (!principal.userId().equals(worklog.getAuthorId())) {
            throw new BusinessException(ErrorCode.WORKLOG_EDIT_FORBIDDEN);
        }
        var candidate = tasks.lockRetryCandidate(worklogId, requestId)
                .orElseThrow(() -> new BusinessException(ErrorCode.WORKLOG_AI_RETRY_STATUS_INVALID));
        if (requestId.equals(candidate.retriedFromRequestId())) {
            return candidate.requestId();
        }
        boolean failed = "FAILED".equals(candidate.executionState());
        if (!failed) {
            throw new BusinessException(ErrorCode.WORKLOG_AI_RETRY_STATUS_INVALID);
        }
        if ("FILE_SUMMARY".equals(candidate.taskKind())) {
            var file = files.findByIdForUpdate(candidate.targetId())
                    .orElseThrow(() -> new BusinessException(ErrorCode.WORKLOG_AI_RETRY_STATUS_INVALID));
            if (Boolean.TRUE.equals(file.getIsDeleted()) || !worklogId.equals(file.getWorklogId())
                    || !tasks.isCurrentFileInput(candidate.inputHash(), file.getId(), file.getStoredPath(),
                    file.getOriginalName(), file.getFileExtension())) {
                throw new BusinessException(ErrorCode.WORKLOG_AI_RETRY_STATUS_INVALID);
            }
            file.startAiSummaryProcessing();
        } else if (!tasks.isCurrentWorklogInput(candidate.taskKind(), candidate.inputHash(),
                worklog.getRequestContent(), worklog.getWorkContent())) {
            throw new BusinessException(ErrorCode.WORKLOG_AI_RETRY_STATUS_INVALID);
        }
        if ("SUMMARY".equals(candidate.taskKind())) {
            worklog.startAiProcessing();
        }
        return tasks.createRetry(worklogId, candidate);
    }

    /** 최신 최종 실패·작성자·유효 대상가 함께 있어야 새 요청을 만들며 중복 클릭은 기존 요청을 돌려준다. */
    private UUID retryIndex(CustomUserPrincipal principal, Long worklogId, UUID requestId, boolean deleted) {
        if (!Boolean.TRUE.equals(jdbcTemplate.queryForObject("""
                SELECT EXISTS (SELECT 1 FROM tb_worklog_light_outbox
                    WHERE worklog_id = ? AND (request_id = ? OR retried_from_request_id = ?))
                """, Boolean.class, worklogId, requestId, requestId))) {
            return null;
        }
        var rows = jdbcTemplate.queryForList("""
                SELECT o.operation, o.request_id, o.retried_from_request_id, o.terminal_failed, w.author_id FROM tb_worklog_light_outbox o
                JOIN tb_worklog w ON w.worklog_id = o.worklog_id AND w.light_index_request_id = o.request_id
                WHERE o.worklog_id = ? AND (o.request_id = ? OR o.retried_from_request_id = ?)
                FOR UPDATE OF o
                """, worklogId, requestId, requestId);
        if (rows.isEmpty()) {
            return null;
        }
        String operation = (String) rows.getFirst().get("operation");
        if (!principal.userId().equals(((Number) rows.getFirst().get("author_id")).longValue())) {
            throw new BusinessException(ErrorCode.WORKLOG_EDIT_FORBIDDEN);
        }
        if ("DELETE".equals(operation) != deleted) {
            throw new BusinessException(ErrorCode.WORKLOG_AI_RETRY_STATUS_INVALID);
        }
        UUID current = (UUID) rows.getFirst().get("request_id");
        if (requestId.equals(rows.getFirst().get("retried_from_request_id"))) {
            return current;
        }
        if (!Boolean.TRUE.equals(rows.getFirst().get("terminal_failed"))) {
            throw new BusinessException(ErrorCode.WORKLOG_AI_RETRY_STATUS_INVALID);
        }
        UUID next = UUID.randomUUID();
        jdbcTemplate.update("""
                UPDATE tb_worklog_light_outbox SET retried_from_request_id = request_id, request_id = ?,
                    revision = revision + 1, attempts = 0, terminal_failed = false, claim_token = NULL,
                    lease_until = NULL, next_attempt_at = CURRENT_TIMESTAMP, last_error = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE worklog_id = ? AND request_id = ? AND terminal_failed
                """, next, worklogId, current);
        jdbcTemplate.update("""
                UPDATE tb_worklog SET light_index_request_id = ?, light_index_execution_id = NULL,
                    light_index_status = 'PENDING', light_index_error = NULL, light_index_completed_at = NULL
                WHERE worklog_id = ? AND light_index_request_id = ?
                """, next, worklogId, current);
        return next;
    }
}
