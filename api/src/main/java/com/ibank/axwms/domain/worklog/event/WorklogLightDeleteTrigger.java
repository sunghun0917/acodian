package com.ibank.axwms.domain.worklog.event;

import com.ibank.axwms.domain.worklog.repository.WorklogLightOutboxRepository;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Component;
import org.springframework.transaction.event.TransactionPhase;
import org.springframework.transaction.event.TransactionalEventListener;

/** 업무 삭제 트랜잭션 안에서 AI 문서 삭제 의도를 영속화한다. */
@Component
@RequiredArgsConstructor
public class WorklogLightDeleteTrigger {

    private final WorklogLightOutboxRepository repository;

    /** 삭제 API가 허용된 요청만 이벤트를 발행하므로 삭제 의도를 반드시 저장한다. */
    @TransactionalEventListener(phase = TransactionPhase.BEFORE_COMMIT)
    public void onCommit(WorklogLightDeleteRequestedEvent event) {
        repository.enqueue(event.worklogId(), "DELETE");
    }
}
