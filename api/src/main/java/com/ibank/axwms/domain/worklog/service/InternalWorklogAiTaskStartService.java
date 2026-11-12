package com.ibank.axwms.domain.worklog.service;

import com.ibank.axwms.domain.file.repository.FileRepository;
import com.ibank.axwms.domain.worklog.dto.AiTaskApiDto;
import com.ibank.axwms.domain.worklog.repository.WorklogAiPostProcessOutboxRepository;
import com.ibank.axwms.domain.worklog.repository.WorklogAiTaskRepository;
import com.ibank.axwms.domain.worklog.repository.WorklogRepository;
import java.util.Optional;
import java.util.UUID;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/** 현재 원본을 확인한 뒤에만 작업 실행 권위를 발급한다. */
@Service
@RequiredArgsConstructor
public class InternalWorklogAiTaskStartService {
    private final WorklogAiPostProcessOutboxRepository locks;
    private final WorklogAiTaskRepository tasks;
    private final WorklogRepository worklogs;
    private final FileRepository files;

    /** 삭제·입력 변경과 동시에 옛 작업이 실행되지 않도록 업무 행부터 잠근다. */
    @Transactional
    public Optional<UUID> start(Long worklogId, AiTaskApiDto.Identity identity) {
        if (locks.lockWorklog(worklogId).orElse(true)) {
            return Optional.empty();
        }
        var worklog = worklogs.findById(worklogId).orElse(null);
        if (worklog == null) {
            return Optional.empty();
        }
        if ("FILE_SUMMARY".equals(identity.taskKind())) {
            var file = files.findByIdForUpdate(identity.targetId()).orElse(null);
            if (file == null || Boolean.TRUE.equals(file.getIsDeleted()) || !worklogId.equals(file.getWorklogId())
                    || !tasks.isCurrentFileInput(identity.inputHash(), file.getId(), file.getStoredPath(),
                    file.getOriginalName(), file.getFileExtension())) {
                return Optional.empty();
            }
        } else if (!worklogId.equals(identity.targetId()) || !tasks.isCurrentWorklogInput(identity.taskKind(),
                identity.inputHash(), worklog.getRequestContent(), worklog.getWorkContent())) {
            return Optional.empty();
        }
        return tasks.start(worklogId, identity);
    }
}
