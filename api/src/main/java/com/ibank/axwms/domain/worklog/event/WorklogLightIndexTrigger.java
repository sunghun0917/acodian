package com.ibank.axwms.domain.worklog.event;

import com.ibank.axwms.domain.worklog.external.AiWorklogLightIndexProperties;
import com.ibank.axwms.domain.worklog.repository.WorklogLightOutboxRepository;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Component;
import org.springframework.transaction.event.TransactionPhase;
import org.springframework.transaction.event.TransactionalEventListener;

/** 업무 수정 트랜잭션 안에서 재색인 의도를 영속화한다. */
@Component
@RequiredArgsConstructor
public class WorklogLightIndexTrigger {

    private final WorklogLightOutboxRepository repository;
    private final AiWorklogLightIndexProperties properties;

    /** 롤백되면 작업도 롤백되도록 BEFORE_COMMIT 에 기록한다. */
    @TransactionalEventListener(phase = TransactionPhase.BEFORE_COMMIT)
    public void onCommit(WorklogLightIndexRequestedEvent event) {
        if (properties.enabled() && properties.updateEnabled()) {
            repository.enqueue(event.worklogId(), "REINDEX");
        }
    }
}
