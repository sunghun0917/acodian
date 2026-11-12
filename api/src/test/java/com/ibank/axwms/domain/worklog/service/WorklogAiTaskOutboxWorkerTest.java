package com.ibank.axwms.domain.worklog.service;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.ibank.axwms.domain.file.dto.TriggerFileSummaryDto;
import com.ibank.axwms.domain.file.external.FileSummaryClient;
import com.ibank.axwms.domain.file.external.AiFileSummaryProperties;
import com.ibank.axwms.domain.worklog.dto.TriggerWorklogPipelineDto;
import com.ibank.axwms.domain.worklog.event.WorklogAiPostProcessRequestedEvent;
import com.ibank.axwms.domain.worklog.external.WorklogPipelineClient;
import com.ibank.axwms.domain.worklog.external.AiWorklogPipelineProperties;
import com.ibank.axwms.domain.worklog.repository.WorklogAiTaskRepository;
import com.ibank.axwms.domain.worklog.repository.WorklogAiTaskRepository.DeliveryClaim;
import java.util.List;
import java.util.Optional;
import java.util.UUID;
import java.time.Duration;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import org.springframework.scheduling.annotation.Scheduled;

class WorklogAiTaskOutboxWorkerTest {
    private final WorklogAiTaskRepository tasks = mock(WorklogAiTaskRepository.class);
    private final WorklogPipelineClient pipeline = mock(WorklogPipelineClient.class);
    private final FileSummaryClient files = mock(FileSummaryClient.class);
    private final WorklogAiTaskDeliveryOutcomeService outcomes = mock(WorklogAiTaskDeliveryOutcomeService.class);
    private final WorklogAiTaskRecoveryService recovery = mock(WorklogAiTaskRecoveryService.class);
    private final WorklogAiTaskOutboxWorker worker = new WorklogAiTaskOutboxWorker(tasks, pipeline, files, outcomes, recovery,
            new AiWorklogPipelineProperties(true, null, Duration.ofSeconds(1), Duration.ofSeconds(1)),
            new AiFileSummaryProperties(true, null, Duration.ofSeconds(1), Duration.ofSeconds(1)));

    @Test
    void summaryDispatchUsesImmutableSnapshotAndClaimIdentity() throws Exception {
        var claim = claim("SUMMARY", 10L);
        when(tasks.claimDelivery(isNull(), any(), eq(true), eq(true))).thenReturn(Optional.of(claim));
        assertThat(worker.processOne()).isTrue();
        var captor = ArgumentCaptor.forClass(TriggerWorklogPipelineDto.Request.class);
        verify(pipeline).requestPipeline(captor.capture());
        assertThat(captor.getValue().requestId()).isEqualTo(claim.requestId());
        assertThat(captor.getValue().taskKind()).isEqualTo("SUMMARY");
        assertThat(captor.getValue().workContent()).isEqualTo("본문");
        verify(tasks).deliveryAccepted(claim);
        verifyNoInteractions(files);
    }

    @Test
    void fileDispatchUsesOnlySelectedFileAndRetainsIdentity() throws Exception {
        var claim = claim("FILE_SUMMARY", 20L);
        when(tasks.claimDelivery(isNull(), any(), eq(true), eq(true))).thenReturn(Optional.of(claim));
        assertThat(worker.processOne()).isTrue();
        var captor = ArgumentCaptor.forClass(TriggerFileSummaryDto.Request.class);
        verify(files).requestSummary(captor.capture());
        assertThat(captor.getValue().fileId()).isEqualTo(20L);
        assertThat(captor.getValue().requestId()).isEqualTo(claim.requestId());
        assertThat(captor.getValue().storageKey()).isEqualTo("key");
        verify(tasks).deliveryAccepted(claim);
        verifyNoInteractions(pipeline);
    }

    @Test
    void deliveryFailureReleasesClaimWithoutReportingSuccess() throws Exception {
        var claim = claim("TAG", 10L);
        when(tasks.claimDelivery(isNull(), any(), eq(true), eq(true))).thenReturn(Optional.of(claim));
        doThrow(new IllegalStateException("remote failed")).when(pipeline).requestPipeline(any());
        assertThat(worker.processOne()).isTrue();
        verify(outcomes).fail(eq(claim), anyString());
        verify(tasks, never()).deliveryAccepted(any());
    }



    @Test
    void scheduledPollRecoversAndDispatchesDueTasks() throws Exception {
        assertThat(WorklogAiTaskOutboxWorker.class.getMethod("poll").getAnnotation(Scheduled.class)).isNotNull();
        var claim = claim("SUMMARY", 10L);
        when(tasks.claimDelivery(isNull(), any(), eq(true), eq(true))).thenReturn(Optional.of(claim), Optional.empty());
        worker.poll();
        verify(tasks).deliveryAccepted(claim);
        verify(tasks, times(2)).claimDelivery(isNull(), any(), eq(true), eq(true));
    }

    @Test
    void disabledFileSummaryIsNotClaimed() {
        var configured = new WorklogAiTaskOutboxWorker(tasks, pipeline, files, outcomes, recovery,
                new AiWorklogPipelineProperties(true, null, null, null),
                new AiFileSummaryProperties(false, null, null, null));
        when(tasks.claimDelivery(isNull(), any(), eq(true), eq(false))).thenReturn(Optional.empty());
        assertThat(configured.processOne()).isFalse();
        verify(tasks).claimDelivery(isNull(), any(), eq(true), eq(false));
        verifyNoInteractions(files, pipeline);
    }

    /** 요청을 실제 저장 형식으로 직렬화해 worker 복원 계약을 검증한다. */
    private DeliveryClaim claim(String kind, Long target) throws Exception {
        var event = new WorklogAiPostProcessRequestedEvent(10L, "제목", "요청", "본문", 1L, 2L, 3L,
                List.of(new WorklogAiPostProcessRequestedEvent.FileSummaryTarget(20L, "key", "x.pdf", "pdf")));
        return new DeliveryClaim(10L, kind, target, UUID.randomUUID(), 1, "a".repeat(64), new ObjectMapper().writeValueAsString(event), UUID.randomUUID());
    }
}
