package com.ibank.axwms.domain.worklog.service;

import com.ibank.axwms.domain.worklog.dto.TriggerWorklogLightDeleteDto;
import com.ibank.axwms.domain.worklog.dto.TriggerWorklogLightIndexDto;
import com.ibank.axwms.domain.worklog.external.WorklogLightIndexClient;
import com.ibank.axwms.domain.worklog.repository.WorklogLightOutboxRepository;
import com.ibank.axwms.testsupport.IntegrationTestSupport;
import java.util.UUID;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.bean.override.mockito.MockitoBean;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.doNothing;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;

class WorklogLightOutboxIntegrationTest extends IntegrationTestSupport {

    @Autowired
    private JdbcTemplate jdbcTemplate;

    @Autowired
    private org.springframework.transaction.support.TransactionTemplate transaction;

    @Autowired
    private WorklogLightOutboxRepository repository;

    private final WorklogLightIndexClient client = mock(WorklogLightIndexClient.class);
    private final InternalWorklogLightIndexService indexService = mock(InternalWorklogLightIndexService.class);
    private WorklogLightOutboxWorker worker;

    @MockitoBean
    private WorklogLightOutboxWorker scheduledWorker;

    /** 스케줄러의 자동 실행과 경쟁하지 않고 실제 DB 선점과 후속 처리를 검증한다. */
    @BeforeEach
    void setUp() {
        jdbcTemplate.execute("TRUNCATE TABLE tb_worklog_light_outbox");
        worker = new WorklogLightOutboxWorker(repository, client, indexService);
        org.mockito.Mockito.doAnswer(call -> {
            repository.fail(call.getArgument(0), call.getArgument(1));
            return null;
        }).when(indexService).deliveryFailed(any(), any());
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
    @DisplayName("lease 만료 후 재선점되면 이전 선점의 접수·실패 처리는 현재 선점을 변경하지 않는다")
    void expired_claim_cannot_change_replacement_request() {
        repository.enqueue(501L, "REINDEX");
        var stale = repository.claim(501L, UUID.randomUUID()).getFirst();
        jdbcTemplate.update("UPDATE tb_worklog_light_outbox SET lease_until = CURRENT_TIMESTAMP - INTERVAL '1 second' WHERE worklog_id = 501");
        assertThat(repository.claim(501L, UUID.randomUUID())).isEmpty();
        repository.enqueue(501L, "REINDEX");
        var current = repository.claim(501L, UUID.randomUUID()).getFirst();

        repository.awaitResult(stale);
        repository.fail(stale, "obsolete failure");

        assertThat(jdbcTemplate.queryForObject("""
                SELECT claim_token = ? AND attempts = 1 AND last_error IS NULL
                    AND lease_until > CURRENT_TIMESTAMP AND next_attempt_at <= CURRENT_TIMESTAMP
                FROM tb_worklog_light_outbox WHERE worklog_id = 501
                """, Boolean.class, current.token())).isTrue();
    }

    @Test
    @DisplayName("수정·삭제 요청은 AI 접수 후에도 보존하고 결과 대기 중 즉시 재전달하지 않는다")
    void poll_retains_accepted_reindex_and_delete_until_result() {
        repository.enqueue(501L, "REINDEX");
        repository.enqueue(502L, "DELETE");

        worker.poll();
        worker.poll();

        verify(client).requestReindex(any());
        verify(client).requestDelete(any());
        assertThat(jdbcTemplate.queryForObject("SELECT COUNT(*) FROM tb_worklog_light_outbox", Integer.class)).isEqualTo(2);
        assertThat(repository.claim(null, UUID.randomUUID())).isEmpty();
    }

    @Test
    @DisplayName("AI 전달 실패 후 재접수에 성공해도 완료 콜백 전까지 작업을 보존한다")
    void failed_dispatch_is_retained_until_poll_retry_succeeds() {
        repository.enqueue(501L, "REINDEX");
        var request = TriggerWorklogLightIndexDto.Request.of(501L, jdbcTemplate.queryForObject(
                "SELECT request_id FROM tb_worklog_light_outbox WHERE worklog_id = 501", UUID.class));
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
        assertThat(jdbcTemplate.queryForObject("SELECT COUNT(*) FROM tb_worklog_light_outbox", Integer.class)).isEqualTo(1);
    }
    @Test
    void latestIntentKeepsOneRowIncreasingRevisionAndDeletePriority() {
        repository.enqueue(501L, "INDEX");
        UUID first = jdbcTemplate.queryForObject("SELECT request_id FROM tb_worklog_light_outbox WHERE worklog_id = 501", UUID.class);
        repository.enqueue(501L, "REINDEX");
        assertThat(jdbcTemplate.queryForObject("SELECT operation FROM tb_worklog_light_outbox WHERE worklog_id = 501", String.class)).isEqualTo("REINDEX");
        repository.enqueue(501L, "DELETE");
        repository.enqueue(501L, "INDEX");
        assertThat(jdbcTemplate.queryForObject("SELECT count(*) FROM tb_worklog_light_outbox WHERE worklog_id = 501", Integer.class)).isEqualTo(1);
        var latest = jdbcTemplate.queryForMap("SELECT operation, revision, request_id, attempts FROM tb_worklog_light_outbox WHERE worklog_id = 501");
        assertThat(latest.get("operation")).isEqualTo("DELETE");
        assertThat(latest.get("revision")).isEqualTo(4L);
        assertThat(latest.get("request_id")).isNotEqualTo(first);
        assertThat(latest.get("attempts")).isEqualTo(0);
    }

    @Test
    void lockedWorklogIsSkippedWithoutBlockingOtherWorklog() {
        repository.enqueue(601L, "INDEX");
        repository.enqueue(602L, "DELETE");
        transaction.executeWithoutResult(ignored -> {
            jdbcTemplate.queryForList("SELECT worklog_id FROM tb_worklog_light_outbox WHERE worklog_id = 601 FOR UPDATE");
            try (var pool = java.util.concurrent.Executors.newSingleThreadExecutor()) {
                var claims = pool.submit(() -> repository.claim(null, UUID.randomUUID()))
                        .get(3, java.util.concurrent.TimeUnit.SECONDS);
                assertThat(claims).singleElement().satisfies(claim -> assertThat(claim.worklogId()).isEqualTo(602L));
            } catch (Exception exception) {
                throw new AssertionError("잠긴 업무가 다른 업무 선점을 막았습니다", exception);
            }
        });
    }

}
