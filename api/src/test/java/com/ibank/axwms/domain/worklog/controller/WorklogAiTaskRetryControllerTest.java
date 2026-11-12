package com.ibank.axwms.domain.worklog.controller;

import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

import com.ibank.axwms.domain.worklog.service.WorklogAiTaskRetryService;
import com.ibank.axwms.global.error.GlobalExceptionHandler;
import com.ibank.axwms.global.response.GlobalResponseAdvice;
import com.ibank.axwms.global.security.CustomUserPrincipal;
import java.util.UUID;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.springframework.security.authentication.TestingAuthenticationToken;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.security.web.method.annotation.AuthenticationPrincipalArgumentResolver;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

class WorklogAiTaskRetryControllerTest {
    private final WorklogAiTaskRetryService service = mock(WorklogAiTaskRetryService.class);
    private final CustomUserPrincipal principal = new CustomUserPrincipal(1L, "test@example.com", "MEMBER");

    @AfterEach
    void clearSecurity() {
        SecurityContextHolder.clearContext();
    }

    @Test
    void retryReturnsNewRequestAndRejectsInvalidId() throws Exception {
        UUID oldId = UUID.randomUUID();
        UUID newId = UUID.randomUUID();
        SecurityContextHolder.getContext().setAuthentication(new TestingAuthenticationToken(principal, null, "ROLE_MEMBER"));
        when(service.retry(principal, 10L, oldId)).thenReturn(newId);
        var mvc = MockMvcBuilders.standaloneSetup(new WorklogAiTaskRetryController(service))
                .setCustomArgumentResolvers(new AuthenticationPrincipalArgumentResolver())
                .setControllerAdvice(new GlobalExceptionHandler(), new GlobalResponseAdvice()).build();
        mvc.perform(post("/worklogs/10/ai-tasks/" + oldId + "/retry"))
                .andExpect(status().isOk()).andExpect(jsonPath("$.data.requestId").value(newId.toString()));
        mvc.perform(post("/worklogs/10/ai-tasks/bad/retry")).andExpect(status().isBadRequest());
        verify(service).retry(principal, 10L, oldId);
        verifyNoMoreInteractions(service);
    }
}
