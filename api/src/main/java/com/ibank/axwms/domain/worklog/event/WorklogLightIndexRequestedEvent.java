package com.ibank.axwms.domain.worklog.event;

/**
 * 업무일지 수정 트랜잭션에 재색인 의도를 기록할 업무 ID를 전달한다.
 */
public record WorklogLightIndexRequestedEvent(
        Long worklogId
) {
}
