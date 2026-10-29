package com.ibank.axwms.domain.worklog.event;

import com.ibank.axwms.domain.worklog.repository.WorklogAiPostProcessOutboxRepository;
import com.ibank.axwms.domain.worklog.repository.WorklogRepository;
import com.ibank.axwms.domain.worklog.repository.WorklogLightOutboxRepository;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Component;
import org.springframework.transaction.event.TransactionPhase;
import org.springframework.transaction.event.TransactionalEventListener;

/**
 * 원본과 등록 AI 후처리 의도를 같은 트랜잭션에 저장하여 커밋 직후 종료되어도 작업을 보존한다.
 */
@Component
@RequiredArgsConstructor
public class WorklogAiPostProcessTrigger {

    private final WorklogAiPostProcessOutboxRepository repository;
    private final WorklogRepository worklogRepository;
    private final WorklogLightOutboxRepository lightOutboxRepository;

    /** 업무 변경을 먼저 flush하여 완료 처리와 같은 잠금 순서를 지키고, 롤백 시 작업도 제거한다. */
    @TransactionalEventListener(phase = TransactionPhase.BEFORE_COMMIT)
    public void onCommit(WorklogAiPostProcessRequestedEvent event) {
        worklogRepository.flush();
        if (repository.enqueue(event)) {
            lightOutboxRepository.enqueue(event.worklogId(), "INDEX");
        }
    }
}
