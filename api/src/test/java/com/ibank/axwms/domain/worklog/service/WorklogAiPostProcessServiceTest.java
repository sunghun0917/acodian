package com.ibank.axwms.domain.worklog.service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.lenient;
import static org.mockito.Mockito.when;

import com.ibank.axwms.domain.file.dto.TriggerFileSummaryDto;
import com.ibank.axwms.domain.file.external.AiFileSummaryProperties;
import com.ibank.axwms.domain.file.external.FileSummaryClient;
import com.ibank.axwms.domain.file.service.FileService;
import com.ibank.axwms.domain.notification.service.NotificationService;
import com.ibank.axwms.domain.worklog.dto.TriggerWorklogLightIndexDto;
import com.ibank.axwms.domain.worklog.dto.TriggerWorklogPipelineDto;
import com.ibank.axwms.domain.worklog.event.WorklogAiPostProcessRequestedEvent;
import com.ibank.axwms.domain.worklog.external.AiWorklogLightIndexProperties;
import com.ibank.axwms.domain.worklog.external.AiWorklogPipelineProperties;
import com.ibank.axwms.domain.worklog.external.WorklogLightIndexClient;
import com.ibank.axwms.domain.worklog.external.WorklogPipelineClient;
import com.ibank.axwms.domain.worklog.repository.WorklogAiPostProcessOutboxRepository;
import com.ibank.axwms.domain.worklog.repository.WorklogAiPostProcessOutboxRepository.Claim;
import com.ibank.axwms.global.error.BusinessException;
import java.time.Duration;
import java.util.List;
import java.util.Optional;
import java.util.Set;
import java.util.UUID;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

@ExtendWith(MockitoExtension.class)
class WorklogAiPostProcessServiceTest {

    private static final String WORKLOG_TITLE = "월간 리스크 점검 보고";

    @Mock
    private FileSummaryClient fileSummaryClient;

    @Mock
    private FileService fileService;

    private final AiFileSummaryProperties fileSummaryProperties =
            new AiFileSummaryProperties(true, "http://ai.test/ai", Duration.ofSeconds(1), Duration.ofSeconds(2));

    @Mock
    private WorklogPipelineClient worklogPipelineClient;

    @Mock
    private WorklogLightIndexClient worklogLightIndexClient;

    @Mock
    private NotificationService notificationService;

    @Mock
    private WorklogAiProcessingStatusService worklogAiProcessingStatusService;

    @Mock
    private WorklogAiPostProcessOutboxRepository outboxRepository;

    private final AiWorklogPipelineProperties worklogPipelineProperties =
            new AiWorklogPipelineProperties(true, "http://ai.test/ai", Duration.ofSeconds(1), Duration.ofSeconds(2));

    private final AiWorklogLightIndexProperties worklogLightIndexProperties =
            new AiWorklogLightIndexProperties(true, false, false, "http://ai.test/ai", Duration.ofSeconds(1), Duration.ofSeconds(2), null);

    private WorklogAiPostProcessService service;

    @BeforeEach
    void setUp() {
        service = new WorklogAiPostProcessService(
                fileSummaryClient,
                fileService,
                fileSummaryProperties,
                worklogPipelineClient,
                worklogLightIndexClient,
                worklogPipelineProperties,
                worklogLightIndexProperties,
                notificationService,
                worklogAiProcessingStatusService,
                outboxRepository
        );
        lenient().when(outboxRepository.isWorklogActive(501L)).thenReturn(true);
        lenient().when(outboxRepository.renewLease(any())).thenReturn(true);
        lenient().when(outboxRepository.checkpoint(any(), any())).thenReturn(true);
        lenient().when(outboxRepository.lockWorklog(501L)).thenReturn(Optional.of(false));
        lenient().when(outboxRepository.lockClaim(any())).thenReturn(true);
        lenient().when(fileService.isActiveWorklogFile(501L, 9001L)).thenReturn(true);
    }

    @Test
    @DisplayName("세 AI 요청이 모두 dispatch 되면 완료 상태와 성공 알림을 생성한다")
    void process_creates_success_notification_when_all_requests_succeed() {
        WorklogAiPostProcessRequestedEvent event = eventWithFiles();

        processAndFinish(service, event);

        ArgumentCaptor<TriggerFileSummaryDto.Request> fileCaptor = ArgumentCaptor.forClass(TriggerFileSummaryDto.Request.class);
        verify(fileService).startWorklogFileAiSummaryProcessing(9001L);
        ArgumentCaptor<TriggerWorklogPipelineDto.Request> pipelineCaptor = ArgumentCaptor.forClass(TriggerWorklogPipelineDto.Request.class);
        ArgumentCaptor<TriggerWorklogLightIndexDto.Request> indexCaptor = ArgumentCaptor.forClass(TriggerWorklogLightIndexDto.Request.class);

        verify(fileSummaryClient).requestSummary(fileCaptor.capture());
        verify(worklogPipelineClient).requestPipeline(pipelineCaptor.capture());
        verify(worklogLightIndexClient).requestIndex(indexCaptor.capture());

        assertThat(fileCaptor.getValue().fileId()).isEqualTo(9001L);
        assertThat(fileCaptor.getValue().worklogId()).isEqualTo(501L);
        assertThat(pipelineCaptor.getValue().worklogId()).isEqualTo(501L);
        assertThat(pipelineCaptor.getValue().departmentId()).isEqualTo(9L);
        assertThat(indexCaptor.getValue().worklogIds()).containsExactly(501L);

        verify(worklogAiProcessingStatusService).completeAiProcessing(501L);
        verify(worklogAiProcessingStatusService, never()).startAiProcessing(any());
        verify(worklogAiProcessingStatusService, never()).failAiProcessing(any());
        verify(notificationService).createWorklogAiPostProcessResultNotification(
                101L,
                9L,
                21L,
                501L,
                WORKLOG_TITLE,
                true,
                List.of()
        );
    }

    @Test
    @DisplayName("하나라도 실패하면 실패 단계가 포함된 실패 알림을 생성한다")
    void process_creates_failure_notification_when_any_request_fails() {
        WorklogAiPostProcessRequestedEvent event = eventWithFiles();
        doThrow(new IllegalStateException("AI down")).when(worklogPipelineClient).requestPipeline(any());

        processAndFinish(service, event);

        @SuppressWarnings("unchecked")
        ArgumentCaptor<List<String>> failedStagesCaptor = ArgumentCaptor.forClass(List.class);
        verify(worklogAiProcessingStatusService).failAiProcessing(501L);
        verify(worklogAiProcessingStatusService, never()).startAiProcessing(any());
        verify(worklogAiProcessingStatusService, never()).completeAiProcessing(any());
        verify(notificationService).createWorklogAiPostProcessResultNotification(
                eq(101L),
                eq(9L),
                eq(21L),
                eq(501L),
                eq(WORKLOG_TITLE),
                eq(false),
                failedStagesCaptor.capture()
        );
        assertThat(failedStagesCaptor.getValue()).containsExactly("WORKLOG_PIPELINE");
        verify(worklogLightIndexClient).requestIndex(any());
        verify(outboxRepository).fail(any(), eq("WORKLOG_PIPELINE"));
        verify(outboxRepository, never()).complete(any());
    }

    @Test
    @DisplayName("파일이 없으면 파일 요약 호출 없이 완료 상태와 성공 알림을 생성한다")
    void process_skips_file_summary_when_no_files() {
        WorklogAiPostProcessRequestedEvent event = eventWithoutFiles();

        processAndFinish(service, event);

        verify(fileSummaryClient, never()).requestSummary(any());
        verify(worklogPipelineClient).requestPipeline(any());
        verify(worklogLightIndexClient).requestIndex(any());
        verify(worklogAiProcessingStatusService).completeAiProcessing(501L);
        verify(worklogAiProcessingStatusService, never()).startAiProcessing(any());
        verify(worklogAiProcessingStatusService, never()).failAiProcessing(any());
        verify(notificationService).createWorklogAiPostProcessResultNotification(
                101L,
                9L,
                21L,
                501L,
                WORKLOG_TITLE,
                true,
                List.of()
        );
    }


    @Test
    @DisplayName("파일 요약이 비활성화되어 있으면 파일 요약 호출만 건너뛰고 완료 상태로 처리한다")
    void process_skips_file_summary_when_file_summary_disabled() {
        WorklogAiPostProcessService disabledService = new WorklogAiPostProcessService(
                fileSummaryClient,
                fileService,
                new AiFileSummaryProperties(false, "http://ai.test/ai", Duration.ofSeconds(1), Duration.ofSeconds(2)),
                worklogPipelineClient,
                worklogLightIndexClient,
                worklogPipelineProperties,
                worklogLightIndexProperties,
                notificationService,
                worklogAiProcessingStatusService,
                outboxRepository
        );

        processAndFinish(disabledService, eventWithFiles());

        verify(fileService, never()).startWorklogFileAiSummaryProcessing(any());
        verify(fileSummaryClient, never()).requestSummary(any());
        verify(worklogAiProcessingStatusService).completeAiProcessing(501L);
        verify(worklogAiProcessingStatusService, never()).startAiProcessing(any());
        verify(worklogAiProcessingStatusService, never()).failAiProcessing(any());
        verify(notificationService).createWorklogAiPostProcessResultNotification(
                101L,
                9L,
                21L,
                501L,
                WORKLOG_TITLE,
                true,
                List.of()
        );
    }

    @Test
    @DisplayName("파일과 파이프라인이 이미 접수됐으면 실패한 색인만 재시도한다")
    void process_resumes_only_unfinished_stages() {
        Claim claim = new Claim(501L, eventWithFiles(), Set.of("FILE_SUMMARY(9001)", "WORKLOG_PIPELINE"), 1, UUID.randomUUID());

        assertThat(service.process(claim)).isEmpty();

        verify(fileSummaryClient, never()).requestSummary(any());
        verify(worklogPipelineClient, never()).requestPipeline(any());
        verify(worklogLightIndexClient).requestIndex(any());
        verify(outboxRepository).checkpoint(claim, "LIGHT_INDEX");
        verify(worklogAiProcessingStatusService, never()).completeAiProcessing(any());
    }

    @Test
    @DisplayName("선점 소유권을 잃으면 남은 외부 요청을 중단한다")
    void process_stops_dispatching_after_claim_loss() {
        when(outboxRepository.checkpoint(any(), any())).thenReturn(false);

        assertThatThrownBy(() -> service.process(claim(eventWithFiles())))
                .isInstanceOf(BusinessException.class);

        verify(fileSummaryClient).requestSummary(any());
        verify(worklogPipelineClient, never()).requestPipeline(any());
        verify(worklogLightIndexClient, never()).requestIndex(any());
    }

    @Test
    @DisplayName("삭제된 업무이면 AI 요청 없이 등록 후처리를 취소한다")
    void deleted_worklog_is_cancelled_without_dispatch_or_notification() {
        when(outboxRepository.isWorklogActive(501L)).thenReturn(false);
        when(outboxRepository.lockWorklog(501L)).thenReturn(Optional.of(true));

        processAndFinish(service, eventWithFiles());

        verify(fileSummaryClient, never()).requestSummary(any());
        verify(worklogPipelineClient, never()).requestPipeline(any());
        verify(worklogLightIndexClient, never()).requestIndex(any());
        verify(outboxRepository).complete(any());
        verify(worklogAiProcessingStatusService, never()).completeAiProcessing(any());
        verify(notificationService, never()).createWorklogAiPostProcessResultNotification(
                any(), any(), any(), any(), any(), eq(true), any());
    }

    @Test
    @DisplayName("첨부 파일이 삭제됐으면 해당 파일만 건너뛰고 나머지 요청을 실행한다")
    void removed_attachment_is_skipped_without_blocking_other_stages() {
        when(fileService.isActiveWorklogFile(501L, 9001L)).thenReturn(false);

        assertThat(service.process(claim(eventWithFiles()))).isEmpty();

        verify(fileSummaryClient, never()).requestSummary(any());
        verify(outboxRepository).checkpoint(any(), eq("FILE_SUMMARY(9001)"));
        verify(worklogPipelineClient).requestPipeline(any());
        verify(worklogLightIndexClient).requestIndex(any());
    }

    @Test
    @DisplayName("오래된 claim이면 업무 상태와 결과 알림을 갱신하지 않는다")
    void finish_ignores_stale_claim() {
        when(outboxRepository.lockClaim(any())).thenReturn(false);

        service.finish(claim(eventWithFiles()), List.of());

        verify(outboxRepository, never()).complete(any());
        verify(worklogAiProcessingStatusService, never()).completeAiProcessing(any());
        verify(notificationService, never()).createWorklogAiPostProcessResultNotification(
                any(), any(), any(), any(), any(), eq(true), any());
    }

    /** 실제 워커와 동일하게 외부 실행과 짧은 완료 트랜잭션 경계를 나눈다. */
    private static void processAndFinish(WorklogAiPostProcessService service, WorklogAiPostProcessRequestedEvent event) {
        Claim claim = claim(event);
        service.finish(claim, service.process(claim));
    }

    /** 새 작업은 아직 성공한 단계가 없는 불변 선점 snapshot으로 시작한다. */
    private static Claim claim(WorklogAiPostProcessRequestedEvent event) {
        return new Claim(event.worklogId(), event, Set.of(), 0, UUID.randomUUID());
    }

    /** 등록 원문과 첨부 파일이 재시도에도 보존되는 입력이다. */
    private static WorklogAiPostProcessRequestedEvent eventWithFiles() {
        return new WorklogAiPostProcessRequestedEvent(
                501L,
                WORKLOG_TITLE,
                "요청 내용",
                "업무 내용",
                101L,
                21L,
                9L,
                List.of(new WorklogAiPostProcessRequestedEvent.FileSummaryTarget(
                        9001L,
                        "worklog/501/report.txt",
                        "report.txt",
                        "txt"
                ))
        );
    }

    /** 파일 없는 등록도 본문 파이프라인과 색인을 요청한다. */
    private static WorklogAiPostProcessRequestedEvent eventWithoutFiles() {
        return new WorklogAiPostProcessRequestedEvent(
                501L,
                WORKLOG_TITLE,
                "요청 내용",
                "업무 내용",
                101L,
                21L,
                9L,
                List.of()
        );
    }
}
