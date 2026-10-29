package com.ibank.axwms.domain.worklog.event;

import com.ibank.axwms.domain.worklog.repository.WorklogAiPostProcessOutboxRepository;
import com.ibank.axwms.domain.worklog.repository.WorklogLightOutboxRepository;
import com.ibank.axwms.domain.worklog.repository.WorklogRepository;
import java.util.List;
import org.junit.jupiter.api.Test;

import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

class WorklogAiPostProcessTriggerTest {
    private final WorklogAiPostProcessOutboxRepository general = mock(WorklogAiPostProcessOutboxRepository.class);
    private final WorklogRepository worklogs = mock(WorklogRepository.class);
    private final WorklogLightOutboxRepository light = mock(WorklogLightOutboxRepository.class);
    private final WorklogAiPostProcessTrigger trigger = new WorklogAiPostProcessTrigger(general, worklogs, light);

    @Test
    void active_registration_enqueues_initial_index_in_light_outbox() {
        var event = event();
        when(general.enqueue(event)).thenReturn(true);

        trigger.onCommit(event);

        verify(worklogs).flush();
        verify(light).enqueue(501L, "INDEX");
    }

    @Test
    void queued_retry_does_not_replace_current_index_before_activation() {
        var event = event();
        when(general.enqueue(event)).thenReturn(false);

        trigger.onCommit(event);

        verify(light, never()).enqueue(501L, "INDEX");
    }

    /** 일반 snapshot과 최초 색인이 같은 업무를 참조하는 요청이다. */
    private static WorklogAiPostProcessRequestedEvent event() {
        return new WorklogAiPostProcessRequestedEvent(501L, "업무", "요청", "본문", 101L, 21L, 9L, List.of());
    }
}
