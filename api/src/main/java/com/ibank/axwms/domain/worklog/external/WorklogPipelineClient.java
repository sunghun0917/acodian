package com.ibank.axwms.domain.worklog.external;

import com.ibank.axwms.domain.worklog.dto.TriggerWorklogPipelineDto;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;

/**
 * AI 서버에 업무일지 본문 요약과 태그 생성 파이프라인 실행을 요청한다.
 * 결과 반영은 AI 서버가 내부 콜백 API 를 다시 호출하는 비동기 계약이다.
 */
@Component
public class WorklogPipelineClient {

    private static final String WORKLOG_PIPELINE_PATH = "/pipeline/worklog";

    private final RestClient restClient;
    private final String internalToken;

    public WorklogPipelineClient(AiWorklogPipelineProperties properties) {
        this.internalToken = properties.internalToken();
        this.restClient = RestClient.builder()
                .baseUrl(properties.baseUrl())
                .requestFactory(requestFactory(properties))
                .build();
    }

    /** 식별된 요청은 동일한 작업 하나가 202로 접수됐을 때만 전달 성공으로 인정한다. */
    public void requestPipeline(TriggerWorklogPipelineDto.Request request) {
        if (internalToken == null || internalToken.isBlank() || request.requestId() == null || request.worklogId() == null || request.worklogId() <= 0
                || request.inputHash() == null
                || !request.inputHash().matches("[0-9a-f]{64}")
                || !("SUMMARY".equals(request.taskKind()) || "TAG".equals(request.taskKind()))) {
            throw new IllegalStateException("AI 작업 식별자가 올바르지 않습니다");
        }
        var response = restClient.post().uri(WORKLOG_PIPELINE_PATH).header("X-AI-Internal-Token", internalToken).body(request).retrieve()
                .toEntity(TriggerWorklogPipelineDto.Response.class);
        var body = response.getBody();
        if (response.getStatusCode().value() != 202 || body == null
                || !request.worklogId().equals(body.worklogId()) || !request.requestId().equals(body.requestId())
                || !"ACCEPTED".equals(body.status()) || !isSelectedTaskAccepted(request.taskKind(), body)) {
            throw new IllegalStateException("AI 작업 접수 결과가 실패 또는 누락되었습니다");
        }
    }

    /** 선택한 작업만 큐에 등록됐음을 응답의 task ID로 확인한다. */
    private boolean isSelectedTaskAccepted(String kind, TriggerWorklogPipelineDto.Response response) {
        String selected = "SUMMARY".equals(kind) ? response.summaryTaskId() : response.taggingTaskId();
        String other = "SUMMARY".equals(kind) ? response.taggingTaskId() : response.summaryTaskId();
        return selected != null && !selected.isBlank() && other == null;
    }

    /** AI 장애가 등록 후처리 경로를 오래 붙잡지 않도록 connect/read timeout 을 짧게 제한한다. */
    private static SimpleClientHttpRequestFactory requestFactory(AiWorklogPipelineProperties properties) {
        SimpleClientHttpRequestFactory factory = new SimpleClientHttpRequestFactory();
        factory.setConnectTimeout(properties.connectTimeout());
        factory.setReadTimeout(properties.readTimeout());
        return factory;
    }
}
