package com.ibank.axwms.domain.worklog.service;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import com.ibank.axwms.domain.notification.service.NotificationService;
import com.ibank.axwms.domain.file.repository.FileRepository;
import com.ibank.axwms.domain.tag.service.InternalTagAiCallbackService;
import com.ibank.axwms.domain.worklog.dto.AiTaskApiDto;
import com.ibank.axwms.domain.worklog.entity.Worklog;
import com.ibank.axwms.domain.worklog.event.WorklogAiPostProcessRequestedEvent;
import com.ibank.axwms.domain.worklog.repository.WorklogAiPostProcessOutboxRepository;
import com.ibank.axwms.domain.worklog.repository.WorklogAiTaskRepository;
import com.ibank.axwms.domain.worklog.repository.WorklogAiTaskRepository.ResultAuthority;
import com.ibank.axwms.domain.worklog.repository.WorklogRepository;
import java.util.List;
import java.util.Optional;
import java.util.UUID;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

class InternalWorklogAiTaskResultServiceTest {
    private final WorklogAiTaskRepository tasks = mock(WorklogAiTaskRepository.class);
    private final WorklogAiPostProcessOutboxRepository locks = mock(WorklogAiPostProcessOutboxRepository.class);
    private final WorklogRepository worklogs = mock(WorklogRepository.class);
    private final InternalTagAiCallbackService tags = mock(InternalTagAiCallbackService.class);
    private final NotificationService notifications = mock(NotificationService.class);
    private final FileRepository files = mock(FileRepository.class);
    private final Worklog worklog = mock(Worklog.class);
    private final InternalWorklogAiTaskResultService service = new InternalWorklogAiTaskResultService(tasks, locks, worklogs, tags, notifications, files);
    private final UUID requestId = UUID.randomUUID();
    private final UUID executionId = UUID.randomUUID();
    private final UUID token = UUID.randomUUID();

    @BeforeEach
    void setup() {
        lenient().when(locks.lockWorklog(10L)).thenReturn(Optional.of(false));
        lenient().when(worklogs.findById(10L)).thenReturn(Optional.of(worklog));
    }

    @Test
    void appliesSummaryAndCreatesOneNotification() {
        var result = result("SUMMARY", "COMPLETED", "요약");
        when(tasks.lockResult(eq(10L), any(), eq("COMPLETED"), anyString())).thenReturn(ResultAuthority.APPLY);
        when(tasks.isCurrentWorklogInput(eq("SUMMARY"), anyString(), any(), any())).thenReturn(true);
        when(tasks.resultSnapshot(eq(10L), eq(result.identity()))).thenReturn(
                new WorklogAiPostProcessRequestedEvent(10L, "원래 제목", "요청", "내용", 1L, 2L, 3L, List.of()));

        assertThat(service.apply(10L, result)).isTrue();
        verify(worklog).changeAiSummary("요약");
        verify(tasks).completeResult(eq(10L), eq(result.identity()), eq("COMPLETED"), anyString(), isNull());
        verify(notifications).createAiTaskResultNotification(1L, 3L, 2L, 10L, "원래 제목", requestId, "SUMMARY", 10L, "COMPLETED");
    }

    @Test
    void staleDuplicateAndDeletedResultsNeverChangeDomain() {
        var result = result("SUMMARY", "COMPLETED", "요약");
        when(tasks.lockResult(eq(10L), any(), anyString(), anyString())).thenReturn(ResultAuthority.REJECTED, ResultAuthority.DUPLICATE);
        assertThat(service.apply(10L, result)).isFalse();
        assertThat(service.apply(10L, result)).isTrue();
        when(locks.lockWorklog(10L)).thenReturn(Optional.of(true));
        assertThat(service.apply(10L, result)).isFalse();
        verify(worklog, never()).changeAiSummary(anyString());
        verifyNoInteractions(notifications, tags);
    }

    @Test
    void failureDoesNotModifyContentButProducesFailureNotification() {
        var result = result("SUMMARY", "FAILED", null);
        when(tasks.lockResult(eq(10L), any(), eq("FAILED"), anyString())).thenReturn(ResultAuthority.APPLY);
        when(tasks.isCurrentWorklogInput(eq("SUMMARY"), anyString(), any(), any())).thenReturn(true);
        when(tasks.resultSnapshot(eq(10L), eq(result.identity()))).thenReturn(
                new WorklogAiPostProcessRequestedEvent(10L, "원래 제목", "요청", "내용", 1L, 2L, 3L, List.of()));
        assertThat(service.apply(10L, result)).isTrue();
        verify(worklog, never()).changeAiSummary(any());
        verify(notifications).createAiTaskResultNotification(1L, 3L, 2L, 10L, "원래 제목", requestId, "SUMMARY", 10L, "FAILED");
    }

    /** 결과 본문을 변경해도 실행 권위는 동일하게 유지한다. */
    private AiTaskApiDto.Result result(String kind, String status, String summary) {
        return new AiTaskApiDto.Result(requestId, executionId, "a".repeat(64), kind, 10L, status, null, summary, null, null);
    }
}
