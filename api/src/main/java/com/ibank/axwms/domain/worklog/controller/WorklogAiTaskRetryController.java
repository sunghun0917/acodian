package com.ibank.axwms.domain.worklog.controller;

import com.ibank.axwms.domain.worklog.service.WorklogAiTaskRetryService;
import com.ibank.axwms.global.security.CustomUserPrincipal;
import com.ibank.axwms.global.error.BusinessException;
import com.ibank.axwms.global.error.ErrorCode;
import java.util.UUID;
import lombok.RequiredArgsConstructor;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/** 실패 작업별 재시도는 작성자 검증을 수행하는 서비스에 위임한다. */
@RestController
@RequestMapping("/worklogs/{worklogId}/ai-tasks")
@RequiredArgsConstructor
public class WorklogAiTaskRetryController implements WorklogAiTaskRetryControllerDocs {
    private final WorklogAiTaskRetryService service;

    @Override
    @PostMapping("/{requestId}/retry")
    @PreAuthorize("hasAnyRole('DIRECTOR','DEPT_HEAD','TEAM_LEAD','MEMBER')")
    public Response retry(@AuthenticationPrincipal CustomUserPrincipal principal,
                          @PathVariable Long worklogId, @PathVariable String requestId) {
        return new Response(service.retry(principal, worklogId, parseRequestId(requestId)));
    }

    /** 잘못된 요청 ID가 서버 내부 오류 대신 잘못된 요청으로 처리되게 한다. */
    private UUID parseRequestId(String requestId) {
        try {
            return UUID.fromString(requestId);
        } catch (IllegalArgumentException e) {
            throw new BusinessException(ErrorCode.COMMON_INVALID_REQUEST);
        }
    }

    /** 같은 실패 요청의 중복 클릭은 동일한 새 요청 ID를 반환한다. */
    public record Response(UUID requestId) {}
}
