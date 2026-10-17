package com.ibank.axwms.domain.worklog.service;

import com.ibank.axwms.domain.worklog.repository.WorklogAiPostProcessOutboxRepository;
import com.ibank.axwms.domain.worklog.repository.WorklogAiPostProcessOutboxRepository.Claim;
import java.util.List;
import java.util.UUID;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

/** 등록 후처리를 DB에서 복구해 실행하며 외부 호출 동안에는 DB 트랜잭션을 유지하지 않는다. */
@Slf4j
@Component
@RequiredArgsConstructor
public class WorklogAiPostProcessOutboxWorker {

    private static final int MAX_JOBS_PER_POLL = 20;
    private final WorklogAiPostProcessOutboxRepository repository;
    private final WorklogAiPostProcessService service;

    /** API 재기동 후에도 기한이 된 작업을 선점하며 한 번에 처리하는 개수를 제한한다. */
    @Scheduled(fixedDelayString = "${ai.worklog-post-process.outbox-poll-delay-ms:5000}",
            initialDelayString = "${ai.worklog-post-process.outbox-poll-delay-ms:5000}")
    public void poll() {
        for (int i = 0; i < MAX_JOBS_PER_POLL; i++) {
            if (!processOne(null)) {
                break;
            }
        }
    }

    /** 선점 실패는 정상 종료하고 실행·완료 트랜잭션 실패는 checkpoint를 유지한 채 재시도한다. */
    public boolean processOne(Long worklogId) {
        List<Claim> claims = repository.claim(worklogId, UUID.randomUUID());
        if (claims.isEmpty()) {
            return false;
        }
        Claim claim = claims.getFirst();
        try {
            service.finish(claim, service.process(claim));
        } catch (RuntimeException e) {
            repository.fail(claim, e.getClass().getSimpleName());
            log.warn("AI 등록 Outbox 처리 실패 worklogId={}", claim.worklogId(), e);
        }
        return true;
    }
}
