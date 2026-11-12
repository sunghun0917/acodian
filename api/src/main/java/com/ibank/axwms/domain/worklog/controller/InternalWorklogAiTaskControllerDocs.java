package com.ibank.axwms.domain.worklog.controller;

import com.ibank.axwms.domain.worklog.dto.AiTaskApiDto;
import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.tags.Tag;

@Tag(name = "InternalWorklogAiTask", description = "내부 토큰으로 보호된 AI 작업 실행 제어")
public interface InternalWorklogAiTaskControllerDocs {
    @Operation(summary = "현재 작업 실행 시작 승인")
    AiTaskApiDto.StartResponse start(Long worklogId, String token, AiTaskApiDto.Identity identity);

    @Operation(summary = "일시적 실행 실패와 재시도 가능 여부 기록")
    AiTaskApiDto.TransitionResponse attemptResult(Long worklogId, String token, AiTaskApiDto.AttemptResult request);

    @Operation(summary = "현재 실행의 최종 결과와 작업별 알림 반영")
    AiTaskApiDto.ResultResponse result(Long worklogId, String token, AiTaskApiDto.Result request);
}
