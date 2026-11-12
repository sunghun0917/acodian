package com.ibank.axwms.domain.file.external;

import static org.assertj.core.api.Assertions.*;

import com.ibank.axwms.domain.file.dto.TriggerFileSummaryDto;
import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.Test;

class FileSummaryClientTest {
    @Test
    void identifiedFileRequiresMatchingAcceptedResponse() throws Exception {
        UUID requestId = UUID.randomUUID();
        String body = "{\"fileId\":9,\"requestId\":\"" + requestId
                + "\",\"summaryTaskId\":\"task-1\",\"status\":\"ACCEPTED\"}";
        assertThatCode(() -> send(requestId, 202, body)).doesNotThrowAnyException();
        assertThatThrownBy(() -> send(requestId, 200, body)).isInstanceOf(IllegalStateException.class);
        assertThatThrownBy(() -> send(requestId, 202, body.replace(requestId.toString(), UUID.randomUUID().toString())))
                .isInstanceOf(IllegalStateException.class);
        assertThatThrownBy(() -> send(requestId, 202, body.replace("task-1", " "))).isInstanceOf(IllegalStateException.class);
    }

    /** 실 HTTP로 요청 식별자와 비동기 접수를 검증한다. */
    private void send(UUID id, int status, String body) throws Exception {
        HttpServer server = HttpServer.create(new InetSocketAddress(0), 0);
        AtomicReference<String> token = new AtomicReference<>();
        server.createContext("/ai/summarize/files", exchange -> {
            token.set(exchange.getRequestHeaders().getFirst("X-AI-Internal-Token"));
            exchange.getRequestBody().readAllBytes();
            byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().set("Content-Type", "application/json");
            exchange.sendResponseHeaders(status, bytes.length);
            exchange.getResponseBody().write(bytes);
            exchange.close();
        });
        server.start();
        try {
            var client = new FileSummaryClient(new AiFileSummaryProperties(true,
                    "http://localhost:" + server.getAddress().getPort() + "/ai", Duration.ofSeconds(1), Duration.ofSeconds(1), "secret"));
            try {
                client.requestSummary(new TriggerFileSummaryDto.Request(9L, 10L, "key", "x.pdf", "pdf", id, "a".repeat(64)));
            } finally {
                assertThat(token.get()).isEqualTo("secret");
            }
        } finally {
            server.stop(0);
        }
    }
}
