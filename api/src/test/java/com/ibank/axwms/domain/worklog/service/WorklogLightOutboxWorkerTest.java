package com.ibank.axwms.domain.worklog.service;

import com.ibank.axwms.domain.worklog.dto.TriggerWorklogLightDeleteDto;
import com.ibank.axwms.domain.worklog.dto.TriggerWorklogLightIndexDto;
import com.ibank.axwms.domain.worklog.external.AiWorklogLightIndexProperties;
import com.ibank.axwms.domain.worklog.external.WorklogLightIndexClient;
import com.ibank.axwms.domain.worklog.repository.WorklogLightOutboxRepository;
import com.ibank.axwms.domain.worklog.repository.WorklogLightOutboxRepository.Claim;
import org.junit.jupiter.api.Test;

import java.time.Duration;
import java.util.List;
import java.util.UUID;

import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

class WorklogLightOutboxWorkerTest {

    private final WorklogLightOutboxRepository repository = mock(WorklogLightOutboxRepository.class);
    private final WorklogLightIndexClient client = mock(WorklogLightIndexClient.class);
    private final AiWorklogLightIndexProperties properties = new AiWorklogLightIndexProperties(
            true, true, true, "http://ai.test/ai", Duration.ofSeconds(1), Duration.ofSeconds(2), "token");

    @Test
    void reindex_completes_claim_only_after_ai_success() {
        Claim claim = new Claim(501L, "REINDEX", 2, UUID.randomUUID());
        when(repository.claim(eq(501L), any())).thenReturn(List.of(claim));

        new WorklogLightOutboxWorker(repository, client, properties).processOne(501L);

        verify(client).requestReindex(TriggerWorklogLightIndexDto.Request.of(501L));
        verify(repository).complete(claim);
    }

    @Test
    void delete_takes_priority_and_is_retried_after_ai_failure() {
        Claim claim = new Claim(501L, "DELETE", 3, UUID.randomUUID());
        when(repository.claim(eq(501L), any())).thenReturn(List.of(claim));
        doThrow(new IllegalStateException("AI down")).when(client)
                .requestDelete(TriggerWorklogLightDeleteDto.Request.of(501L));

        new WorklogLightOutboxWorker(repository, client, properties).processOne(501L);

        verify(repository).fail(eq(claim), any());
    }
}
