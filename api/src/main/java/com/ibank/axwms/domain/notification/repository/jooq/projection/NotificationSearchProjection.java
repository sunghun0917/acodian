package com.ibank.axwms.domain.notification.repository.jooq.projection;

import static com.ibank.axwms.global.jooq.Tables.TB_NOTIFICATION;

import java.time.LocalDateTime;
import java.util.UUID;
import org.jooq.Record;
import org.jooq.Field;
import org.jooq.impl.DSL;

/** 내 알림 목록 한 행에 필요한 필드만 담는 읽기 전용 projection 이다. */
public record NotificationSearchProjection(
        Long notificationId,
        String notificationType,    // TODO enum 추가되면 반영될 예정
        String title,
        String content,
        String referenceType,   // TODO enum 추가되면 반영될 예정
        Long referenceId,
        Long departmentId,
        Long teamId,
        Boolean isRead,
        LocalDateTime readAt,
        LocalDateTime createdAt,
        UUID retryRequestId,
        Boolean retryable,
        Boolean targetDeleted
) {

    public static final Field<UUID> RETRY_REQUEST_ID = DSL.field(DSL.name("tb_notification", "ai_request_id"), UUID.class).as("retry_request_id");
    public static final Field<Boolean> TARGET_DELETED = DSL.field("""
            EXISTS (SELECT 1 FROM tb_worklog w WHERE w.worklog_id = tb_notification.reference_id AND w.is_deleted)
            """, Boolean.class).as("target_deleted");
    public static final Field<Boolean> RETRYABLE = DSL.field("""
            COALESCE(tb_notification.ai_result = 'FAILED' AND (
                (EXISTS (SELECT 1 FROM tb_worklog_ai_post_process_outbox a
                         WHERE a.worklog_id = tb_notification.reference_id AND a.request_id = tb_notification.ai_request_id
                           AND a.task_kind IN ('SUMMARY', 'TAG', 'FILE_SUMMARY')
                           AND a.execution_state = 'FAILED')
                 AND EXISTS (SELECT 1 FROM tb_worklog w
                             JOIN tb_worklog_ai_post_process_outbox a ON a.worklog_id = w.worklog_id
                             WHERE w.worklog_id = tb_notification.reference_id AND a.request_id = tb_notification.ai_request_id
                               AND NOT w.is_deleted AND (
                                 (a.task_kind IN ('SUMMARY', 'TAG')
                                  AND a.payload->>'workContent' IS NOT DISTINCT FROM w.work_content
                                  AND (a.task_kind = 'TAG' OR a.payload->>'requestContent' IS NOT DISTINCT FROM w.request_content))
                                 OR (a.task_kind = 'FILE_SUMMARY' AND EXISTS (
                                     SELECT 1 FROM tb_file f, jsonb_array_elements(a.payload->'files') snapshot
                                     WHERE f.file_id = a.target_id AND f.worklog_id = w.worklog_id AND NOT f.is_deleted
                                       AND snapshot->>'fileId' = f.file_id::text
                                       AND snapshot->>'storageKey' = f.stored_path
                                       AND snapshot->>'originalName' = f.original_name
                                       AND snapshot->>'fileExtension' = f.file_extension
                                 )))))
              OR (tb_notification.ai_task_kind IN ('INDEX', 'REINDEX', 'DELETE')
                  AND EXISTS (SELECT 1 FROM tb_worklog_light_outbox o
                              JOIN tb_worklog w ON w.worklog_id = o.worklog_id AND w.light_index_request_id = o.request_id
                              WHERE o.worklog_id = tb_notification.reference_id AND o.request_id = tb_notification.ai_request_id
                                AND o.terminal_failed AND ((o.operation = 'DELETE') = w.is_deleted)))
            ), FALSE)
            """, Boolean.class).as("retryable");

    /** 기존 알림에는 작업 식별자와 재시도 정보가 없다. */
    public NotificationSearchProjection(Long notificationId, String notificationType, String title, String content,
                                        String referenceType, Long referenceId, Long departmentId, Long teamId,
                                        Boolean isRead, LocalDateTime readAt, LocalDateTime createdAt) {
        this(notificationId, notificationType, title, content, referenceType, referenceId, departmentId, teamId,
                isRead, readAt, createdAt, null, false, false);
    }

    /** JOOQ 조회 결과 record 를 알림 목록 projection 으로 조립한다. */
    public static NotificationSearchProjection from(Record record) {
        return new NotificationSearchProjection(
                record.get(TB_NOTIFICATION.NOTIFICATION_ID),
                record.get(TB_NOTIFICATION.NOTIFICATION_TYPE),
                record.get(TB_NOTIFICATION.TITLE),
                record.get(TB_NOTIFICATION.CONTENT),
                record.get(TB_NOTIFICATION.REFERENCE_TYPE),
                record.get(TB_NOTIFICATION.REFERENCE_ID),
                record.get(TB_NOTIFICATION.DEPARTMENT_ID),
                record.get(TB_NOTIFICATION.TEAM_ID),
                record.get(TB_NOTIFICATION.IS_READ),
                record.get(TB_NOTIFICATION.READ_AT),
                record.get(TB_NOTIFICATION.CREATED_AT),
                record.get(RETRY_REQUEST_ID),
                record.get(RETRYABLE),
                record.get(TARGET_DELETED)
        );
    }
}
