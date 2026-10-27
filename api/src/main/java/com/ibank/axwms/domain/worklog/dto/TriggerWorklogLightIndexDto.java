package com.ibank.axwms.domain.worklog.dto;

import lombok.AccessLevel;
import lombok.NoArgsConstructor;
import java.util.List;
import java.util.UUID;

@NoArgsConstructor(access = AccessLevel.PRIVATE)
public final class TriggerWorklogLightIndexDto {
    /** 전달 재시도에서도 동일 요청 식별자를 보존하는 일괄 접수 계약이다. */
    public record Request(List<Long> worklogIds, UUID requestId) {
        /** 단건 의도를 AI 일괄 접수 계약에 맞춘다. */
        public static Request of(Long worklogId, UUID requestId) {
            return new Request(List.of(worklogId), requestId);
        }
    }
    /** 브로커 발행 식별자만 반환하며 실행 성공을 뜻하지 않는다. */
    public record Response(List<Item> items) {}
    public record Item(Long worklogId, UUID requestId, String taskId) {}
}
