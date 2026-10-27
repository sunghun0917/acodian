package com.ibank.axwms.domain.worklog.external;

import com.ibank.axwms.domain.worklog.dto.TriggerWorklogLightIndexDto;
import com.ibank.axwms.domain.worklog.dto.TriggerWorklogLightDeleteDto;
import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.Test;
import static org.assertj.core.api.Assertions.*;

class WorklogLightIndexClientTest {
    private static final UUID REQUEST = new UUID(0, 1);
    private static final String ACCEPTED = "{\"items\":[{\"worklogId\":501,\"requestId\":\"" + REQUEST + "\",\"taskId\":\"task-1\"}]}";

    @Test
    void rejects_duplicate_task_ids_for_distinct_worklogs() throws Exception {
        String body = "{\"items\":[{\"worklogId\":501,\"requestId\":\"" + REQUEST
                + "\",\"taskId\":\"same\"},{\"worklogId\":502,\"requestId\":\"" + REQUEST + "\",\"taskId\":\"same\"}]}";
        HttpServer server = server("index", 202, body, new AtomicReference<>(), new AtomicReference<>());
        try {
            assertThatThrownBy(() -> client(server, "secret").requestIndex(
                    new TriggerWorklogLightIndexDto.Request(java.util.List.of(501L, 502L), REQUEST)))
                    .isInstanceOf(IllegalStateException.class);
        } finally { server.stop(0); }
    }

    @Test
    void accepts_202_and_rejects_synchronous_200() throws Exception {
        for (String operation : new String[]{"index", "reindex", "delete"}) {
            for (int status : new int[]{202, 200}) {
                AtomicReference<String> body = new AtomicReference<>();
                AtomicReference<String> token = new AtomicReference<>();
                HttpServer server = server(operation, status, ACCEPTED, body, token);
                try {
                    var client = client(server, "secret");
                    if (status == 202) {
                        send(client, operation);
                        assertThat(body.get()).isEqualTo("{\"worklogIds\":[501],\"requestId\":\"" + REQUEST + "\"}");
                        assertThat(token.get()).isEqualTo("secret");
                    } else {
                        assertThatThrownBy(() -> send(client, operation)).isInstanceOf(IllegalStateException.class);
                    }
                } finally { server.stop(0); }
            }
        }
    }

    @Test
    void rejects_missing_duplicate_wrong_request_and_blank_task() throws Exception {
        for (String response : new String[]{"{\"items\":[]}", "{\"items\":[null]}",
                ACCEPTED.replace("task-1", " "), ACCEPTED.replace(REQUEST.toString(), new UUID(0, 2).toString()),
                ACCEPTED.replace("501", "502"), ACCEPTED.replace("}]", "},{\"worklogId\":501}]"),
                "{\"items\":[{\"worklogId\":501,\"indexed\":true}]}", "{}"}) {
            HttpServer server = server("index", 202, response, new AtomicReference<>(), new AtomicReference<>());
            try {
                assertThatThrownBy(() -> send(client(server, "secret"), "index"))
                        .isInstanceOf(IllegalStateException.class);
            } finally { server.stop(0); }
        }
    }

    @Test
    void rejects_missing_token_before_network_call() {
        for (String token : new String[]{null, "", " "}) {
            for (String operation : new String[]{"index", "reindex", "delete"}) {
                var client = new WorklogLightIndexClient(new AiWorklogLightIndexProperties(true, true, true,
                        "http://localhost:1/ai", Duration.ofSeconds(1), Duration.ofSeconds(1), token));
                assertThatThrownBy(() -> send(client, operation)).isInstanceOf(IllegalStateException.class);
            }
        }
    }

    @Test
    void propagates_broker_and_authentication_failure() throws Exception {
        for (int status : new int[]{403, 503}) {
            HttpServer server = server("index", status, "{}", new AtomicReference<>(), new AtomicReference<>());
            try {
                assertThatThrownBy(() -> send(client(server, "secret"), "index"))
                        .isInstanceOf(org.springframework.web.client.RestClientResponseException.class);
            } finally { server.stop(0); }
        }
    }

    /** 상태 코드와 본문을 실제 HTTP로 검증하여 transport 성공과 접수를 구분한다. */
    private HttpServer server(String operation, int status, String response, AtomicReference<String> body,
            AtomicReference<String> token) throws Exception {
        HttpServer server = HttpServer.create(new InetSocketAddress(0), 0);
        server.createContext("/ai/light/worklogs-v4/" + operation, exchange -> {
            body.set(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
            token.set(exchange.getRequestHeaders().getFirst("X-AI-Internal-Token"));
            byte[] bytes = response.getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().set("Content-Type", "application/json");
            exchange.sendResponseHeaders(status, bytes.length);
            exchange.getResponseBody().write(bytes);
            exchange.close();
        });
        server.start();
        return server;
    }

    /** 서버별 포트로 격리해 외부 AI 서버 없이 계약을 확인한다. */
    private WorklogLightIndexClient client(HttpServer server, String token) {
        return new WorklogLightIndexClient(new AiWorklogLightIndexProperties(true, true, true,
                "http://localhost:" + server.getAddress().getPort() + "/ai", Duration.ofSeconds(1), Duration.ofSeconds(1), token));
    }

    /** 세 작업 모두 같은 접수 검증을 사용하는지 확인한다. */
    private void send(WorklogLightIndexClient client, String operation) {
        switch (operation) {
            case "delete" -> client.requestDelete(TriggerWorklogLightDeleteDto.Request.of(501L, REQUEST));
            case "reindex" -> client.requestReindex(TriggerWorklogLightIndexDto.Request.of(501L, REQUEST));
            default -> client.requestIndex(TriggerWorklogLightIndexDto.Request.of(501L, REQUEST));
        }
    }
}
