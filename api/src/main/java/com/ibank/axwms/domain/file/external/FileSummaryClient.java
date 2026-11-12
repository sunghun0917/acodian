package com.ibank.axwms.domain.file.external;

import com.ibank.axwms.domain.file.dto.TriggerFileSummaryDto;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;

/**
 * AI 서버에 파일 1건 요약을 요청한다. 호출은 fire-and-forget 시맨틱이며 응답 본문은 사용하지 않는다.
 * timeout 을 짧게 잡아 AI 장애가 사용자 요청 경로로 번지지 않도록 한다.
 */
@Component
public class FileSummaryClient {

    private static final String SUMMARIZE_FILES_PATH = "/summarize/files";

    private final RestClient restClient;
    private final String internalToken;

    public FileSummaryClient(AiFileSummaryProperties properties) {
        this.internalToken = properties.internalToken();
        this.restClient = RestClient.builder()
                .baseUrl(properties.baseUrl())
                .requestFactory(requestFactory(properties))
                .build();
    }

    /**
     * AI 서버에 파일 요약 요청을 던지고 응답은 무시한다.
     * 호출 자체는 트랜잭션 커밋 후 비동기 listener 에서 수행되므로 예외 처리는 호출측 책임이다.
     *
     * @param request 요약 대상 파일 식별자와 스토리지 키
     */
    public void requestSummary(TriggerFileSummaryDto.Request request) {
        if (internalToken == null || internalToken.isBlank() || request.requestId() == null || request.fileId() == null || request.fileId() <= 0
                || request.inputHash() == null
                || !request.inputHash().matches("[0-9a-f]{64}")) {
            throw new IllegalStateException("AI 파일 작업 식별자가 올바르지 않습니다");
        }
        var response = restClient.post().uri(SUMMARIZE_FILES_PATH).header("X-AI-Internal-Token", internalToken).body(request).retrieve()
                .toEntity(TriggerFileSummaryDto.Response.class);
        var body = response.getBody();
        if (response.getStatusCode().value() != 202 || body == null || !request.fileId().equals(body.fileId())
                || !request.requestId().equals(body.requestId()) || !"ACCEPTED".equals(body.status())
                || body.summaryTaskId() == null || body.summaryTaskId().isBlank()) {
            throw new IllegalStateException("AI 파일 접수 결과가 실패 또는 누락되었습니다");
        }
    }

    /** AI 장애가 콜백 이전 트리거 경로를 길게 잡아두지 않도록 connect/read timeout 을 짧게 제한한다. */
    private static SimpleClientHttpRequestFactory requestFactory(AiFileSummaryProperties properties) {
        SimpleClientHttpRequestFactory factory = new SimpleClientHttpRequestFactory();
        factory.setConnectTimeout(properties.connectTimeout());
        factory.setReadTimeout(properties.readTimeout());
        return factory;
    }
}
