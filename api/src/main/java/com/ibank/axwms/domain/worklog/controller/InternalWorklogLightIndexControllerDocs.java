package com.ibank.axwms.domain.worklog.controller;

import com.ibank.axwms.domain.worklog.dto.InternalWorklogLightIndexApiDto.*;
import com.ibank.axwms.global.response.EmptyResponse;
import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.tags.Tag;

@Tag(name = "InternalWorklogLightIndex", description = "내부 토큰으로 보호된 비동기 색인 실행 콜백")
public interface InternalWorklogLightIndexControllerDocs {
    @Operation(summary = "색인 실행 시작", description = "최신 요청만 RUNNING으로 표시하며 과거 요청은 실행하지 않는다.")
    StartResponse start(Long worklogId, String token, StartRequest request);
    @Operation(summary = "현재 색인 실행의 일시 실패를 기록", description = "최신 실행만 Outbox의 남은 시도 횟수 안에서 backoff 후 재개한다.")
    AttemptResponse attemptFailed(Long worklogId, String token, StartRequest request);
    @Operation(summary = "현재 실행 결과 반영", description = "과거 요청·실행과 중복 최종 결과는 상태를 변경하지 않는다.")
    EmptyResponse result(Long worklogId, String token, ResultRequest request);
}
