package com.ibank.axwms.domain.worklog.service;

import com.ibank.axwms.domain.worklog.dto.TriggerWorklogLightDeleteDto;
import com.ibank.axwms.domain.worklog.dto.TriggerWorklogLightIndexDto;
import com.ibank.axwms.domain.worklog.external.AiWorklogLightIndexProperties;
import com.ibank.axwms.domain.worklog.external.WorklogLightIndexClient;
import com.ibank.axwms.domain.worklog.repository.WorklogLightOutboxRepository;
import com.ibank.axwms.testsupport.IntegrationTestSupport;
import java.time.Duration;
import java.util.UUID;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.TestPropertySource;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.doNothing;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;

@TestPropertySource(properties = "ai.worklog-light-index.enabled=false")
class WorklogLightOutboxIntegrationTest extends IntegrationTestSupport {

    @Autowired
    private JdbcTemplate jdbcTemplate;

    @Autowired
    private WorklogLightOutboxRepository repository;

    private final WorklogLightIndexClient client = mock(WorklogLightIndexClient.class);
    private WorklogLightOutboxWorker worker;

    /** 스케줄러의 자동 실행과 경쟁하지 않고 실제 DB 선점과 후속 처리를 검증한다. */
    @BeforeEach
    void setUp() {
        jdbcTemplate.execute("TRUNCATE TABLE tb_worklog_light_outbox");
        worker = new WorklogLightOutboxWorker(repository, client, new AiWorklogLightIndexProperties(
                true, true, true, "http://ai.test/ai", Duration.ofSeconds(1), Duration.ofSeconds(2), "token"));
    }

    @Test
    @DisplayName("대기 작업이 없으면 null ID 선점이 오류 없이 빈 결과를 반환한다")
    void null_id_claim_returns_empty_when_no_jobs_exist() {
        assertThat(repository.claim(null, UUID.randomUUID())).isEmpty();
    }

    @Test
    @DisplayName("ID를 지정하면 다른 대기 작업을 선점하지 않는다")
    void explicit_id_claim_only_selects_requested_job() {
        repository.enqueue(501L, "REINDEX");
        repository.enqueue(502L, "DELETE");

        var claims = repository.claim(502L, UUID.randomUUID());

        assertThat(claims).singleElement().satisfies(claim -> {
            assertThat(claim.worklogId()).isEqualTo(502L);
            assertThat(claim.operation()).isEqualTo("DELETE");
        });
        assertThat(repository.claim(502L, UUID.randomUUID())).isEmpty();
        assertThat(repository.claim(501L, UUID.randomUUID())).hasSize(1);
    }

    @Test
    @DisplayName("스케줄러가 수정과 삭제 작업을 null ID로 선점하면 AI 전달 후 작업을 제거한다")
    void poll_dispatches_reindex_and_delete_and_removes_successful_jobs() {
        repository.enqueue(501L, "REINDEX");
        repository.enqueue(502L, "DELETE");

        worker.poll();

        verify(client).requestReindex(TriggerWorklogLightIndexDto.Request.of(501L));
        verify(client).requestDelete(TriggerWorklogLightDeleteDto.Request.of(502L));
        assertThat(jdbcTemplate.queryForObject("SELECT COUNT(*) FROM tb_worklog_light_outbox", Integer.class)).isZero();
    }

    @Test
    @DisplayName("AI 전달이 실패하면 작업을 보존하고 재시도 기한이 된 후 성공한 작업을 제거한다")
    void failed_dispatch_is_retained_until_poll_retry_succeeds() {
        repository.enqueue(501L, "REINDEX");
        var request = TriggerWorklogLightIndexDto.Request.of(501L);
        doThrow(new IllegalStateException("AI unavailable")).when(client).requestReindex(request);

        assertThat(worker.processOne(null)).isTrue();

        assertThat(jdbcTemplate.queryForObject("""
                SELECT attempts = 1 AND next_attempt_at > updated_at
                    AND claim_token IS NULL AND lease_until IS NULL
                    AND last_error LIKE '%AI unavailable%'
                FROM tb_worklog_light_outbox WHERE worklog_id = 501
                """, Boolean.class)).isTrue();
        jdbcTemplate.update("""
                UPDATE tb_worklog_light_outbox SET next_attempt_at = CURRENT_TIMESTAMP + INTERVAL '1 hour'
                WHERE worklog_id = 501
                """);
        worker.poll();
        verify(client).requestReindex(request);
        doNothing().when(client).requestReindex(request);
        jdbcTemplate.update("""
                UPDATE tb_worklog_light_outbox SET next_attempt_at = CURRENT_TIMESTAMP - INTERVAL '1 second'
                WHERE worklog_id = 501
                """);

        worker.poll();

        verify(client, times(2)).requestReindex(request);
        assertThat(repository.claim(null, UUID.randomUUID())).isEmpty();
        assertThat(jdbcTemplate.queryForObject("SELECT COUNT(*) FROM tb_worklog_light_outbox", Integer.class)).isZero();
    }
}
