package com.ibank.axwms.domain.file.dto;

import io.swagger.v3.oas.annotations.media.Schema;
import java.util.UUID;

/**
 * AI 서버 {@code POST /summarize/files} 트리거 페이로드.
 * 콜백 식별자 회수(fileId)와 객체 다운로드 키(storageKey), 확장자/원본명을 함께 전달한다.
 */
public final class TriggerFileSummaryDto {

    @Schema(description = "AI 파일 요약 트리거 요청")
    public record Request(

            @Schema(description = "요약 대상 파일 ID")
            Long fileId,

            @Schema(description = "파일이 속한 업무일지 ID")
            Long worklogId,

            @Schema(description = "객체 스토리지 키 (다운로드용)")
            String storageKey,

            @Schema(description = "원본 파일명")
            String originalName,

            @Schema(description = "파일 확장자 (소문자, dot 제외)")
            String fileExtension,
            UUID requestId,
            String inputHash
    ) {
        /** 기존 통합 등록 경로는 식별자를 제공하지 않는다. */
        public Request(Long fileId, Long worklogId, String storageKey, String originalName, String fileExtension) {
            this(fileId, worklogId, storageKey, originalName, fileExtension, null, null);
        }
    }

    /** 파일별 큐 접수를 동일 요청으로 확인하기 위한 응답이다. */
    public record Response(Long fileId, UUID requestId, String summaryTaskId, String status) {}
}
