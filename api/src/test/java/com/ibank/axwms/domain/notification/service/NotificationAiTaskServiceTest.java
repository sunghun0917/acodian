package com.ibank.axwms.domain.notification.service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.*;

import com.ibank.axwms.domain.notification.entity.Notification;
import com.ibank.axwms.domain.notification.event.NotificationCreatedEvent;
import com.ibank.axwms.domain.notification.repository.NotificationRepository;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.springframework.context.ApplicationEventPublisher;

class NotificationAiTaskServiceTest {
    @Test
    void persistsTaskSnapshotAndPublishesAfterCommitEvent() {
        NotificationRepository repository = mock(NotificationRepository.class);
        ApplicationEventPublisher publisher = mock(ApplicationEventPublisher.class);
        when(repository.saveAndFlush(any(Notification.class))).thenAnswer(invocation -> invocation.getArgument(0));
        NotificationService service = new NotificationService(repository, publisher);
        UUID requestId = UUID.randomUUID();

        Notification saved = service.createAiTaskResultNotification(1L, 2L, 3L, 4L,
                "업무 제목", requestId, "TAG", 4L, "COMPLETED");

        assertThat(saved.getAiRequestId()).isEqualTo(requestId);
        assertThat(saved.getAiResult()).isEqualTo("COMPLETED");
        verify(repository).saveAndFlush(saved);
        verify(publisher).publishEvent(any(NotificationCreatedEvent.class));
    }
}
