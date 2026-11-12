package com.ibank.axwms.domain.worklog.config;

import java.time.Duration;
import java.time.temporal.ChronoUnit;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.boot.convert.DurationUnit;

/** Outbox의 단일 시도 한도와 Celery hard limit을 덮는 결과 대기 lease를 제공한다. */
@ConfigurationProperties(prefix = "ai.task-execution-budget")
public record AiTaskExecutionBudgetProperties(
        @DurationUnit(ChronoUnit.SECONDS) Duration hardLimit, Integer maxAttempts) {
    /** 누락 설정에만 기본값을 적용하며 무한 재시도나 즉시 만료되는 lease는 거절한다. */
    public AiTaskExecutionBudgetProperties {
        hardLimit = hardLimit == null ? Duration.ofSeconds(540) : hardLimit;
        maxAttempts = maxAttempts == null ? 4 : maxAttempts;
        if (!hardLimit.isPositive() || maxAttempts < 1) {
            throw new IllegalArgumentException("AI task timeout and attempts must be positive");
        }
    }

    /** 큐 대기와 결과 전달 여유를 포함해 선점부터 종결까지 하나의 기한만 사용한다. */
    public Duration timeout() {
        return hardLimit.plusSeconds(60);
    }
}
