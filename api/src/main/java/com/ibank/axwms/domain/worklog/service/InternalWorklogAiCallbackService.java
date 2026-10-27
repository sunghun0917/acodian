package com.ibank.axwms.domain.worklog.service;

import com.ibank.axwms.domain.worklog.dto.UpdateWorklogAiApiDto;
import com.ibank.axwms.domain.worklog.entity.Worklog;
import com.ibank.axwms.domain.worklog.repository.WorklogRepository;
import com.ibank.axwms.domain.worklog.repository.WorklogAiPostProcessOutboxRepository;
import com.ibank.axwms.global.enums.AiProcessingStatus;
import com.ibank.axwms.global.error.BusinessException;
import com.ibank.axwms.global.error.ErrorCode;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
@RequiredArgsConstructor
@Transactional(readOnly = true)
public class InternalWorklogAiCallbackService {

    private final WorklogRepository worklogRepository;
    private final WorklogAiPostProcessOutboxRepository outboxRepository;

    /**
     * 요약 본문은 반영하되 등록 후처리가 관리하는 전체 상태를 개별 요약 콜백으로 덮지 않는다.
     */
    @Transactional
    public void updateAiResult(Long worklogId, UpdateWorklogAiApiDto.Request request) {

        outboxRepository.lockWorklog(worklogId);
        Worklog worklog = getWorklogOrThrow(worklogId);
        boolean managedPostProcess = outboxRepository.hasPostProcess(worklogId);

        if (request.aiProcessingStatus() == AiProcessingStatus.COMPLETED) {
            worklog.changeAiSummary(request.aiSummary());
            if (!managedPostProcess) {
                worklog.completeAiProcessing();
            }
        } else if (request.aiProcessingStatus() == AiProcessingStatus.FAILED && !managedPostProcess) {
            worklog.failAiProcessing();
        }

    }

    /**
     * 잘못된 업무일지 ID로 AI 요약 처리 결과가 오염되지 않도록 대상 존재를 먼저 확정한다.
     */
    private Worklog getWorklogOrThrow(Long worklogId) {
        return worklogRepository.findById(worklogId)
                .orElseThrow(() -> new BusinessException(ErrorCode.WORKLOG_NOT_FOUND));
    }

}
