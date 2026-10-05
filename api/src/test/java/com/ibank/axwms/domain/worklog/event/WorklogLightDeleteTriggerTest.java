package com.ibank.axwms.domain.worklog.event;

import com.ibank.axwms.domain.worklog.repository.WorklogLightOutboxRepository;
import org.junit.jupiter.api.Test;

import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;

class WorklogLightDeleteTriggerTest {

    @Test
    void enqueues_delete_in_worklog_transaction() {
        WorklogLightOutboxRepository repository = mock(WorklogLightOutboxRepository.class);
        WorklogLightDeleteTrigger trigger = new WorklogLightDeleteTrigger(repository);

        trigger.onCommit(new WorklogLightDeleteRequestedEvent(501L));

        verify(repository).enqueue(501L, "DELETE");
    }
}
