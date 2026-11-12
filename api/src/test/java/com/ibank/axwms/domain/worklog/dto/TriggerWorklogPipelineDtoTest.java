package com.ibank.axwms.domain.worklog.dto;

import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.ObjectMapper;
import java.util.UUID;
import org.junit.jupiter.api.Test;

class TriggerWorklogPipelineDtoTest {

    @Test
    void includesSelectedTaskKindInAiRequest() throws Exception {
        var request = new TriggerWorklogPipelineDto.Request(1L, "요청", "업무", 2L, 3L, 4L, "SUMMARY");

        var json = new ObjectMapper().readTree(new ObjectMapper().writeValueAsBytes(request));

        assertThat(json.get("taskKind").asText()).isEqualTo("SUMMARY");
    }

    @Test
    void includesCurrentTaskIdentityInAiRequest() throws Exception {
        UUID id = UUID.randomUUID();
        var request = new TriggerWorklogPipelineDto.Request(1L, "요청", "업무", 2L, 3L, 4L, "SUMMARY", id, "a".repeat(64));
        var json = new ObjectMapper().valueToTree(request);
        assertThat(json.get("requestId").asText()).isEqualTo(id.toString());
        assertThat(json.has("dispatchSeq")).isFalse();
        assertThat(json.get("inputHash").asText()).isEqualTo("a".repeat(64));
    }
}
