package com.ibank.axwms.domain.worklog.event;

import com.ibank.axwms.domain.worklog.external.AiWorklogLightIndexProperties;
import com.ibank.axwms.domain.worklog.repository.WorklogLightOutboxRepository;
import org.junit.jupiter.api.Test;

import java.time.Duration;

import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;

class WorklogLightIndexTriggerTest {

    @Test
    void enqueues_reindex_when_enabled() {
        WorklogLightOutboxRepository repository = mock(WorklogLightOutboxRepository.class);
        WorklogLightIndexTrigger trigger = new WorklogLightIndexTrigger(repository,
                new AiWorklogLightIndexProperties(true, true, false, "http://ai.test/ai",
                        Duration.ofSeconds(1), Duration.ofSeconds(2), "token"));

        trigger.onCommit(new WorklogLightIndexRequestedEvent(501L));

        verify(repository).enqueue(501L, "REINDEX");
    }

    @Test
    void does_not_enqueue_reindex_before_gate_enabled() {
        WorklogLightOutboxRepository repository = mock(WorklogLightOutboxRepository.class);
        WorklogLightIndexTrigger trigger = new WorklogLightIndexTrigger(repository,
                new AiWorklogLightIndexProperties(true, false, false, "http://ai.test/ai",
                        Duration.ofSeconds(1), Duration.ofSeconds(2), "token"));

        trigger.onCommit(new WorklogLightIndexRequestedEvent(501L));

        verify(repository, never()).enqueue(501L, "REINDEX");
    }
}
