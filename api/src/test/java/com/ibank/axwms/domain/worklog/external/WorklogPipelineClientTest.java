package com.ibank.axwms.domain.worklog.external;

import static org.assertj.core.api.Assertions.assertThatCode;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.ibank.axwms.domain.worklog.dto.TriggerWorklogPipelineDto;
import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.Test;

class WorklogPipelineClientTest {
    private static final UUID ID = UUID.randomUUID();

    @Test
    void targetedRequestWithoutCompleteIdentityFailsBeforeNetwork() {
        var client = new WorklogPipelineClient(new AiWorklogPipelineProperties(true,
                "http://localhost:1/ai", Duration.ofSeconds(1), Duration.ofSeconds(1)));
        assertThatThrownBy(() -> client.requestPipeline(new TriggerWorklogPipelineDto.Request(1L, "요청", "업무", 2L, 3L, 4L, "SUMMARY"))).isInstanceOf(IllegalStateException.class);
    }

    @Test
    void identifiedTaskRequiresMatching202AndSelectedTaskId() throws Exception {
        String valid = "{\"worklogId\":1,\"requestId\":\"" + ID
                + "\",\"summaryTaskId\":\"celery-1\",\"taggingTaskId\":null,\"status\":\"ACCEPTED\"}";
        for (String body : new String[]{valid.replace(ID.toString(), UUID.randomUUID().toString()),
                valid.replace("celery-1", " "), valid.replace("\"worklogId\":1", "\"worklogId\":2"),
                valid.replace("\"taggingTaskId\":null", "\"taggingTaskId\":\"unexpected\""), "{}"}) {
            assertThatThrownBy(() -> send(202, body)).isInstanceOf(IllegalStateException.class);
        }
        assertThatThrownBy(() -> send(200, valid)).isInstanceOf(IllegalStateException.class);
        assertThatCode(() -> send(202, valid)).doesNotThrowAnyException();
    }

    /** 원격 접수 상태와 본문을 실제 HTTP로 검증한다. */
    private void send(int status, String body) throws Exception {
        HttpServer server = HttpServer.create(new InetSocketAddress(0), 0);
        AtomicReference<String> token = new AtomicReference<>();
        server.createContext("/ai/pipeline/worklog", exchange -> {
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
            var client = new WorklogPipelineClient(new AiWorklogPipelineProperties(true,
                    "http://localhost:" + server.getAddress().getPort() + "/ai", Duration.ofSeconds(1), Duration.ofSeconds(1), "secret"));
            try {
                client.requestPipeline(new TriggerWorklogPipelineDto.Request(1L, "요청", "업무", 2L, 3L, 4L, "SUMMARY", ID, "a".repeat(64)));
            } finally {
                org.assertj.core.api.Assertions.assertThat(token.get()).isEqualTo("secret");
            }
        } finally {
            server.stop(0);
        }
    }
}
