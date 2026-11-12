package com.ibank.axwms.domain.worklog.service;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.ibank.axwms.domain.notification.service.NotificationService;
import com.ibank.axwms.domain.file.repository.FileRepository;
import com.ibank.axwms.domain.tag.service.InternalTagAiCallbackService;
import com.ibank.axwms.domain.worklog.dto.AiTaskApiDto;
import com.ibank.axwms.domain.worklog.dto.ApplyWorklogAiTagsApiDto;
import com.ibank.axwms.domain.worklog.repository.WorklogAiPostProcessOutboxRepository;
import com.ibank.axwms.domain.worklog.repository.WorklogAiTaskRepository;
import com.ibank.axwms.domain.worklog.repository.WorklogAiTaskRepository.ResultAuthority;
import com.ibank.axwms.domain.worklog.repository.WorklogRepository;
import com.ibank.axwms.global.error.BusinessException;
import com.ibank.axwms.global.error.ErrorCode;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.HexFormat;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/** 현재 일반 AI 실행의 결과와 작업별 알림을 하나의 트랜잭션으로 확정한다. */
@Service
@RequiredArgsConstructor
public class InternalWorklogAiTaskResultService {
    private static final ObjectMapper OBJECT_MAPPER = new ObjectMapper();
    private final WorklogAiTaskRepository tasks;
    private final WorklogAiPostProcessOutboxRepository locks;
    private final WorklogRepository worklogs;
    private final InternalTagAiCallbackService tags;
    private final NotificationService notifications;
    private final FileRepository files;

    /** 업무→작업 순서로 잠그고 입력과 실행 권위가 현재인 결과만 반영한다. */
    @Transactional
    public boolean apply(Long worklogId, AiTaskApiDto.Result result) {
        boolean fileTask = "FILE_SUMMARY".equals(result.taskKind());
        if (!("SUMMARY".equals(result.taskKind()) || "TAG".equals(result.taskKind()) || fileTask)
                || (!fileTask && !worklogId.equals(result.targetId()))) {
            throw new BusinessException(ErrorCode.COMMON_INVALID_REQUEST);
        }
        if (locks.lockWorklog(worklogId).orElse(true)) {
            return false;
        }
        String digest = digest(result);
        ResultAuthority authority = tasks.lockResult(worklogId, result.identity(), result.status(), digest);
        if (authority == ResultAuthority.DUPLICATE) {
            return true;
        }
        if (authority != ResultAuthority.APPLY) {
            return false;
        }
        var worklog = worklogs.findById(worklogId).orElseThrow(() -> new BusinessException(ErrorCode.WORKLOG_NOT_FOUND));
        com.ibank.axwms.domain.file.entity.File file = null;
        if (fileTask) {
            file = files.findByIdForUpdate(result.targetId()).orElse(null);
            if (file == null || Boolean.TRUE.equals(file.getIsDeleted()) || !worklogId.equals(file.getWorklogId())
                    || !tasks.isCurrentFileInput(result.inputHash(), file.getId(), file.getStoredPath(),
                    file.getOriginalName(), file.getFileExtension())) {
                return false;
            }
        } else if (!tasks.isCurrentWorklogInput(result.taskKind(), result.inputHash(), worklog.getRequestContent(), worklog.getWorkContent())) {
            return false;
        }
        var snapshot = tasks.resultSnapshot(worklogId, result.identity());
        if ("COMPLETED".equals(result.status())) {
            if (fileTask) {
                if (result.aiSummary() == null || result.aiSummary().isBlank()) {
                    throw new BusinessException(ErrorCode.COMMON_INVALID_REQUEST);
                }
                file.changeAiSummary(result.aiSummary());
                file.completeAiSummaryProcessing();
            } else {
                applySuccessfulResult(worklogId, result, worklog);
                if ("SUMMARY".equals(result.taskKind())) {
                    worklog.completeAiProcessing();
                }
            }
        } else if (fileTask) {
            file.failAiSummaryProcessing();
        } else if ("SUMMARY".equals(result.taskKind())) {
            worklog.failAiProcessing();
        }
        tasks.completeResult(worklogId, result.identity(), result.status(), digest, result.errorCode());
        notifications.createAiTaskResultNotification(snapshot.authorId(), snapshot.departmentId(), snapshot.teamId(),
                worklogId, snapshot.worklogTitle(), result.requestId(), result.taskKind(), result.targetId(), result.status());
        return true;
    }

    /** 성공 결과만 기존 도메인 규칙을 거쳐 반영하고 실패 결과는 본문을 건드리지 않는다. */
    private void applySuccessfulResult(Long worklogId, AiTaskApiDto.Result result,
                                       com.ibank.axwms.domain.worklog.entity.Worklog worklog) {
        if ("SUMMARY".equals(result.taskKind())) {
            if (result.aiSummary() == null || result.aiSummary().isBlank()) {
                throw new BusinessException(ErrorCode.COMMON_INVALID_REQUEST);
            }
            worklog.changeAiSummary(result.aiSummary());
            return;
        }
        if (result.existingTagIds() == null) {
            throw new BusinessException(ErrorCode.COMMON_INVALID_REQUEST);
        }
        var request = new ApplyWorklogAiTagsApiDto.Request(result.existingTagIds(), result.newTags());
        if (!request.hasAnyTag() || !request.isWithinTagLimit()) {
            throw new BusinessException(ErrorCode.COMMON_INVALID_REQUEST);
        }
        tags.applyAiGeneratedTags(worklogId, request);
    }

    /** 같은 실행의 다른 최종 본문을 중복 성공으로 오인하지 않도록 전체 결과를 고정한다. */
    private String digest(AiTaskApiDto.Result result) {
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256")
                    .digest(OBJECT_MAPPER.writeValueAsBytes(result)));
        } catch (JsonProcessingException | NoSuchAlgorithmException e) {
            throw new IllegalStateException("AI 작업 결과 해시 실패", e);
        }
    }
}
