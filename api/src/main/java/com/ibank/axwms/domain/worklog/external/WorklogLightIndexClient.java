package com.ibank.axwms.domain.worklog.external;

import com.ibank.axwms.domain.worklog.dto.TriggerWorklogLightIndexDto;
import com.ibank.axwms.domain.worklog.dto.TriggerWorklogLightDeleteDto;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;

/**
 * AI 서버의 Light v4 업무일지 index endpoint 에 색인 요청을 전달한다.
 */
@Component
public class WorklogLightIndexClient {

    private static final String WORKLOG_LIGHT_INDEX_PATH = "/light/worklogs-v4/index";
    private static final String WORKLOG_LIGHT_REINDEX_PATH = "/light/worklogs-v4/reindex";
    private static final String WORKLOG_LIGHT_DELETE_PATH = "/light/worklogs-v4/delete";

    private final RestClient restClient;
    private final String internalToken;

    public WorklogLightIndexClient(AiWorklogLightIndexProperties properties) {
        this.internalToken = properties.internalToken();
        this.restClient = RestClient.builder()
                .baseUrl(properties.baseUrl())
                .requestFactory(requestFactory(properties))
                .build();
    }

    /** 삭제 접수를 확인하며 실행 결과는 별도 콜백으로 반영한다. */
    public void requestDelete(TriggerWorklogLightDeleteDto.Request request) {
        request(WORKLOG_LIGHT_DELETE_PATH, request, request.worklogIds(), request.requestId());
    }

    /** 최초 색인은 202 및 건별 브로커 발행 확인이 있어야 전달 완료로 인정한다. */
    public void requestIndex(TriggerWorklogLightIndexDto.Request request) {
        request(WORKLOG_LIGHT_INDEX_PATH, request, request.worklogIds(), request.requestId());
    }

    /** 재색인도 최초 색인과 동일한 비동기 접수 계약을 적용한다. */
    public void requestReindex(TriggerWorklogLightIndexDto.Request request) {
        request(WORKLOG_LIGHT_REINDEX_PATH, request, request.worklogIds(), request.requestId());
    }

    /** 동기 200·누락·중복·다른 요청 응답을 전달 성공으로 오인하지 않는다. */
    private void request(String path, Object body, java.util.List<Long> ids, java.util.UUID requestId) {
        if (internalToken == null || internalToken.isBlank() || requestId == null) {
            throw new IllegalStateException("AI 내부 토큰과 요청 식별자가 필요합니다");
        }
        var response = restClient.post().uri(path)
                .header("X-AI-Internal-Token", internalToken).body(body).retrieve()
                .toEntity(TriggerWorklogLightIndexDto.Response.class);
        var accepted = response.getBody();
        if (response.getStatusCode().value() != 202 || accepted == null || accepted.items() == null
                || accepted.items().size() != ids.size()
                || accepted.items().stream().filter(java.util.Objects::nonNull)
                .map(TriggerWorklogLightIndexDto.Item::taskId).distinct().count() != ids.size()
                || ids.stream().anyMatch(id -> accepted.items().stream().filter(item -> item != null
                && id.equals(item.worklogId()) && requestId.equals(item.requestId())
                && item.taskId() != null && !item.taskId().isBlank()).count() != 1)) {
            throw new IllegalStateException("AI Light 업무일지 접수 결과가 실패 또는 누락되었습니다");
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
