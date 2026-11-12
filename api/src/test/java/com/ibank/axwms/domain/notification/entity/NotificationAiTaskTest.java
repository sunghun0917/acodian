package com.ibank.axwms.domain.notification.entity;

import static org.assertj.core.api.Assertions.assertThat;

import java.util.UUID;
import org.junit.jupiter.api.Test;

class NotificationAiTaskTest {
    @Test
    void aiResultRetainsIndependentTaskIdentityAndRecipient() {
        UUID requestId = UUID.randomUUID();
        Notification notification = Notification.createAiTaskResult(1L, 2L, 3L, 4L,
                "업무 제목", requestId, "SUMMARY", 4L, "FAILED");
        assertThat(notification.getUserId()).isEqualTo(1L);
        assertThat(notification.getReferenceId()).isEqualTo(4L);
        assertThat(notification.getAiRequestId()).isEqualTo(requestId);
        assertThat(notification.getAiTaskKind()).isEqualTo("SUMMARY");
        assertThat(notification.getAiTargetId()).isEqualTo(4L);
        assertThat(notification.getAiResult()).isEqualTo("FAILED");
        assertThat(notification.getContent()).contains("업무 제목");
        assertThat(notification.getIsRead()).isFalse();
    }

}
