package com.ibank.axwms.domain.worklog.service;

import com.ibank.axwms.domain.worklog.dto.TriggerWorklogLightDeleteDto;
import com.ibank.axwms.domain.worklog.dto.TriggerWorklogLightIndexDto;
import com.ibank.axwms.domain.worklog.external.AiWorklogLightIndexProperties;
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
    private final AiWorklogLightIndexProperties properties;

    /** 일정한 소량의 기한 도래 작업을 처리해 장애 복구와 재시도를 보장한다. */
    @Scheduled(fixedDelayString = "${ai.worklog-light-index.outbox-poll-delay-ms:5000}")
    public void poll() {
        if (!properties.enabled()) {
            return;
        }
        for (int i = 0; i < 20; i++) {
            if (!processOne(null)) {
                break;
            }
        }
    }

    /** 지정 ID 또는 가장 오래된 작업 하나를 선점해 AI에 전달한다. */
    public boolean processOne(Long worklogId) {
        if (!properties.enabled()) {
            return false;
        }
        List<Claim> claimed = repository.claim(worklogId, UUID.randomUUID());
        if (claimed.isEmpty()) {
            return false;
        }
        Claim claim = claimed.getFirst();
        try {
            if ("DELETE".equals(claim.operation())) {
                if (!properties.deleteEnabled()) {
                    repository.fail(claim, "AI Light 삭제 기능 비활성화");
                    return true;
                }
                client.requestDelete(TriggerWorklogLightDeleteDto.Request.of(claim.worklogId()));
            } else {
                if (!properties.updateEnabled()) {
                    repository.fail(claim, "AI Light 재색인 기능 비활성화");
                    return true;
                }
                client.requestReindex(TriggerWorklogLightIndexDto.Request.of(claim.worklogId()));
            }
            repository.complete(claim);
        } catch (RuntimeException e) {
            repository.fail(claim, e.toString());
            log.warn("AI Light outbox 처리 실패 worklogId={} operation={} revision={}",
                    claim.worklogId(), claim.operation(), claim.revision(), e);
        }
        return true;
    }
}
