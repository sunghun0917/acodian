package com.ibank.axwms.domain.worklog.external;

import com.ibank.axwms.domain.worklog.dto.TriggerWorklogLightIndexDto;
import com.ibank.axwms.domain.worklog.dto.TriggerWorklogLightDeleteDto;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;

/**
 * AI 서버의 Light v3 업무일지 index endpoint 에 색인 요청을 전달한다.
 */
@Component
public class WorklogLightIndexClient {

    private static final String WORKLOG_LIGHT_INDEX_PATH = "/light/worklogs-v3/index";
    private static final String WORKLOG_LIGHT_REINDEX_PATH = "/light/worklogs-v3/reindex";
    private static final String WORKLOG_LIGHT_DELETE_PATH = "/light/worklogs-v3/delete";

    private final RestClient restClient;
    private final String internalToken;

    public WorklogLightIndexClient(AiWorklogLightIndexProperties properties) {
        this.internalToken = properties.internalToken();
        this.restClient = RestClient.builder()
                .baseUrl(properties.baseUrl())
                .requestFactory(requestFactory(properties))
                .build();
    }

    /** 내부 토큰이 설정된 경우에만 AI 삭제 결과의 ID별 성공을 확인한다. */
    public void requestDelete(TriggerWorklogLightDeleteDto.Request request) {
        if (internalToken == null || internalToken.isBlank()) {
            throw new IllegalStateException("AI 내부 토큰이 없어 업무일지 삭제를 요청할 수 없습니다");
        }
        TriggerWorklogLightDeleteDto.Response response = restClient.post()
                .uri(WORKLOG_LIGHT_DELETE_PATH)
                .header("X-AI-Internal-Token", internalToken)
                .body(request)
                .retrieve()
                .body(TriggerWorklogLightDeleteDto.Response.class);
        if (response == null || response.items() == null || response.items().size() != request.worklogIds().size()
                || request.worklogIds().stream().anyMatch(id -> response.items().stream()
                .filter(item -> id.equals(item.worklogId()) && item.deleted()).count() != 1)) {
            throw new IllegalStateException("AI Light 업무일지 삭제 결과가 실패 또는 누락되었습니다");
        }
    }

    /** HTTP 성공이어도 ID별 색인 실패나 결과 누락이면 후처리 실패로 전달한다. */
    public void requestIndex(TriggerWorklogLightIndexDto.Request request) {
        TriggerWorklogLightIndexDto.Response response = restClient.post()
                .uri(WORKLOG_LIGHT_INDEX_PATH)
                .body(request)
                .retrieve()
                .body(TriggerWorklogLightIndexDto.Response.class);
        if (response == null || response.items() == null || response.items().size() != request.worklogIds().size()
                || request.worklogIds().stream().anyMatch(id -> response.items().stream()
                .filter(item -> id.equals(item.worklogId()) && item.indexed()).count() != 1)) {
            throw new IllegalStateException("AI Light 업무일지 색인 결과가 실패 또는 누락되었습니다");
        }
    }

    /** 수정 경로는 보호된 교체 endpoint 만 호출하고 ID별 성공을 확인한다. */
    public void requestReindex(TriggerWorklogLightIndexDto.Request request) {
        if (internalToken == null || internalToken.isBlank()) {
            throw new IllegalStateException("AI 내부 토큰이 없어 업무일지 재색인을 요청할 수 없습니다");
        }
        TriggerWorklogLightIndexDto.Response response = restClient.post()
                .uri(WORKLOG_LIGHT_REINDEX_PATH)
                .header("X-AI-Internal-Token", internalToken)
                .body(request)
                .retrieve()
                .body(TriggerWorklogLightIndexDto.Response.class);
        if (response == null || response.items() == null || response.items().size() != request.worklogIds().size()
                || request.worklogIds().stream().anyMatch(id -> response.items().stream()
                .filter(item -> id.equals(item.worklogId()) && item.indexed()).count() != 1)) {
            throw new IllegalStateException("AI Light 업무일지 재색인 결과가 실패 또는 누락되었습니다");
        }
    }

    /** AI index 장애가 생성 후처리 경로를 오래 붙잡지 않도록 connect/read timeout 을 짧게 제한한다. */
    private static SimpleClientHttpRequestFactory requestFactory(AiWorklogLightIndexProperties properties) {
        SimpleClientHttpRequestFactory factory = new SimpleClientHttpRequestFactory();
        factory.setConnectTimeout(properties.connectTimeout());
        factory.setReadTimeout(properties.readTimeout());
        return factory;
    }
}
