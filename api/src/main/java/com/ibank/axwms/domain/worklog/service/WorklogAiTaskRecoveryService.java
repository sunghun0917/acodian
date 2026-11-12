package com.ibank.axwms.domain.worklog.service;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.ibank.axwms.domain.file.repository.FileRepository;
import com.ibank.axwms.domain.notification.service.NotificationService;
import com.ibank.axwms.domain.worklog.event.WorklogAiPostProcessRequestedEvent;
import com.ibank.axwms.domain.worklog.repository.WorklogAiPostProcessOutboxRepository;
import com.ibank.axwms.domain.worklog.repository.WorklogAiTaskRepository;
import com.ibank.axwms.domain.worklog.repository.WorklogAiTaskRepository.RecoveryCandidate;
import com.ibank.axwms.domain.worklog.repository.WorklogRepository;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/** 단일 lease가 만료된 현재 작업을 실패 알림·수동 재시도로 연결한다. */
@Service
@RequiredArgsConstructor
public class WorklogAiTaskRecoveryService {
    private static final ObjectMapper OBJECT_MAPPER = new ObjectMapper();
    private final WorklogAiPostProcessOutboxRepository locks;
    private final WorklogAiTaskRepository tasks;
    private final WorklogRepository worklogs;
    private final FileRepository files;
    private final NotificationService notifications;

    /** 업무→Outbox 순으로 잠그고 현재 요청의 lease 만료만 실패로 정리한다. */
    @Transactional
    public boolean reconcile(RecoveryCandidate candidate) {
        boolean active = locks.lockWorklog(candidate.worklogId()).map(deleted -> !deleted).orElse(false);
        var payload = tasks.failTimedOut(candidate);
        if (payload.isEmpty()) return false;
        if (active) notifyFailure(candidate, payload.get());
        return true;
    }

    /** timeout 실패와 사용자 알림을 동일 트랜잭션에서 저장한다. */
    private void notifyFailure(RecoveryCandidate candidate, String payload) {
        WorklogAiPostProcessRequestedEvent snapshot;
        try {
            snapshot = OBJECT_MAPPER.readValue(payload, WorklogAiPostProcessRequestedEvent.class);
        } catch (JsonProcessingException e) {
            throw new IllegalStateException("AI 작업 snapshot 역직렬화 실패", e);
        }
        if ("SUMMARY".equals(candidate.taskKind())) {
            worklogs.findById(candidate.worklogId()).ifPresent(worklog -> worklog.failAiProcessing());
        } else if ("FILE_SUMMARY".equals(candidate.taskKind())) {
            files.findByIdForUpdate(candidate.targetId())
                    .filter(file -> candidate.worklogId().equals(file.getWorklogId()) && !Boolean.TRUE.equals(file.getIsDeleted()))
                    .ifPresent(file -> file.failAiSummaryProcessing());
        }
        notifications.createAiTaskResultNotification(snapshot.authorId(), snapshot.departmentId(), snapshot.teamId(),
                candidate.worklogId(), snapshot.worklogTitle(), candidate.requestId(), candidate.taskKind(),
                candidate.targetId(), "FAILED");
    }
}
