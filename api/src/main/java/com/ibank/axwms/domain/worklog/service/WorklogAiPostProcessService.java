package com.ibank.axwms.domain.worklog.service;

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
import com.ibank.axwms.global.error.ErrorCode;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Propagation;
import org.springframework.transaction.annotation.Transactional;

/**
 * 업무 생성 이후 AI 파일 요약, 본문 파이프라인, Light index 요청을 하나의 결과 알림으로 묶는 후처리 서비스다.
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
    private final WorklogLightIndexClient worklogLightIndexClient;
    private final AiWorklogPipelineProperties worklogPipelineProperties;
    private final AiWorklogLightIndexProperties worklogLightIndexProperties;
    private final NotificationService notificationService;
    private final WorklogAiProcessingStatusService worklogAiProcessingStatusService;
    private final WorklogAiPostProcessOutboxRepository outboxRepository;

    /**
     * DB 연결을 점유하지 않고 미완료 단계만 실행한다. 요약은 접수, 색인은 실제 완료 후 checkpoint한다.
     *
     * @param claim 등록 snapshot과 이전 성공 단계를 가진 현재 선점
     * @return 단계 실패와 색인 전달 실패 요청 ID. 원격 접수 직후 장애는 중복 요청될 수 있다.
     */
    @Transactional(propagation = Propagation.NOT_SUPPORTED)
    public ProcessResult process(Claim claim) {
        if (!outboxRepository.isWorklogActive(claim.worklogId())) {
            return new ProcessResult(List.of(), null);
        }
        List<String> failedStages = new ArrayList<>();
        requestFileSummaries(claim, failedStages);
        requestWorklogPipeline(claim, failedStages);
        UUID failedLightRequestId = requestLightIndex(claim, failedStages);
        return new ProcessResult(List.copyOf(failedStages), failedLightRequestId);
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
            return;
        }
        WorklogAiPostProcessRequestedEvent event = claim.event();
        List<String> failedStages = new ArrayList<>(result.failedStages());
        boolean terminalLightFailure = false;
        if (worklogLightIndexProperties.enabled()) {
            if (result.failedLightRequestId() != null && outboxRepository.pendingLightRequest(claim)
                    .filter(result.failedLightRequestId()::equals).isPresent()) {
                failedStages.add(LIGHT_INDEX_STAGE);
            }
            String lightState = outboxRepository.lightState(claim);
            if ("PENDING".equals(lightState) || "RUNNING".equals(lightState)) {
                if (outboxRepository.recoverTimedOutLightRequest(claim)) {
                    outboxRepository.fail(claim, "LIGHT_INDEX_RESULT_TIMEOUT");
                    return;
                }
                if (failedStages.isEmpty()) {
                    outboxRepository.awaitLightResult(claim);
                    return;
                }
            } else if ("FAILED".equals(lightState)) {
                failedStages.add(LIGHT_INDEX_STAGE);
                terminalLightFailure = outboxRepository.ownsLightRequest(claim);
            } else if ("COMPLETED".equals(lightState)) {
                failedStages.removeIf(LIGHT_INDEX_STAGE::equals);
                outboxRepository.checkpoint(claim, LIGHT_INDEX_STAGE);
            }
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

    /**
     * 전달 실패의 요청 ID를 보존하고 현재 요청 여부는 finish의 잠금 안에서 판정한다.
     * 비활성화된 Light index는 운영 설정에 따른 정상 skip으로 처리한다.
     */
    private UUID requestLightIndex(Claim claim, List<String> failedStages) {
        if (!worklogLightIndexProperties.enabled()) {
            executeStage(claim, LIGHT_INDEX_STAGE, () -> {}, failedStages);
            return null;
        }
        UUID requestId = outboxRepository.pendingLightRequest(claim).orElse(null);
        if (requestId == null) {
            return null;
        }
        if (!outboxRepository.renewLease(claim)) {
            throw new BusinessException(ErrorCode.COMMON_INTERNAL_SERVER_ERROR);
        }
        try {
            worklogLightIndexClient.requestIndex(TriggerWorklogLightIndexDto.Request.of(claim.worklogId(), requestId));
            outboxRepository.acceptLightRequest(claim, requestId);
            log.info("Light index accepted worklogId={} requestId={} operation=INDEX deliveryAttemptId={}",
                    claim.worklogId(), requestId, claim.token());
            return null;
        } catch (RuntimeException e) {
            log.warn("AI 색인 접수 실패 worklogId={} requestId={}", claim.worklogId(), requestId, e);
            return requestId;
        }
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

    /** 색인 전달 실패의 요청을 보존해 완료 잠금 이후 교체된 요청에 실패를 반영하지 않는다. */
    public record ProcessResult(List<String> failedStages, UUID failedLightRequestId) {
    }

}
