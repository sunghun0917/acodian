package com.ibank.axwms.domain.file.event;

import com.ibank.axwms.domain.file.repository.FileRepository;
import com.ibank.axwms.domain.organization.team.repository.TeamRepository;
import com.ibank.axwms.domain.worklog.event.WorklogAiPostProcessRequestedEvent;
import com.ibank.axwms.domain.worklog.repository.WorklogAiTaskRepository;
import com.ibank.axwms.domain.worklog.repository.WorklogRepository;
import com.ibank.axwms.global.error.BusinessException;
import com.ibank.axwms.global.error.ErrorCode;
import java.util.List;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Component;
import org.springframework.transaction.event.TransactionPhase;
import org.springframework.transaction.event.TransactionalEventListener;

/** 업무 수정 중 추가된 파일의 요약 의도를 파일 저장과 같은 트랜잭션에 보관한다. */
@Component
@RequiredArgsConstructor
public class WorklogFileAiSummaryTrigger {
    private final FileRepository files;
    private final WorklogRepository worklogs;
    private final TeamRepository teams;
    private final WorklogAiTaskRepository tasks;

    /** 커밋 실패 시 파일과 작업이 함께 롤백되며 외부 호출은 스케줄러에 맡긴다. */
    @TransactionalEventListener(phase = TransactionPhase.BEFORE_COMMIT)
    public void onCommit(WorklogFileAiSummaryRequestedEvent event) {
        files.flush();
        var worklog = worklogs.findById(event.worklogId())
                .orElseThrow(() -> new BusinessException(ErrorCode.WORKLOG_NOT_FOUND));
        Long departmentId = teams.findById(worklog.getTeamId()).map(team -> team.getDepartmentId()).orElse(null);
        var file = new WorklogAiPostProcessRequestedEvent.FileSummaryTarget(event.fileId(), event.storageKey(),
                event.originalName(), event.fileExtension());
        var snapshot = new WorklogAiPostProcessRequestedEvent(worklog.getId(), worklog.getTitle(),
                worklog.getRequestContent(), worklog.getWorkContent(), worklog.getAuthorId(), worklog.getTeamId(),
                departmentId, List.of(file));
        tasks.enqueueFileTask(snapshot, file);
    }
}
