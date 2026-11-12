package com.ibank.axwms.domain.worklog.dto;

import com.ibank.axwms.domain.worklog.dto.ApplyWorklogAiTagsApiDto.NewTag;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Positive;
import jakarta.validation.constraints.Size;
import java.util.List;
import java.util.UUID;
import lombok.AccessLevel;
import lombok.NoArgsConstructor;

/** 최신 요청과 해당 시도의 최초 결과만 내부 콜백에서 반영한다. */
@NoArgsConstructor(access = AccessLevel.PRIVATE)
public final class AiTaskApiDto {
    /** 최신 요청과 실행을 함께 식별한다. */
    public record Identity(
            @NotNull UUID requestId, @NotNull UUID executionId,
            @NotNull @Pattern(regexp = "[0-9a-f]{64}") String inputHash,
            @NotNull @Pattern(regexp = "SUMMARY|TAG|FILE_SUMMARY") String taskKind,
            @NotNull @Positive Long targetId) {}

    /** AI 작업 시작 보고에 대해 최신 요청이면 실행을 허용하는 응답이다. */
    public record StartResponse(boolean execute) {}
    /** 시도 실패 보고의 수락 여부와 자동 재시도 예약 여부를 담은 응답이다. */
    public record TransitionResponse(boolean accepted, boolean retryScheduled) {}
    /** 최종 결과 보고가 최신 요청·실행으로 수락됐는지 담은 응답이다. */
    public record ResultResponse(boolean accepted) {}

    /** 현재 실행의 재시도 가능 실패를 전달한다. */
    public record AttemptResult(
            @NotNull UUID requestId, @NotNull UUID executionId,
            @NotNull @Pattern(regexp = "[0-9a-f]{64}") String inputHash,
            @NotNull @Pattern(regexp = "SUMMARY|TAG|FILE_SUMMARY") String taskKind,
            @NotNull @Positive Long targetId, @NotNull Boolean retryable,
            @Size(max = 100) String errorCode) {
        /** 시작 확인과 실패 통보가 같은 요청·실행을 가리키도록 변환한다. */
        public Identity identity() {
            return new Identity(requestId, executionId, inputHash, taskKind, targetId);
        }
    }

    /** 현재 실행의 확정 결과와 작업별 산출물을 전달한다. */
    public record Result(
            @NotNull UUID requestId, @NotNull UUID executionId,
            @NotNull @Pattern(regexp = "[0-9a-f]{64}") String inputHash,
            @NotNull @Pattern(regexp = "SUMMARY|TAG|FILE_SUMMARY") String taskKind,
            @NotNull @Positive Long targetId,
            @NotNull @Pattern(regexp = "COMPLETED|FAILED") String status,
            @Size(max = 100) String errorCode, String aiSummary,
            @Size(max = 5) List<@NotNull @Positive Long> existingTagIds,
            @Size(max = 5) List<@NotNull @Valid NewTag> newTags) {
        /** 도메인 변경 전에 시작 확인과 동일한 최신성 검사를 적용한다. */
        public Identity identity() {
            return new Identity(requestId, executionId, inputHash, taskKind, targetId);
        }
    }
}
