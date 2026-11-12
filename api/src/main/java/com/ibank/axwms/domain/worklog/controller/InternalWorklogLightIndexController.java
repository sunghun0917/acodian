package com.ibank.axwms.domain.worklog.controller;

import com.ibank.axwms.domain.worklog.dto.InternalWorklogLightIndexApiDto.*;
import com.ibank.axwms.domain.worklog.external.AiWorklogLightIndexProperties;
import com.ibank.axwms.domain.worklog.service.InternalWorklogLightIndexService;
import com.ibank.axwms.global.error.BusinessException;
import com.ibank.axwms.global.error.ErrorCode;
import com.ibank.axwms.global.response.EmptyResponse;
import jakarta.validation.Valid;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/internal/worklogs/{worklogId}/light-index")
@RequiredArgsConstructor
public class InternalWorklogLightIndexController implements InternalWorklogLightIndexControllerDocs {
    private final InternalWorklogLightIndexService service;
    private final AiWorklogLightIndexProperties properties;

    @Override
    @PostMapping("/start")
    public StartResponse start(@PathVariable Long worklogId,
            @RequestHeader(value = "X-AI-Internal-Token", required = false) String token,
            @Valid @RequestBody StartRequest request) {
        validateRequest(worklogId, token);
        return new StartResponse(service.start(worklogId, request.requestId(), request.executionId()));
    }

    @Override
    @PostMapping("/attempt-failed")
    public AttemptResponse attemptFailed(@PathVariable Long worklogId,
            @RequestHeader(value = "X-AI-Internal-Token", required = false) String token,
            @Valid @RequestBody StartRequest request) {
        validateRequest(worklogId, token);
        return new AttemptResponse(service.attemptFailed(worklogId, request.requestId(), request.executionId()));
    }

    @Override
    @PostMapping("/result")
    public EmptyResponse result(@PathVariable Long worklogId,
            @RequestHeader(value = "X-AI-Internal-Token", required = false) String token,
            @Valid @RequestBody ResultRequest request) {
        validateRequest(worklogId, token);
        service.result(worklogId, request.requestId(), request.executionId(), request.status(), request.error());
        return EmptyResponse.INSTANCE;
    }

    /** 입력 검증보다 토큰 검증을 먼저 적용해 비인증 호출에 본문 오류를 노출하지 않는다. */
    @ModelAttribute
    void validateBeforeBody(@RequestHeader(value = "X-AI-Internal-Token", required = false) String token,
            @PathVariable Long worklogId) {
        validateRequest(worklogId, token);
    }

    /** 내부 네트워크도 설정 토큰과 양수 업무 ID가 검증된 요청만 허용한다. */
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
