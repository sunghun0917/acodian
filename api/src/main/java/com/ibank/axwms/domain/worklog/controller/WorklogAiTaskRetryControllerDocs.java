package com.ibank.axwms.domain.worklog.controller;

import com.ibank.axwms.global.security.CustomUserPrincipal;
import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.tags.Tag;

@Tag(name = "WorklogAiTaskRetry", description = "실패한 AI 작업 수동 재시도")
public interface WorklogAiTaskRetryControllerDocs {
    @Operation(summary = "현재 실패한 AI 작업 하나를 작성자가 재시도")
    WorklogAiTaskRetryController.Response retry(CustomUserPrincipal principal, Long worklogId, String requestId);
}
