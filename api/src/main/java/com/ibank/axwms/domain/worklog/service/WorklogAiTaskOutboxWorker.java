package com.ibank.axwms.domain.worklog.service;

import com.fasterxml.jackson.core.JsonProcessingException;
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
import java.util.UUID;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Component;
import org.springframework.scheduling.annotation.Scheduled;

/** 선점된 불변 작업을 DB 트랜잭션 밖에서 AI 서버에 발행한다. */
@Slf4j
@Component
@RequiredArgsConstructor
public class WorklogAiTaskOutboxWorker {
    private static final ObjectMapper OBJECT_MAPPER = new ObjectMapper();
    private final WorklogAiTaskRepository tasks;
    private final WorklogPipelineClient pipeline;
    private final FileSummaryClient files;
    private final WorklogAiTaskDeliveryOutcomeService outcomes;
    private final WorklogAiTaskRecoveryService recovery;
    private final AiWorklogPipelineProperties pipelineProperties;
    private final AiFileSummaryProperties fileProperties;

    /** lease가 만료된 작업을 먼저 종결하고 기한이 된 작업을 한 번에 최대 20건 발행한다. */
    @Scheduled(fixedDelayString = "${ai.worklog-post-process.outbox-poll-delay-ms:5000}",
            initialDelayString = "${ai.worklog-post-process.outbox-poll-delay-ms:5000}")
    public void poll() {
        recoverExpired();
        for (int i = 0; i < 20; i++) {
            if (!processOne()) {
                break;
            }
        }
    }

    /** 한 건만 선점하고 실제 202 접수를 확인한 뒤 동일 선점의 결과를 기록한다. */
    public boolean processOne() {
        return processOne(null);
    }

    /** 특정 업무 지정은 수동 점검에 사용하며 일반 폴링은 전체 대기 작업을 처리한다. */
    public boolean processOne(Long worklogId) {
        var maybeClaim = tasks.claimDelivery(worklogId, UUID.randomUUID(), pipelineProperties.enabled(), fileProperties.enabled());
        if (maybeClaim.isEmpty()) {
            return false;
        }
        var claim = maybeClaim.get();
        try {
            dispatch(claim);
            tasks.deliveryAccepted(claim);
        } catch (RuntimeException e) {
            outcomes.fail(claim, e.getClass().getSimpleName());
            log.warn("AI 작업 전달 실패 worklogId={} requestId={} errorType={}",
                    claim.worklogId(), claim.requestId(), e.getClass().getSimpleName());
        }
        return true;
    }

    /** 결과가 오지 않은 작업을 단일 lease 만료로 실패 처리한다. */
    public void recoverExpired() {
        for (var candidate : tasks.recoveryCandidates(20)) {
            try {
                recovery.reconcile(candidate);
            } catch (RuntimeException e) {
                log.warn("AI 실행 복구 실패 worklogId={} requestId={} errorType={}",
                        candidate.worklogId(), candidate.requestId(), e.getClass().getSimpleName());
            }
        }
    }

    /** 저장된 종류와 대상에 맞는 단일 작업만 선택해 같은 요청 식별자를 전달한다. */
    private void dispatch(DeliveryClaim claim) {
        WorklogAiPostProcessRequestedEvent event;
        try {
            event = OBJECT_MAPPER.readValue(claim.payload(), WorklogAiPostProcessRequestedEvent.class);
        } catch (JsonProcessingException e) {
            throw new IllegalStateException("AI 작업 snapshot 역직렬화 실패", e);
        }
        if (!claim.worklogId().equals(event.worklogId())) {
            throw new IllegalStateException("AI 작업 업무 식별자 불일치");
        }
        if ("FILE_SUMMARY".equals(claim.taskKind())) {
            var file = event.files().stream().filter(item -> claim.targetId().equals(item.fileId()))
                    .findFirst().orElseThrow(() -> new IllegalStateException("AI 파일 snapshot 누락"));
            files.requestSummary(new TriggerFileSummaryDto.Request(file.fileId(), event.worklogId(), file.storageKey(),
                    file.originalName(), file.fileExtension(), claim.requestId(), claim.inputHash()));
        } else if (("SUMMARY".equals(claim.taskKind()) || "TAG".equals(claim.taskKind()))
                && claim.worklogId().equals(claim.targetId())) {
            pipeline.requestPipeline(new TriggerWorklogPipelineDto.Request(event.worklogId(), event.requestContent(),
                    event.workContent(), event.authorId(), event.teamId(), event.departmentId(), claim.taskKind(),
                    claim.requestId(), claim.inputHash()));
        } else {
            throw new IllegalStateException("지원하지 않는 AI 작업 대상");
        }
    }
}
