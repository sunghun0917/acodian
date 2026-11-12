package com.ibank.axwms.domain.worklog.dto;

import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;
import java.util.UUID;
import lombok.AccessLevel;
import lombok.NoArgsConstructor;

@NoArgsConstructor(access = AccessLevel.PRIVATE)
public final class InternalWorklogLightIndexApiDto {
    /** 현재 요청의 실행 시작을 확인한다. */
    public record StartRequest(@NotNull UUID requestId, @NotNull UUID executionId) {}
    /** 해당 실행의 시작 허용 여부를 전달한다. */
    public record StartResponse(boolean execute) {}
    /** 실패 시도의 재시도 접수 여부를 전달한다. */
    public record AttemptResponse(boolean accepted) {}
    /** 현재 실행의 최종 색인 결과를 전달한다. */
    public record ResultRequest(@NotNull UUID requestId, @NotNull UUID executionId,
            @NotNull @Pattern(regexp = "COMPLETED|FAILED") String status,
            @Size(max = 1000) String error) {}
}
