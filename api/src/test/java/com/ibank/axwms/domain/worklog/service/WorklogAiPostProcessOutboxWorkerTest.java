package com.ibank.axwms.domain.worklog.service;

import com.ibank.axwms.domain.worklog.event.WorklogAiPostProcessRequestedEvent;
import com.ibank.axwms.domain.worklog.repository.WorklogAiPostProcessOutboxRepository;
import com.ibank.axwms.domain.worklog.repository.WorklogAiPostProcessOutboxRepository.Claim;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

class WorklogAiPostProcessOutboxWorkerTest {

    private final WorklogAiPostProcessOutboxRepository repository = mock(WorklogAiPostProcessOutboxRepository.class);
    private final WorklogAiPostProcessService service = mock(WorklogAiPostProcessService.class);
    private final WorklogAiPostProcessOutboxWorker worker = new WorklogAiPostProcessOutboxWorker(repository, service);

    @Test
    @DisplayName("선점 가능한 작업이 없으면 AI 후처리를 실행하지 않는다")
    void no_due_job_does_not_dispatch() {
        assertThat(worker.processOne(501L)).isFalse();
        verifyNoInteractions(service);
    }

    @Test
    @DisplayName("요청 실패 단계를 정상 반환해도 완료로 오인하지 않고 완료 트랜잭션에 전달한다")
    void failed_stage_result_is_forwarded_to_transactional_finish() {
        Claim claim = claim();
        when(repository.claim(eq(501L), any())).thenReturn(List.of(claim));
        var result = new WorklogAiPostProcessService.ProcessResult(List.of(), UUID.randomUUID());
        when(service.process(claim)).thenReturn(result);

        assertThat(worker.processOne(501L)).isTrue();

        verify(service).finish(claim, result);
        verify(repository, never()).complete(any());
    }

    @Test
    @DisplayName("완료 트랜잭션이 실패하면 작업을 제거하지 않고 재시도 대상으로 남긴다")
    void failed_finish_keeps_job_for_retry() {
        Claim claim = claim();
        when(repository.claim(eq(501L), any())).thenReturn(List.of(claim));
        var result = new WorklogAiPostProcessService.ProcessResult(List.of(), null);
        when(service.process(claim)).thenReturn(result);
        doThrow(new IllegalStateException("DB unavailable")).when(service).finish(claim, result);

        assertThat(worker.processOne(501L)).isTrue();

        verify(repository).fail(claim, "IllegalStateException");
        verify(repository, never()).complete(any());
    }

    /** 외부 요청의 재실행 여부와 무관하게 선점 token을 완료 처리까지 보존한다. */
    private static Claim claim() {
        var event = new WorklogAiPostProcessRequestedEvent(501L, "업무", "요청", "본문", 101L, 21L, 9L, List.of());
        return new Claim(501L, event, Set.of(), 0, UUID.randomUUID());
    }
}
