package com.ibank.axwms.domain.worklog.service;

import com.ibank.axwms.domain.worklog.dto.TriggerWorklogLightDeleteDto;
import com.ibank.axwms.domain.worklog.dto.TriggerWorklogLightIndexDto;
import com.ibank.axwms.domain.worklog.external.WorklogLightIndexClient;
import com.ibank.axwms.domain.worklog.repository.WorklogLightOutboxRepository;
import com.ibank.axwms.domain.worklog.repository.WorklogLightOutboxRepository.Claim;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.UUID;

import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;
import static org.mockito.Mockito.times;

class WorklogLightOutboxWorkerTest {

    private final WorklogLightOutboxRepository repository = mock(WorklogLightOutboxRepository.class);
    private final WorklogLightIndexClient client = mock(WorklogLightIndexClient.class);
    private final InternalWorklogLightIndexService indexService = mock(InternalWorklogLightIndexService.class);

    @Test
    void poll_recovers_once_and_continues_after_individual_failures() {
        Claim first = new Claim(501L, "INDEX", 1, UUID.randomUUID(), UUID.randomUUID());
        Claim second = new Claim(502L, "INDEX", 1, UUID.randomUUID(), UUID.randomUUID());
        when(repository.expiredClaims(20)).thenReturn(List.of(first, second));
        doThrow(new IllegalStateException("recovery failed")).when(indexService).reconcile(first);
        when(repository.claim(eq(null), any())).thenReturn(List.of(first), List.of(second), List.of());
        doThrow(new IllegalStateException("delivery failure handling failed")).when(indexService).deliveryFailed(eq(first), any());
        doThrow(new IllegalStateException("AI down")).when(client).requestIndex(TriggerWorklogLightIndexDto.Request.of(first.worklogId(), first.requestId()));

        new WorklogLightOutboxWorker(repository, client, indexService).poll();

        verify(repository, times(1)).expiredClaims(20);
        verify(indexService).reconcile(second);
        verify(repository).awaitResult(second);
    }

    @Test
    void index_waits_for_result_after_ai_acceptance() {
        Claim claim = new Claim(501L, "INDEX", 1, UUID.randomUUID(), new UUID(0, 1));
        when(repository.claim(eq(501L), any())).thenReturn(List.of(claim));

        new WorklogLightOutboxWorker(repository, client, indexService).processOne(501L);

        verify(client).requestIndex(TriggerWorklogLightIndexDto.Request.of(501L, new UUID(0, 1)));
        verify(repository).awaitResult(claim);
    }

    @Test
    void reindex_waits_for_result_after_ai_acceptance() {
        Claim claim = new Claim(501L, "REINDEX", 2, UUID.randomUUID(), new UUID(0, 1));
        when(repository.claim(eq(501L), any())).thenReturn(List.of(claim));

        new WorklogLightOutboxWorker(repository, client, indexService).processOne(501L);

        verify(client).requestReindex(TriggerWorklogLightIndexDto.Request.of(501L, new java.util.UUID(0, 1)));
        verify(repository).awaitResult(claim);
    }

    @Test
    void delete_takes_priority_and_is_retried_after_ai_failure() {
        Claim claim = new Claim(501L, "DELETE", 3, UUID.randomUUID(), new UUID(0, 1));
        when(repository.claim(eq(501L), any())).thenReturn(List.of(claim));
        doThrow(new IllegalStateException("AI down")).when(client)
                .requestDelete(TriggerWorklogLightDeleteDto.Request.of(501L, new java.util.UUID(0, 1)));

        new WorklogLightOutboxWorker(repository, client, indexService).processOne(501L);

        verify(indexService).deliveryFailed(eq(claim), any());
    }

    @Test
    void unknown_operation_is_failed_instead_of_dispatching_reindex() {
        Claim claim = new Claim(501L, "UNKNOWN", 1, UUID.randomUUID(), new UUID(0, 1));
        when(repository.claim(eq(501L), any())).thenReturn(List.of(claim));

        new WorklogLightOutboxWorker(repository, client, indexService).processOne(501L);

        verify(indexService).deliveryFailed(eq(claim), any());
        verify(repository, never()).awaitResult(claim);
        verify(client, never()).requestIndex(any());
        verify(client, never()).requestReindex(any());
        verify(client, never()).requestDelete(any());
    }
}
