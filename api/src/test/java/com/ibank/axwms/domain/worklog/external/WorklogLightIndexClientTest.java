package com.ibank.axwms.domain.worklog.external;

import com.ibank.axwms.domain.worklog.dto.TriggerWorklogLightIndexDto;
import com.ibank.axwms.domain.worklog.dto.TriggerWorklogLightDeleteDto;
import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

class WorklogLightIndexClientTest {

    @Test
    @DisplayName("AI 삭제 요청은 내부 토큰과 worklogIds를 보내고 성공 항목을 확인한다")
    void delete_sends_token_and_validates_response() throws Exception {
        AtomicReference<String> token = new AtomicReference<>();
        AtomicReference<String> body = new AtomicReference<>();
        HttpServer server = HttpServer.create(new InetSocketAddress(0), 0);
        server.createContext("/ai/light/worklogs-v3/delete", exchange -> {
            token.set(exchange.getRequestHeaders().getFirst("X-AI-Internal-Token"));
            body.set(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
            byte[] response = "{\"items\":[{\"worklogId\":501,\"deleted\":true,\"error\":null}]}"
                    .getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().set("Content-Type", "application/json");
            exchange.sendResponseHeaders(200, response.length);
            exchange.getResponseBody().write(response);
            exchange.close();
        });
        server.start();
        try {
            WorklogLightIndexClient client = new WorklogLightIndexClient(new AiWorklogLightIndexProperties(
                    true, false, true, "http://localhost:" + server.getAddress().getPort() + "/ai",
                    Duration.ofSeconds(1), Duration.ofSeconds(1), "secret"));

            client.requestDelete(TriggerWorklogLightDeleteDto.Request.of(501L));

            assertThat(token.get()).isEqualTo("secret");
            assertThat(body.get()).isEqualTo("{\"worklogIds\":[501]}");
        } finally {
            server.stop(0);
        }
    }

    @Test
    @DisplayName("토큰 누락 또는 개별 삭제 실패는 성공으로 처리하지 않는다")
    void delete_rejects_missing_token_and_failed_item() throws Exception {
        WorklogLightIndexClient tokenless = new WorklogLightIndexClient(new AiWorklogLightIndexProperties(
                true, false, true, "http://localhost:8000/ai", Duration.ofSeconds(1), Duration.ofSeconds(1), null));
        assertThatThrownBy(() -> tokenless.requestDelete(TriggerWorklogLightDeleteDto.Request.of(501L)))
                .isInstanceOf(IllegalStateException.class);

        HttpServer server = HttpServer.create(new InetSocketAddress(0), 0);
        server.createContext("/ai/light/worklogs-v3/delete", exchange -> {
            exchange.getRequestBody().readAllBytes();
            byte[] response = "{\"items\":[{\"worklogId\":501,\"deleted\":false,\"error\":\"failure\"}]}"
                    .getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().set("Content-Type", "application/json");
            exchange.sendResponseHeaders(200, response.length);
            exchange.getResponseBody().write(response);
            exchange.close();
        });
        server.start();
        try {
            WorklogLightIndexClient client = new WorklogLightIndexClient(new AiWorklogLightIndexProperties(
                    true, false, true, "http://localhost:" + server.getAddress().getPort() + "/ai",
                    Duration.ofSeconds(1), Duration.ofSeconds(1), "secret"));
            assertThatThrownBy(() -> client.requestDelete(TriggerWorklogLightDeleteDto.Request.of(501L)))
                    .isInstanceOf(IllegalStateException.class);
        } finally {
            server.stop(0);
        }
    }

    @Test
    @DisplayName("Light v3 index endpoint에 worklogIds 배열 body를 POST로 전송한다")
    void post_worklog_ids_to_light_v3_index_endpoint() throws Exception {
        AtomicReference<String> method = new AtomicReference<>();
        AtomicReference<String> path = new AtomicReference<>();
        AtomicReference<String> body = new AtomicReference<>();
        CountDownLatch requestArrived = new CountDownLatch(1);
        HttpServer server = HttpServer.create(new InetSocketAddress(0), 0);
        server.createContext("/ai/light/worklogs-v3/index", exchange -> {
            method.set(exchange.getRequestMethod());
            path.set(exchange.getRequestURI().getPath());
            body.set(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
            byte[] response = "{\"items\":[{\"worklogId\":501,\"indexed\":true,\"error\":null}]}".getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().set("Content-Type", "application/json");
            exchange.sendResponseHeaders(200, response.length);
            exchange.getResponseBody().write(response);
            exchange.close();
            requestArrived.countDown();
        });
        server.start();

        try {
            int port = server.getAddress().getPort();
            WorklogLightIndexClient client = new WorklogLightIndexClient(new AiWorklogLightIndexProperties(
                    true,
                    false,
                    false,
                    "http://localhost:" + port + "/ai",
                    Duration.ofSeconds(1),
                    Duration.ofSeconds(1),
                    null
            ));

            client.requestIndex(TriggerWorklogLightIndexDto.Request.of(501L));

            assertThat(requestArrived.await(1, TimeUnit.SECONDS)).isTrue();
            assertThat(method.get()).isEqualTo("POST");
            assertThat(path.get()).isEqualTo("/ai/light/worklogs-v3/index");
            assertThat(body.get()).isEqualTo("{\"worklogIds\":[501]}");
        } finally {
            server.stop(0);
        }
    }

    @Test
    @DisplayName("HTTP 200이어도 해당 업무 색인이 실패하거나 결과가 누락되면 실패한다")
    void rejects_failed_or_missing_item() throws Exception {
        for (String body : new String[]{
                "{\"items\":[{\"worklogId\":501,\"indexed\":false,\"error\":\"failure\"}]}",
                "{\"items\":[]}",
                "{\"items\":[{\"worklogId\":501,\"indexed\":true},{\"worklogId\":501,\"indexed\":false}]}"}) {
            HttpServer server = HttpServer.create(new InetSocketAddress(0), 0);
            server.createContext("/ai/light/worklogs-v3/index", exchange -> {
                exchange.getRequestBody().readAllBytes();
                byte[] response = body.getBytes(StandardCharsets.UTF_8);
                exchange.getResponseHeaders().set("Content-Type", "application/json");
                exchange.sendResponseHeaders(200, response.length);
                exchange.getResponseBody().write(response);
                exchange.close();
            });
            server.start();
            try {
                WorklogLightIndexClient client = new WorklogLightIndexClient(new AiWorklogLightIndexProperties(
                        true, false, false, "http://localhost:" + server.getAddress().getPort() + "/ai",
                        Duration.ofSeconds(1), Duration.ofSeconds(1), null));
                assertThatThrownBy(() -> client.requestIndex(TriggerWorklogLightIndexDto.Request.of(501L)))
                        .isInstanceOf(IllegalStateException.class);
            } finally {
                server.stop(0);
            }
        }
    }

    @Test
    @DisplayName("수정 재색인은 보호된 reindex endpoint에 내부 토큰을 보내고 ID별 결과를 확인한다")
    void posts_reindex_with_internal_token() throws Exception {
        AtomicReference<String> token = new AtomicReference<>();
        AtomicReference<String> body = new AtomicReference<>();
        HttpServer server = HttpServer.create(new InetSocketAddress(0), 0);
        server.createContext("/ai/light/worklogs-v3/reindex", exchange -> {
            token.set(exchange.getRequestHeaders().getFirst("X-AI-Internal-Token"));
            body.set(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
            byte[] response = "{\"items\":[{\"worklogId\":501,\"indexed\":true}]}".getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().set("Content-Type", "application/json");
            exchange.sendResponseHeaders(200, response.length);
            exchange.getResponseBody().write(response);
            exchange.close();
        });
        server.start();
        try {
            WorklogLightIndexClient client = new WorklogLightIndexClient(new AiWorklogLightIndexProperties(
                    true, true, false, "http://localhost:" + server.getAddress().getPort() + "/ai",
                    Duration.ofSeconds(1), Duration.ofSeconds(1), "secret"));

            client.requestReindex(TriggerWorklogLightIndexDto.Request.of(501L));

            assertThat(token.get()).isEqualTo("secret");
            assertThat(body.get()).isEqualTo("{\"worklogIds\":[501]}");
        } finally {
            server.stop(0);
        }
    }

    @Test
    @DisplayName("수정 재색인은 내부 토큰이 없으면 네트워크 호출 전 실패한다")
    void reindex_requires_internal_token() {
        WorklogLightIndexClient client = new WorklogLightIndexClient(new AiWorklogLightIndexProperties(
                true, true, false, "http://localhost:1/ai",
                Duration.ofSeconds(1), Duration.ofSeconds(1), null));

        assertThatThrownBy(() -> client.requestReindex(TriggerWorklogLightIndexDto.Request.of(501L)))
                .isInstanceOf(IllegalStateException.class);
    }
}
