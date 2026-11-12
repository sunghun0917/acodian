package com.ibank.axwms.domain.worklog.service;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import com.ibank.axwms.domain.file.entity.File;
import com.ibank.axwms.domain.file.repository.FileRepository;
import com.ibank.axwms.domain.worklog.dto.AiTaskApiDto;
import com.ibank.axwms.domain.worklog.entity.Worklog;
import com.ibank.axwms.domain.worklog.repository.WorklogAiPostProcessOutboxRepository;
import com.ibank.axwms.domain.worklog.repository.WorklogAiTaskRepository;
import com.ibank.axwms.domain.worklog.repository.WorklogRepository;
import java.util.Optional;
import java.util.UUID;
import org.junit.jupiter.api.Test;

class InternalWorklogAiTaskStartServiceTest {
    private final WorklogAiPostProcessOutboxRepository locks = mock(WorklogAiPostProcessOutboxRepository.class);
    private final WorklogAiTaskRepository tasks = mock(WorklogAiTaskRepository.class);
    private final WorklogRepository worklogs = mock(WorklogRepository.class);
    private final FileRepository files = mock(FileRepository.class);
    private final InternalWorklogAiTaskStartService service = new InternalWorklogAiTaskStartService(locks, tasks, worklogs, files);

    @Test
    void changedOrDeletedWorklogNeverStarts() {
        var identity = identity("SUMMARY", 10L);
        when(locks.lockWorklog(10L)).thenReturn(Optional.of(true), Optional.of(false));
        assertThat(service.start(10L, identity)).isEmpty();
        when(worklogs.findById(10L)).thenReturn(Optional.of(mock(Worklog.class)));
        when(tasks.isCurrentWorklogInput(eq("SUMMARY"), anyString(), any(), any())).thenReturn(false);
        assertThat(service.start(10L, identity)).isEmpty();
        verify(tasks, never()).start(anyLong(), any());
    }

    @Test
    void deletedOrDifferentFileNeverStarts() {
        var identity = identity("FILE_SUMMARY", 20L);
        when(locks.lockWorklog(10L)).thenReturn(Optional.of(false));
        when(worklogs.findById(10L)).thenReturn(Optional.of(mock(Worklog.class)));
        File file = mock(File.class);
        when(file.getIsDeleted()).thenReturn(true);
        when(files.findByIdForUpdate(20L)).thenReturn(Optional.of(file));
        assertThat(service.start(10L, identity)).isEmpty();
        verify(tasks, never()).start(anyLong(), any());
    }

    /** 동적 실행 ID 외에는 동일한 요청 권위를 재사용한다. */
    private AiTaskApiDto.Identity identity(String kind, Long targetId) {
        return new AiTaskApiDto.Identity(UUID.randomUUID(), UUID.randomUUID(), "a".repeat(64), kind, targetId);
    }
}
