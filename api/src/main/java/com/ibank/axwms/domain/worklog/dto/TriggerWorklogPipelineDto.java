package com.ibank.axwms.domain.worklog.dto;

import lombok.AccessLevel;
import lombok.NoArgsConstructor;
import java.util.UUID;

@NoArgsConstructor(access = AccessLevel.PRIVATE)
public final class TriggerWorklogPipelineDto {

    /**
     * AI 서버가 콜백으로 다시 식별할 수 있도록 업무 식별자와 본문 생성에 필요한 최소 문맥만 전달한다.
     */
    public record Request(
            Long worklogId,
            String requestContent,
            String workContent,
            Long authorId,
            Long teamId,
            Long departmentId,
            String taskKind,
            UUID requestId,
            String inputHash
    ) {
        /** 작업 종류가 없는 기존 등록 요청은 요약과 태그를 함께 접수한다. */
        public Request(Long worklogId, String requestContent, String workContent,
                       Long authorId, Long teamId, Long departmentId) {
            this(worklogId, requestContent, workContent, authorId, teamId, departmentId, null, null, null);
        }

        /** 작업별 전환 전 요청은 기존 호출 계약을 유지한다. */
        public Request(Long worklogId, String requestContent, String workContent,
                       Long authorId, Long teamId, Long departmentId, String taskKind) {
            this(worklogId, requestContent, workContent, authorId, teamId, departmentId, taskKind, null, null);
        }
    }

    /** 비동기 접수와 실제 작업 완료를 구분하는 AI 응답이다. */
    public record Response(Long worklogId, UUID requestId, String summaryTaskId,
                           String taggingTaskId, String status) {}
}
