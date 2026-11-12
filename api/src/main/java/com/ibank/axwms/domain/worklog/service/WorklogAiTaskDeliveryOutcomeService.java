package com.ibank.axwms.domain.worklog.service;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.ibank.axwms.domain.notification.service.NotificationService;
import com.ibank.axwms.domain.worklog.event.WorklogAiPostProcessRequestedEvent;
import com.ibank.axwms.domain.worklog.repository.WorklogAiPostProcessOutboxRepository;
import com.ibank.axwms.domain.worklog.repository.WorklogAiTaskRepository;
import com.ibank.axwms.domain.worklog.repository.WorklogAiTaskRepository.DeliveryClaim;
import com.ibank.axwms.domain.worklog.repository.WorklogRepository;
import com.ibank.axwms.domain.file.repository.FileRepository;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/** 접수 확인 실패의 자동 중단과 사용자 알림을 원자적으로 저장한다. */
@Service
@RequiredArgsConstructor
public class WorklogAiTaskDeliveryOutcomeService {
    private static final ObjectMapper OBJECT_MAPPER = new ObjectMapper();
    private final WorklogAiPostProcessOutboxRepository locks;
    private final WorklogAiTaskRepository tasks;
    private final NotificationService notifications;
    private final WorklogRepository worklogs;
    private final FileRepository files;

    /** 실행 승인이 먼저 도착한 경우에는 접수 실패 알림을 만들지 않는다. */
    @Transactional
    public boolean fail(DeliveryClaim claim, String error) {
        boolean active = locks.lockWorklog(claim.worklogId()).map(deleted -> !deleted).orElse(false);
        if (!tasks.deliveryFailed(claim, error)) {
            return false;
        }
        if (active && tasks.isFailed(claim)) {
            var snapshot = snapshot(claim);
            if ("SUMMARY".equals(claim.taskKind())) {
                worklogs.findById(claim.worklogId()).ifPresent(worklog -> worklog.failAiProcessing());
            } else if ("FILE_SUMMARY".equals(claim.taskKind())) {
                files.findByIdForUpdate(claim.targetId())
                        .filter(file -> claim.worklogId().equals(file.getWorklogId()) && !Boolean.TRUE.equals(file.getIsDeleted()))
                        .ifPresent(file -> file.failAiSummaryProcessing());
            }
            notifications.createAiTaskResultNotification(snapshot.authorId(), snapshot.departmentId(), snapshot.teamId(),
                    claim.worklogId(), snapshot.worklogTitle(), claim.requestId(), claim.taskKind(), claim.targetId(),
                    "FAILED");
        }
        return true;
    }

    /** 전달 실패 알림은 접수 당시의 작성자와 제목을 표시한다. */
    private WorklogAiPostProcessRequestedEvent snapshot(DeliveryClaim claim) {
        try {
            return OBJECT_MAPPER.readValue(claim.payload(), WorklogAiPostProcessRequestedEvent.class);
        } catch (JsonProcessingException e) {
            throw new IllegalStateException("AI 작업 snapshot 역직렬화 실패", e);
        }
    }
}
