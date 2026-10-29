package com.ibank.axwms.domain.worklog.event;

import com.ibank.axwms.domain.worklog.repository.WorklogLightOutboxRepository;
import org.junit.jupiter.api.Test;

import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;

class WorklogLightIndexTriggerTest {

    @Test
    void enqueues_reindex_without_feature_flag() {
        WorklogLightOutboxRepository repository = mock(WorklogLightOutboxRepository.class);
        WorklogLightIndexTrigger trigger = new WorklogLightIndexTrigger(repository);

        trigger.onCommit(new WorklogLightIndexRequestedEvent(501L));

        verify(repository).enqueue(501L, "REINDEX");
    }

}
