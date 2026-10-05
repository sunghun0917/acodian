package com.ibank.axwms.domain.worklog.dto;

import lombok.AccessLevel;
import lombok.NoArgsConstructor;

import java.util.List;

@NoArgsConstructor(access = AccessLevel.PRIVATE)
public final class TriggerWorklogLightDeleteDto {

    /** AI 삭제 endpoint의 일괄 계약으로 단건 업무 ID를 전달한다. */
    public record Request(List<Long> worklogIds) {
        /** 단건 삭제 요청을 AI의 일괄 요청 형태로 변환한다. */
        public static Request of(Long worklogId) {
            return new Request(List.of(worklogId));
        }
    }

    /** HTTP 성공과 개별 삭제 성공을 구분하기 위한 응답이다. */
    public record Response(List<Item> items) {
    }

    /** deleted=false면 AI 저장소에서 해당 문서 제거가 완료되지 않았다. */
    public record Item(Long worklogId, boolean deleted, String error) {
    }
}
