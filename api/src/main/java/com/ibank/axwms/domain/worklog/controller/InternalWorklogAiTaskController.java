package com.ibank.axwms.domain.worklog.controller;

import com.ibank.axwms.domain.worklog.dto.AiTaskApiDto;
import com.ibank.axwms.domain.worklog.external.AiWorklogLightIndexProperties;
import com.ibank.axwms.domain.worklog.repository.WorklogAiTaskRepository;
import com.ibank.axwms.domain.worklog.service.InternalWorklogAiTaskResultService;
import com.ibank.axwms.domain.worklog.service.InternalWorklogAiTaskStartService;
import com.ibank.axwms.global.error.BusinessException;
import com.ibank.axwms.global.error.ErrorCode;
import jakarta.validation.Valid;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.UUID;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.*;

/** AI 작업 실행 권위를 내부 토큰으로 인증된 호출자에게만 제공한다. */
@RestController
@RequestMapping("/internal/worklogs/{worklogId}/ai-tasks")
@RequiredArgsConstructor
public class InternalWorklogAiTaskController implements InternalWorklogAiTaskControllerDocs {
    private final WorklogAiTaskRepository repository;
    private final InternalWorklogAiTaskResultService resultService;
    private final InternalWorklogAiTaskStartService startService;
    private final AiWorklogLightIndexProperties properties;

    @Override
    @PostMapping("/start")
    public AiTaskApiDto.StartResponse start(@PathVariable Long worklogId,
            @RequestHeader(value = "X-AI-Internal-Token", required = false) String token,
            @Valid @RequestBody AiTaskApiDto.Identity identity) {
        validateRequest(worklogId, token);
        return new AiTaskApiDto.StartResponse(startService.start(worklogId, identity).isPresent());
    }

    @Override
    @PostMapping("/attempt-result")
    public AiTaskApiDto.TransitionResponse attemptResult(@PathVariable Long worklogId,
            @RequestHeader(value = "X-AI-Internal-Token", required = false) String token,
            @Valid @RequestBody AiTaskApiDto.AttemptResult request) {
        validateRequest(worklogId, token);
        if (!request.retryable()) {
            throw new BusinessException(ErrorCode.COMMON_INVALID_REQUEST);
        }
        return repository.retryableFailure(worklogId, request.identity(), request.errorCode());
    }

    @Override
    @PostMapping("/result")
    public AiTaskApiDto.ResultResponse result(@PathVariable Long worklogId,
            @RequestHeader(value = "X-AI-Internal-Token", required = false) String token,
            @Valid @RequestBody AiTaskApiDto.Result request) {
        validateRequest(worklogId, token);
        return new AiTaskApiDto.ResultResponse(resultService.apply(worklogId, request));
    }

    /** 본문 역직렬화 전에 내부 토큰을 확인해 비인증 호출에 입력 검증 결과가 노출되지 않도록 한다. */
    @ModelAttribute
    void validateBeforeBody(@RequestHeader(value = "X-AI-Internal-Token", required = false) String token,
            @PathVariable Long worklogId) {
        validateRequest(worklogId, token);
    }

    /** 내부 호출도 설정 토큰과 양수 업무일지 ID가 일치할 때만 허용한다. */
    private void validateRequest(Long worklogId, String token) {
        String configured = properties.internalToken();
        if (configured == null || configured.isBlank() || token == null
                || !MessageDigest.isEqual(configured.getBytes(StandardCharsets.UTF_8), token.getBytes(StandardCharsets.UTF_8))) {
            throw new BusinessException(ErrorCode.AUTH_ACCESS_DENIED);
        }
        if (worklogId <= 0) {
            throw new BusinessException(ErrorCode.COMMON_INVALID_REQUEST);
        }
    }
}
