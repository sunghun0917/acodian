package com.ibank.axwms.domain.worklog.dto;

import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;
import java.util.UUID;
import lombok.AccessLevel;
import lombok.NoArgsConstructor;

@NoArgsConstructor(access = AccessLevel.PRIVATE)
public final class InternalWorklogLightIndexApiDto {
    public record StartRequest(@NotNull UUID requestId, @NotNull UUID executionId) {}
    public record StartResponse(boolean execute) {}
    public record ResultRequest(@NotNull UUID requestId, @NotNull UUID executionId,
            @NotNull @Pattern(regexp = "COMPLETED|FAILED") String status,
            @Size(max = 1000) String error) {}
}
