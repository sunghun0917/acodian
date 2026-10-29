package com.ibank.axwms.domain.worklog.service;

import com.ibank.axwms.domain.file.dto.TriggerFileSummaryDto;
import com.ibank.axwms.domain.file.external.AiFileSummaryProperties;
import com.ibank.axwms.domain.file.external.FileSummaryClient;
import com.ibank.axwms.domain.file.service.FileService;
import com.ibank.axwms.domain.notification.service.NotificationService;
import com.ibank.axwms.domain.worklog.dto.TriggerWorklogPipelineDto;
import com.ibank.axwms.domain.worklog.event.WorklogAiPostProcessRequestedEvent;
import com.ibank.axwms.domain.worklog.external.AiWorklogPipelineProperties;
import com.ibank.axwms.domain.worklog.external.WorklogPipelineClient;
import com.ibank.axwms.domain.worklog.repository.WorklogAiPostProcessOutboxRepository;
import com.ibank.axwms.domain.worklog.repository.WorklogAiPostProcessOutboxRepository.Claim;
import com.ibank.axwms.domain.worklog.repository.WorklogLightOutboxRepository;
import com.ibank.axwms.global.error.BusinessException;
import com.ibank.axwms.global.error.ErrorCode;
import java.util.ArrayList;
import java.util.List;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Propagation;
import org.springframework.transaction.annotation.Transactional;

/**
 * 일반 AI 요청과 최신 색인 결과를 하나의 등록 결과 알림으로 집계한다.
 */
@Slf4j
@Service
@RequiredArgsConstructor
@Transactional(readOnly = true)
public class WorklogAiPostProcessService {

    private static final String FILE_SUMMARY_STAGE = "FILE_SUMMARY";
    private static final String WORKLOG_PIPELINE_STAGE = "WORKLOG_PIPELINE";
    private static final String LIGHT_INDEX_STAGE = "LIGHT_INDEX";

    private final FileSummaryClient fileSummaryClient;
    private final FileService fileService;
    private final AiFileSummaryProperties fileSummaryProperties;
    private final WorklogPipelineClient worklogPipelineClient;
    private final AiWorklogPipelineProperties worklogPipelineProperties;
    private final NotificationService notificationService;
    private final WorklogAiProcessingStatusService worklogAiProcessingStatusService;
    private final WorklogAiPostProcessOutboxRepository outboxRepository;
    private final WorklogLightOutboxRepository lightOutboxRepository;

    /**
     * DB 연결을 점유하지 않고 일반 AI의 미접수 단계만 실행한다.
     *
     * @param claim 등록 snapshot과 이전 성공 단계를 가진 현재 선점
     * @return 단계별 접수 실패. 원격 접수 직후 장애는 중복 요청될 수 있다.
     */
    @Transactional(propagation = Propagation.NOT_SUPPORTED)
    public ProcessResult process(Claim claim) {
        if (!outboxRepository.isWorklogActive(claim.worklogId())) {
            return new ProcessResult(List.of());
        }
        List<String> failedStages = new ArrayList<>();
        requestFileSummaries(claim, failedStages);
        requestWorklogPipeline(claim, failedStages);
        return new ProcessResult(List.copyOf(failedStages));
    }

    /** 상태·업무당 최초 결과 알림·Outbox 갱신을 함께 커밋하며 재선점된 작업에는 관여하지 않는다. */
    @Transactional
    public void finish(Claim claim, ProcessResult result) {
        boolean active = outboxRepository.lockWorklog(claim.worklogId()).map(deleted -> !deleted).orElse(false);
        if (!outboxRepository.lockClaim(claim)) {
            return;
        }
        if (!active) {
            outboxRepository.complete(claim);
            return;
        }
        if (outboxRepository.activateQueuedRetry(claim)) {
            lightOutboxRepository.enqueue(claim.worklogId(), "INDEX");
            return;
        }
        WorklogAiPostProcessRequestedEvent event = claim.event();
        List<String> failedStages = new ArrayList<>(outboxRepository.pendingFailedStages(claim));
        for (String stage : result.failedStages()) {
            if (!failedStages.contains(stage)) {
                failedStages.add(stage);
            }
        }
        boolean terminalLightFailure = false;
        String lightState = outboxRepository.lightState(claim);
        if ("PENDING".equals(lightState) || "RUNNING".equals(lightState)) {
            outboxRepository.awaitLightResult(claim, failedStages);
            return;
        } else if ("FAILED".equals(lightState)) {
            failedStages.add(LIGHT_INDEX_STAGE);
            terminalLightFailure = lightOutboxRepository.isTerminalFailure(claim.worklogId());
        }
        boolean success = failedStages.isEmpty();
        if (success) {
            worklogAiProcessingStatusService.completeAiProcessing(event.worklogId());
        } else {
            worklogAiProcessingStatusService.failAiProcessing(event.worklogId());
        }
        notificationService.createWorklogAiPostProcessResultNotification(
                event.authorId(), event.departmentId(), event.teamId(), event.worklogId(),
                event.worklogTitle(), success, failedStages);
        if (!success) {
            outboxRepository.clearPendingFailedStages(claim);
        }
        if (success) {
            outboxRepository.complete(claim);
        } else if (terminalLightFailure) {
            outboxRepository.suspendAfterLightFailure(claim, String.join(",", failedStages));
        } else {
            outboxRepository.fail(claim, String.join(",", failedStages));
        }
    }

    /**
     * 파일별로 성공을 남겨 다른 파일 실패 시 재접수를 피하고, 이미 제거된 첨부 파일은 건너뛴다.
     */
    private void requestFileSummaries(Claim claim, List<String> failedStages) {
        WorklogAiPostProcessRequestedEvent event = claim.event();
        for (WorklogAiPostProcessRequestedEvent.FileSummaryTarget file : event.files()) {
            executeStage(claim, FILE_SUMMARY_STAGE + "(" + file.fileId() + ")", () -> {
                if (!fileSummaryProperties.enabled() || !fileService.isActiveWorklogFile(event.worklogId(), file.fileId())) {
                    return;
                }
                fileService.startWorklogFileAiSummaryProcessing(file.fileId());
                fileSummaryClient.requestSummary(new TriggerFileSummaryDto.Request(
                        file.fileId(),
                        event.worklogId(),
                        file.storageKey(),
                        file.originalName(),
                        file.fileExtension()
                ));
            }, failedStages);
        }
    }

    /**
     * 비활성화된 업무 본문 파이프라인은 운영 설정에 따른 정상 skip 으로 보고 실패 집계에서 제외한다.
     */
    private void requestWorklogPipeline(Claim claim, List<String> failedStages) {
        WorklogAiPostProcessRequestedEvent event = claim.event();
        executeStage(claim, WORKLOG_PIPELINE_STAGE, () -> {
            if (!worklogPipelineProperties.enabled()) {
                return;
            }
            worklogPipelineClient.requestPipeline(new TriggerWorklogPipelineDto.Request(
                    event.worklogId(),
                    event.requestContent(),
                    event.workContent(),
                    event.authorId(),
                    event.teamId(),
                    event.departmentId()
            ));
        }, failedStages);
    }

    /** 요청 실패만 집계하고, 소유권이나 checkpoint 유실은 즉시 전파해 남은 외부 호출을 중단한다. */
    private void executeStage(Claim claim, String stage, Runnable request, List<String> failedStages) {
        if (claim.completedStages().contains(stage)) {
            return;
        }
        if (!outboxRepository.renewLease(claim)) {
            throw new BusinessException(ErrorCode.COMMON_INTERNAL_SERVER_ERROR);
        }
        try {
            request.run();
        } catch (RuntimeException e) {
            failedStages.add(stage);
            log.warn("AI 등록 후처리 요청 실패 worklogId={} stage={}", claim.worklogId(), stage, e);
            return;
        }
        // shortcut: 원격 성공 직후 종료되면 재전송될 수 있다. 정확히 한 번 실행이 필요하면 AI 멱등 키를 도입한다.
        if (!outboxRepository.checkpoint(claim, stage)) {
            throw new BusinessException(ErrorCode.COMMON_INTERNAL_SERVER_ERROR);
        }
    }

    /** 일반 AI 단계의 접수 실패만 완료 트랜잭션에 전달한다. */
    public record ProcessResult(List<String> failedStages) {
    }

}
