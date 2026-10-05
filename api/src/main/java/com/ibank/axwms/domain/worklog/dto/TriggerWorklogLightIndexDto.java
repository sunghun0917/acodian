package com.ibank.axwms.domain.worklog.dto;

import lombok.AccessLevel;
import lombok.NoArgsConstructor;

import java.util.List;

@NoArgsConstructor(access = AccessLevel.PRIVATE)
public final class TriggerWorklogLightIndexDto {

    /**
     * Light v3 index endpoint 가 여러 업무 ID 일괄 색인 계약만 제공하므로 단건 생성도 배열로 전달한다.
     */
    public record Request(
            List<Long> worklogIds
    ) {

        /** 단건 업무 생성 흐름에서 나온 ID 를 AI index 요청의 일괄 계약으로 감싼다. */
        public static Request of(Long worklogId) {
            return new Request(List.of(worklogId));
        }
    }

    /** AI가 HTTP 성공과 별도로 각 업무의 색인 실패를 보고하므로 결과를 ID별로 확인한다. */
    public record Response(List<Item> items) {
    }

    /** 각 ID의 indexed=false는 HTTP 성공과 관계없이 색인 실패를 뜻한다. */
    public record Item(Long worklogId, boolean indexed, String error) {
    }
}
