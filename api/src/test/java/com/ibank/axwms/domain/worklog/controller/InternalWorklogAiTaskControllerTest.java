package com.ibank.axwms.domain.worklog.controller;

import com.ibank.axwms.domain.worklog.dto.AiTaskApiDto;
import com.ibank.axwms.domain.worklog.external.AiWorklogLightIndexProperties;
import com.ibank.axwms.domain.worklog.repository.WorklogAiTaskRepository;
import com.ibank.axwms.domain.worklog.service.InternalWorklogAiTaskResultService;
import com.ibank.axwms.domain.worklog.service.InternalWorklogAiTaskStartService;
import com.ibank.axwms.global.error.GlobalExceptionHandler;
import com.ibank.axwms.global.response.GlobalResponseAdvice;
import java.time.Duration;
import java.util.Optional;
import java.util.Map;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

class InternalWorklogAiTaskControllerTest {
    private final WorklogAiTaskRepository repository = mock(WorklogAiTaskRepository.class);
    private final InternalWorklogAiTaskResultService resultService = mock(InternalWorklogAiTaskResultService.class);
    private final InternalWorklogAiTaskStartService startService = mock(InternalWorklogAiTaskStartService.class);
    private static final String ID = "\"requestId\":\"00000000-0000-0000-0000-000000000001\","
            + "\"executionId\":\"00000000-0000-0000-0000-000000000002\","
            + "\"inputHash\":\"" + "a".repeat(64) + "\",\"taskKind\":\"SUMMARY\",\"targetId\":501";
    private static final String TOKEN = ",\"token\":\"00000000-0000-0000-0000-000000000003\"";
    private static final String PROVIDER = ",\"operationKey\":\"" + "b".repeat(64)
            + "\",\"reservationId\":\"00000000-0000-0000-0000-000000000004\"";



    @Test
    void invalidBodyDoesNotRevealValidationBeforeInternalTokenCheck() throws Exception {
        for (String operation : new String[]{"start", "attempt-result", "result"}) {
            for (String body : new String[]{"{}", "{"}) {
                mvc("secret").perform(request(operation, body, "wrong"))
                        .andExpect(status().isForbidden())
                        .andExpect(jsonPath("$.error.code").value("AUTH_ACCESS_DENIED"));
                mvc("secret").perform(post("/internal/worklogs/501/ai-tasks/" + operation)
                                .contentType(MediaType.APPLICATION_JSON).content(body))
                        .andExpect(status().isForbidden());
                mvc("secret").perform(request(operation, body, "secret"))
                        .andExpect(status().isBadRequest());
            }
        }
        verifyNoInteractions(repository, resultService, startService);
    }





    @Test
    void successfulCallsReturnUnwrappedContracts() throws Exception {
        when(startService.start(eq(501L), any())).thenReturn(Optional.of(UUID.randomUUID()));
        when(repository.retryableFailure(eq(501L), any(), eq("TimeoutError")))
                .thenReturn(new AiTaskApiDto.TransitionResponse(true, true));
        when(resultService.apply(eq(501L), any())).thenReturn(true);
        mvc("secret").perform(request("start", "{" + ID + "}", "secret"))
                .andExpect(status().isOk()).andExpect(jsonPath("$.execute").value(true))
                .andExpect(jsonPath("$.token").doesNotExist());
        mvc("secret").perform(request("attempt-result", "{" + ID
                + ",\"retryable\":true,\"errorCode\":\"TimeoutError\"}", "secret"))
                .andExpect(status().isOk()).andExpect(jsonPath("$.retryScheduled").value(true));
        mvc("secret").perform(request("result", "{" + ID
                + ",\"status\":\"COMPLETED\",\"aiSummary\":\"요약\"}", "secret"))
                .andExpect(status().isOk()).andExpect(jsonPath("$.accepted").value(true));
    }

    @Test
    void endpointsRequireConfiguredMatchingToken() throws Exception {
        for (String operation : new String[]{"start", "attempt-result", "result"}) {
            String body = "{" + ID + TOKEN + (operation.equals("provider-attempt") ? PROVIDER : "")
                    + (operation.equals("attempt-result") ? ",\"retryable\":true" : "")
                    + (operation.equals("result") ? ",\"status\":\"FAILED\"" : "") + "}";
            for (String configured : new String[]{"secret", "", " "}) {
                for (String sent : new String[]{"", "wrong"}) {
                    mvc(configured).perform(request(operation, body, sent)).andExpect(status().isForbidden());
                }
            }
        }
        verifyNoInteractions(repository);
        verifyNoInteractions(resultService);
        verifyNoInteractions(startService);
    }



    /** 실제 MVC 검증과 내부 응답 형식은 유지하고 데이터베이스만 대체한다. */
    private MockMvc mvc(String token) {
        return MockMvcBuilders.standaloneSetup(new InternalWorklogAiTaskController(repository, resultService, startService,
                new AiWorklogLightIndexProperties("http://ai/ai", Duration.ofSeconds(1), Duration.ofSeconds(1), token)))
                .setControllerAdvice(new GlobalExceptionHandler(), new GlobalResponseAdvice()).build();
    }

    /** 각 테스트가 동일한 내부 호출 경계로 요청하도록 구성한다. */
    private org.springframework.test.web.servlet.request.MockHttpServletRequestBuilder request(String operation, String body, String token) {
        return post("/internal/worklogs/501/ai-tasks/" + operation)
                .header("X-AI-Internal-Token", token).contentType(MediaType.APPLICATION_JSON).content(body);
    }
}
