package com.ibank.axwms.domain.worklog.controller;

import com.ibank.axwms.domain.worklog.external.AiWorklogLightIndexProperties;
import com.ibank.axwms.domain.worklog.service.InternalWorklogLightIndexService;
import com.ibank.axwms.global.error.GlobalExceptionHandler;
import com.ibank.axwms.global.response.GlobalResponseAdvice;
import java.time.Duration;
import org.junit.jupiter.api.Test;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;
import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

class InternalWorklogLightIndexControllerTest {
    private final InternalWorklogLightIndexService service = mock(InternalWorklogLightIndexService.class);
    private static final String IDS = "\"requestId\":\"00000000-0000-0000-0000-000000000001\",\"executionId\":\"00000000-0000-0000-0000-000000000002\"";

    @Test
    void start_returns_execute_and_result_returns_empty_response() throws Exception {
        when(service.start(any(), any(), any())).thenReturn(true);
        mvc("secret").perform(post("/internal/worklogs/501/light-index/start")
                .header("X-AI-Internal-Token", "secret").contentType(MediaType.APPLICATION_JSON).content("{" + IDS + "}"))
                .andExpect(status().isOk()).andExpect(jsonPath("$.execute").value(true));
        mvc("secret").perform(post("/internal/worklogs/501/light-index/result")
                .header("X-AI-Internal-Token", "secret").contentType(MediaType.APPLICATION_JSON)
                .content("{" + IDS + ",\"status\":\"COMPLETED\",\"error\":null}"))
                .andExpect(status().isOk());
        verify(service).result(eq(501L), any(), any(), eq("COMPLETED"), isNull());
    }

    @Test
    void both_callbacks_require_matching_nonempty_configured_token() throws Exception {
        for (String operation : new String[]{"start", "result"}) {
            String body = "{" + IDS + (operation.equals("result") ? ",\"status\":\"FAILED\"" : "") + "}";
            for (String configured : new String[]{"secret", "", " "}) {
                for (String sent : new String[]{"", "wrong"}) {
                    var request = post("/internal/worklogs/501/light-index/" + operation)
                            .contentType(MediaType.APPLICATION_JSON).content(body);
                    if (!sent.isEmpty()) request.header("X-AI-Internal-Token", sent);
                    mvc(configured).perform(request).andExpect(status().isForbidden());
                }
            }
        }
        verifyNoInteractions(service);
    }

    @Test
    void rejects_missing_ids_invalid_uuid_and_nonterminal_status() throws Exception {
        for (String body : new String[]{"{}", "{\"requestId\":\"bad\"}", "{" + IDS + ",\"status\":\"RUNNING\"}"}) {
            mvc("secret").perform(post("/internal/worklogs/501/light-index/result")
                    .header("X-AI-Internal-Token", "secret").contentType(MediaType.APPLICATION_JSON).content(body))
                    .andExpect(status().isBadRequest());
        }
        verifyNoInteractions(service);
    }

    @Test
    void rejects_nonpositive_worklog_id() throws Exception {
        mvc("secret").perform(post("/internal/worklogs/0/light-index/start")
                .header("X-AI-Internal-Token", "secret").contentType(MediaType.APPLICATION_JSON)
                .content("{" + IDS + "}"))
                .andExpect(status().isBadRequest());
        verifyNoInteractions(service);
    }

    /** 실제 MVC 바인딩·검증·응답 봉투를 사용하고 외부 인프라만 생략한다. */
    private MockMvc mvc(String token) {
        return MockMvcBuilders.standaloneSetup(new InternalWorklogLightIndexController(service,
                new AiWorklogLightIndexProperties(true, true, true, "http://ai/ai", Duration.ofSeconds(1), Duration.ofSeconds(1), token)))
                .setControllerAdvice(new GlobalExceptionHandler(), new GlobalResponseAdvice()).build();
    }
}
