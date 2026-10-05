package com.ibank.axwms.domain.worklog.event;

/** 업무일지 소프트 삭제 트랜잭션에 AI 삭제 의도를 기록할 식별자이다. */
public record WorklogLightDeleteRequestedEvent(Long worklogId) {
}
