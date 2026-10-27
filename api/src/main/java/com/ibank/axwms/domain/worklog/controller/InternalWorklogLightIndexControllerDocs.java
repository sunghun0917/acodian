package com.ibank.axwms.domain.worklog.controller;

import com.ibank.axwms.domain.worklog.dto.InternalWorklogLightIndexApiDto.*;
import com.ibank.axwms.global.response.EmptyResponse;
import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.tags.Tag;

@Tag(name = "InternalWorklogLightIndex", description = "내부 토큰으로 보호된 비동기 색인 실행 콜백")
public interface InternalWorklogLightIndexControllerDocs {
    @Operation(summary = "현재 요청의 실행 시작 등록", description = "execute=false면 원본 조회 및 색인 쓰기를 생략한다.")
    StartResponse start(Long worklogId, String token, StartRequest request);
    @Operation(summary = "현재 실행 결과 반영", description = "과거 요청·실행과 중복 최종 결과는 상태를 변경하지 않는다.")
    EmptyResponse result(Long worklogId, String token, ResultRequest request);
}
