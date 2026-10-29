package com.ibank.axwms.domain.worklog.service;

import com.ibank.axwms.domain.worklog.dto.TriggerWorklogLightDeleteDto;
import com.ibank.axwms.domain.worklog.dto.TriggerWorklogLightIndexDto;
import com.ibank.axwms.domain.worklog.external.WorklogLightIndexClient;
import com.ibank.axwms.domain.worklog.repository.WorklogLightOutboxRepository;
import com.ibank.axwms.domain.worklog.repository.WorklogLightOutboxRepository.Claim;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

import java.util.List;
import java.util.UUID;

/** DB에 보존된 최신 업무일지 AI 변경을 인스턴스 간 중복 없이 전달한다. */
@Slf4j
@Component
@RequiredArgsConstructor
public class WorklogLightOutboxWorker {

    private final WorklogLightOutboxRepository repository;
    private final WorklogLightIndexClient client;

    /** 일정한 소량의 기한 도래 작업을 처리해 장애 복구와 재시도를 보장한다. */
    @Scheduled(fixedDelayString = "${ai.worklog-light-index.outbox-poll-delay-ms:5000}")
    public void poll() {
        for (int i = 0; i < 20; i++) {
            if (!processOne(null)) {
                break;
            }
        }
    }

    /** 지정 ID 또는 가장 오래된 작업 하나를 선점해 AI에 전달한다. */
    public boolean processOne(Long worklogId) {
        List<Claim> claimed = repository.claim(worklogId, UUID.randomUUID());
        if (claimed.isEmpty()) {
            return false;
        }
        Claim claim = claimed.getFirst();
        try {
            switch (claim.operation()) {
                case "INDEX" -> client.requestIndex(TriggerWorklogLightIndexDto.Request.of(claim.worklogId(), claim.requestId()));
                case "REINDEX" -> client.requestReindex(TriggerWorklogLightIndexDto.Request.of(claim.worklogId(), claim.requestId()));
                case "DELETE" -> client.requestDelete(TriggerWorklogLightDeleteDto.Request.of(claim.worklogId(), claim.requestId()));
                default -> throw new IllegalStateException("Unknown Light operation: " + claim.operation());
            }
            log.info("Light index accepted worklogId={} requestId={} operation={} deliveryAttemptId={}",
                    claim.worklogId(), claim.requestId(), claim.operation(), claim.token());
            repository.awaitResult(claim);
        } catch (RuntimeException e) {
            repository.fail(claim, e.toString());
            log.warn("AI Light outbox 처리 실패 worklogId={} operation={} revision={}",
                    claim.worklogId(), claim.operation(), claim.revision(), e);
        }
        return true;
    }
}
