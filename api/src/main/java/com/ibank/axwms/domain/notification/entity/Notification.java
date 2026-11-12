package com.ibank.axwms.domain.notification.entity;

import com.ibank.axwms.domain.notification.NotificationReferenceType;
import com.ibank.axwms.domain.notification.NotificationType;
import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import lombok.AccessLevel;
import lombok.Getter;
import lombok.NoArgsConstructor;
import org.hibernate.annotations.CreationTimestamp;
import org.hibernate.annotations.UpdateTimestamp;

import java.time.LocalDateTime;
import java.util.UUID;

@Entity
@Table(name = "tb_notification")
@Getter
@NoArgsConstructor(access = AccessLevel.PROTECTED)
public class Notification {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    @Column(name = "notification_id")
    private Long id;

    @Column(name = "user_id", nullable = false)
    private Long userId;

    @Column(name = "department_id")
    private Long departmentId;

    @Column(name = "team_id")
    private Long teamId;

    @Column(name = "notification_type", nullable = false, length = 50)
    private String notificationType;

    @Column(name = "title", nullable = false, length = 200)
    private String title;

    @Column(name = "content", columnDefinition = "TEXT")
    private String content;

    @Column(name = "reference_type", length = 50)
    private String referenceType;

    @Column(name = "reference_id")
    private Long referenceId;

    @Column(name = "is_read", nullable = false)
    private Boolean isRead;

    @Column(name = "read_at")
    private LocalDateTime readAt;

    @CreationTimestamp
    @Column(name = "created_at", nullable = false, updatable = false)
    private LocalDateTime createdAt;

    @UpdateTimestamp
    @Column(name = "updated_at", nullable = false)
    private LocalDateTime updatedAt;

    @Column(name = "ai_request_id")
    private UUID aiRequestId;

    @Column(name = "ai_task_kind", length = 20)
    private String aiTaskKind;

    @Column(name = "ai_target_id")
    private Long aiTargetId;

    @Column(name = "ai_result", length = 30)
    private String aiResult;

    /** 작업별 알림은 생성 당시 제목과 요청 식별자를 보존하여 이후 재시도 대상을 판별한다. */
    public static Notification createAiTaskResult(Long userId, Long departmentId, Long teamId, Long worklogId,
                                                   String worklogTitle, UUID requestId, String taskKind,
                                                   Long targetId, String result) {
        String label = switch (taskKind) {
            case "SUMMARY" -> "요약";
            case "TAG" -> "태그";
            case "FILE_SUMMARY" -> "파일 요약";
            case "INDEX" -> "색인";
            case "REINDEX" -> "재색인";
            case "DELETE" -> "색인 삭제";
            default -> throw new IllegalArgumentException("Invalid AI task kind");
        };
        if (!("COMPLETED".equals(result) || "FAILED".equals(result))) {
            throw new IllegalArgumentException("Invalid AI task result");
        }
        String outcome = "COMPLETED".equals(result) ? "완료" : "실패";
        String action = "COMPLETED".equals(result) ? "완료되었습니다." : "실패했습니다.";
        Notification notification = create(userId, departmentId, teamId, NotificationType.WORKLOG_AI_TASK_RESULT.name(),
                "AI " + label + " " + outcome, "업무일지 '" + worklogTitle + "'의 " + label + " 작업이 " + action,
                NotificationReferenceType.WORKLOG.name(), worklogId, false, null);
        notification.aiRequestId = requestId;
        notification.aiTaskKind = taskKind;
        notification.aiTargetId = targetId;
        notification.aiResult = result;
        return notification;
    }

    /**
     * D-3 업무 알림의 중복 방지 identity 가 엔티티 생성 시점부터 같은 값으로 저장되게 한다.
     */
    public static Notification createWorklogDueSoonReminder(Long userId,
                                                            Long departmentId,
                                                            Long teamId,
                                                            Long worklogId,
                                                            String title,
                                                            String content) {
        return createWorklogReminder(
                userId,
                departmentId,
                teamId,
                worklogId,
                NotificationType.WORKLOG_DUE_SOON,
                title,
                content
        );
    }

    /**
     * 당일 마감 업무 알림도 같은 업무 reminder identity 를 사용해 D-3 알림 row 와 이어지게 한다.
     */
    public static Notification createWorklogDueTodayReminder(Long userId,
                                                             Long departmentId,
                                                             Long teamId,
                                                             Long worklogId,
                                                             String title,
                                                             String content) {
        return createWorklogReminder(
                userId,
                departmentId,
                teamId,
                worklogId,
                NotificationType.WORKLOG_DUE_TODAY,
                title,
                content
        );
    }

    /**
     * 마감 초과 업무 알림을 저장-only 배치 경로에서 생성해 실시간 전파와 분리한다.
     */
    public static Notification createWorklogOverdueReminder(Long userId,
                                                            Long departmentId,
                                                            Long teamId,
                                                            Long worklogId,
                                                            String title,
                                                            String content) {
        return createWorklogReminder(
                userId,
                departmentId,
                teamId,
                worklogId,
                NotificationType.WORKLOG_OVERDUE,
                title,
                content
        );
    }

    /**
     * 선행 업무 완료 알림은 부모 업무 기준으로 한 번만 재발행되도록 WORKLOG 참조 identity 를 부모 ID 로 고정한다.
     */
    public static Notification createWorklogDependencyReady(Long userId,
                                                            Long departmentId,
                                                            Long teamId,
                                                            Long parentWorklogId,
                                                            String title,
                                                            String content) {
        return createWorklogReminder(
                userId,
                departmentId,
                teamId,
                parentWorklogId,
                NotificationType.WORKLOG_DEPENDENCY_READY,
                title,
                content
        );
    }

    /**
     * 업무 reminder 계열이 같은 중복 방지 identity 와 WORKLOG 참조 계약을 공유하게 한다.
     */
    private static Notification createWorklogReminder(Long userId,
                                                      Long departmentId,
                                                      Long teamId,
                                                      Long worklogId,
                                                      NotificationType notificationType,
                                                      String title,
                                                      String content) {
        Notification notification = new Notification();
        notification.userId = userId;
        notification.departmentId = departmentId;
        notification.teamId = teamId;
        notification.notificationType = notificationType.name();
        notification.title = title;
        notification.content = content;
        notification.referenceType = NotificationReferenceType.WORKLOG.name();
        notification.referenceId = worklogId;
        notification.isRead = Boolean.FALSE;
        notification.readAt = null;
        return notification;
    }

    /**
     * 기존 업무 알림의 최초 생성 이력은 보존하되, 미해결 마감 상태를 다시 확인하도록 읽음 이력을 초기화한다.
     */
    public void updateWorklogDeadlineReminder(NotificationType notificationType,
                                              String title,
                                              String content) {
        this.notificationType = notificationType.name();
        this.title = title;
        this.content = content;
        this.isRead = Boolean.FALSE;
        this.readAt = null;
    }

    /**
     * 신규 알림을 생성한다.
     * 읽음 상태가 생략되면 DB 기본값과 같은 false 로 보정하고, 읽지 않은 알림이면 readAt 은 null 로 전달한다.
     */
    public static Notification create(Long userId,
                                      Long departmentId,
                                      Long teamId,
                                      String notificationType,
                                      String title,
                                      String content,
                                      String referenceType,
                                      Long referenceId,
                                      Boolean isRead,
                                      LocalDateTime readAt) {
        Notification notification = new Notification();
        notification.userId = userId;
        notification.departmentId = departmentId;
        notification.teamId = teamId;
        notification.notificationType = notificationType;
        notification.title = title;
        notification.content = content;
        notification.referenceType = referenceType;
        notification.referenceId = referenceId;
        notification.isRead = Boolean.TRUE.equals(isRead);
        notification.readAt = readAt;
        return notification;
    }

    /**
     * 최초 읽음 시각을 보존해 같은 알림을 반복 확인해도 사용자 읽음 이력이 덮어써지지 않게 한다.
     */
    public void markAsRead(LocalDateTime readAt) {
        if (Boolean.TRUE.equals(isRead)) {
            return;
        }
        this.isRead = Boolean.TRUE;
        this.readAt = readAt;
    }
}
