package com.ibank.axwms.domain.worklog.service;

import com.ibank.axwms.domain.worklog.event.WorklogAiPostProcessRequestedEvent;
import com.ibank.axwms.domain.worklog.repository.WorklogAiPostProcessOutboxRepository;
import com.ibank.axwms.domain.worklog.repository.WorklogAiPostProcessOutboxRepository.Claim;
import com.ibank.axwms.testsupport.IntegrationTestSupport;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.context.ApplicationEventPublisher;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.TestPropertySource;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.Mockito.verifyNoInteractions;

@TestPropertySource(properties = "ai.worklog-post-process.outbox-poll-delay-ms=3600000")
class WorklogAiPostProcessOutboxIntegrationTest extends IntegrationTestSupport {

    @Autowired
    private JdbcTemplate jdbcTemplate;

    @Autowired
    private ApplicationEventPublisher eventPublisher;

    @Autowired
    private PlatformTransactionManager transactionManager;

    @Autowired
    private WorklogAiPostProcessOutboxRepository repository;

    @MockitoBean
    private WorklogAiPostProcessService postProcessService;

    @MockitoBean
    private WorklogLightOutboxWorker scheduledLightWorker;

    @BeforeEach
    void clearOutbox() {
        jdbcTemplate.execute("TRUNCATE TABLE tb_worklog_ai_post_process_outbox");
        jdbcTemplate.execute("TRUNCATE TABLE tb_worklog_light_outbox");
    }

    @Test
    @DisplayName("등록 트랜잭션이 커밋되면 파일과 본문 snapshot을 Outbox에 보존하고 AI를 직접 호출하지 않는다")
    void registration_commit_persists_snapshot_without_dispatching_ai() {
        assertThat(jdbcTemplate.queryForObject("""
                SELECT EXISTS (SELECT 1 FROM information_schema.tables
                               WHERE table_name = 'tb_worklog_ai_post_process_outbox')
                """, Boolean.class)).isTrue();

        new TransactionTemplate(transactionManager).executeWithoutResult(status ->
                eventPublisher.publishEvent(event(501L)));

        assertThat(jdbcTemplate.queryForObject("""
                SELECT payload ->> 'workContent' FROM tb_worklog_ai_post_process_outbox WHERE worklog_id = 501
                """, String.class)).isEqualTo("업무 내용");
        assertThat(jdbcTemplate.queryForObject("""
                SELECT payload -> 'files' -> 0 ->> 'fileId'
                FROM tb_worklog_ai_post_process_outbox WHERE worklog_id = 501
                """, String.class)).isEqualTo("9001");
        assertThat(jdbcTemplate.queryForObject("""
                SELECT operation FROM tb_worklog_light_outbox WHERE worklog_id = 501
                """, String.class)).isEqualTo("INDEX");
        verifyNoInteractions(postProcessService);
    }

    @Test
    @DisplayName("등록 트랜잭션이 롤백되면 후처리 작업도 남지 않는다")
    void registration_rollback_does_not_persist_job() {
        new TransactionTemplate(transactionManager).executeWithoutResult(status -> {
            eventPublisher.publishEvent(event(501L));
            status.setRollbackOnly();
        });

        assertThat(jobCount()).isZero();
        assertThat(jdbcTemplate.queryForObject("SELECT COUNT(*) FROM tb_worklog_light_outbox", Integer.class)).isZero();
        verifyNoInteractions(postProcessService);
    }

    @Test
    @DisplayName("Outbox 저장이 실패하면 같은 트랜잭션의 다른 DB 쓰기도 롤백한다")
    void enqueue_failure_rolls_back_other_database_writes() {
        assertThatThrownBy(() -> new TransactionTemplate(transactionManager).executeWithoutResult(status -> {
            jdbcTemplate.update("""
                    INSERT INTO tb_worklog_light_outbox (worklog_id, operation) VALUES (99501, 'REINDEX')
                    """);
            eventPublisher.publishEvent(event(null));
        })).isInstanceOf(RuntimeException.class);

        assertThat(jdbcTemplate.queryForObject("""
                SELECT COUNT(*) FROM tb_worklog_light_outbox WHERE worklog_id = 99501
                """, Integer.class)).isZero();
        assertThat(jobCount()).isZero();
    }

    @Test
    @DisplayName("동시에 선점을 시도해도 하나의 워커만 작업을 얻는다")
    void concurrent_claims_have_one_owner() throws Exception {
        repository.enqueue(event(501L));
        var start = new CountDownLatch(1);
        try (var executor = Executors.newFixedThreadPool(2)) {
            var first = executor.submit(() -> {
                assertThat(start.await(10, TimeUnit.SECONDS)).isTrue();
                return repository.claim(501L, UUID.randomUUID());
            });
            var second = executor.submit(() -> {
                assertThat(start.await(10, TimeUnit.SECONDS)).isTrue();
                return repository.claim(501L, UUID.randomUUID());
            });
            start.countDown();

            assertThat(first.get(10, TimeUnit.SECONDS).size()
                    + second.get(10, TimeUnit.SECONDS).size()).isEqualTo(1);
        }
    }

    @Test
    @DisplayName("실패한 작업은 오류와 백오프를 보존하고 기한이 되면 재선점된다")
    void failed_job_remains_for_retry_after_backoff() {
        repository.enqueue(event(501L));
        Claim claim = claimJob();

        repository.fail(claim, "AI unavailable");

        assertThat(jobCount()).isEqualTo(1);
        assertThat(jdbcTemplate.queryForObject("""
                SELECT attempts = 1 AND last_error = 'AI unavailable'
                    AND next_attempt_at > CURRENT_TIMESTAMP AND claim_token IS NULL AND lease_until IS NULL
                FROM tb_worklog_ai_post_process_outbox WHERE worklog_id = 501
                """, Boolean.class)).isTrue();
        assertThat(repository.claim(501L, UUID.randomUUID())).isEmpty();
        makeDue();

        Claim retry = claimJob();
        assertThat(retry.attempts()).isEqualTo(1);
        assertThat(retry.event()).isEqualTo(event(501L));
    }

    @Test
    @DisplayName("성공한 단계는 선점 만료 뒤에도 보존되어 재처리에서 복구된다")
    void checkpoint_survives_reclaim() {
        repository.enqueue(event(501L));
        Claim claim = claimJob();

        assertThat(repository.checkpoint(claim, "pipeline")).isTrue();
        expireLease();

        assertThat(claimJob().completedStages()).containsExactly("pipeline");
    }

    @Test
    @DisplayName("원본 업무가 없으면 선점이 유효해도 외부 요청 실행 기한을 갱신하지 않는다")
    void lease_renewal_requires_existing_active_worklog() {
        repository.enqueue(event(501L));
        Claim claim = claimJob();

        assertThat(repository.renewLease(claim)).isFalse();
    }

    @Test
    @DisplayName("선점이 만료되면 새 워커가 없더라도 성공 단계를 추가할 수 없다")
    void expired_claim_cannot_checkpoint_before_reclaim() {
        repository.enqueue(event(501L));
        Claim claim = claimJob();
        expireLease();

        assertThat(repository.checkpoint(claim, "pipeline")).isFalse();
        assertThat(claimJob().completedStages()).isEmpty();
    }

    @Test
    @DisplayName("완료 트랜잭션은 현재 소유권만 잠그며 만료된 선점은 거부한다")
    void completion_lock_requires_current_unexpired_claim() {
        repository.enqueue(event(501L));
        Claim claim = claimJob();
        var transaction = new TransactionTemplate(transactionManager);

        assertThat(transaction.<Boolean>execute(status -> repository.lockClaim(claim))).isTrue();
        expireLease();

        assertThat(transaction.<Boolean>execute(status -> repository.lockClaim(claim))).isFalse();
    }

    @Test
    @DisplayName("후속 단계가 실패해도 이미 성공한 단계는 재시도에서 유지된다")
    void later_failure_does_not_discard_completed_stage() {
        repository.enqueue(event(501L));
        Claim claim = claimJob();
        assertThat(repository.checkpoint(claim, "pipeline")).isTrue();

        repository.fail(claim, "index unavailable");
        makeDue();

        assertThat(claimJob().completedStages()).containsExactly("pipeline");
    }

    @Test
    @DisplayName("만료된 선점자는 단계 저장·완료·실패로 새 선점자의 작업을 변경할 수 없다")
    void expired_owner_cannot_modify_reclaimed_job() {
        repository.enqueue(event(501L));
        Claim stale = claimJob();
        expireLease();
        Claim current = claimJob();

        assertThat(repository.checkpoint(stale, "pipeline")).isFalse();
        repository.complete(stale);
        repository.fail(stale, "stale failure");

        assertThat(jdbcTemplate.queryForObject("""
                SELECT claim_token = ? AND attempts = 0 AND last_error IS NULL
                    AND cardinality(completed_stages) = 0
                FROM tb_worklog_ai_post_process_outbox WHERE worklog_id = 501
                """, Boolean.class, current.token())).isTrue();
        repository.complete(current);
        assertThat(jobCount()).isZero();
    }

    @Test
    @DisplayName("재요청은 기존 파일 snapshot·성공 단계·진행 중 선점을 덮어쓰지 않는다")
    void repeated_enqueue_preserves_snapshot_checkpoints_and_owner() {
        repository.enqueue(event(501L));
        Claim original = claimJob();
        assertThat(repository.checkpoint(original, "pipeline")).isTrue();
        var retryWithoutFiles = new WorklogAiPostProcessRequestedEvent(
                501L, "변경된 제목", "변경된 요청", "변경된 본문", 101L, 21L, 9L, List.of());

        repository.enqueue(retryWithoutFiles);

        assertThat(repository.claim(501L, UUID.randomUUID())).isEmpty();
        assertThat(jdbcTemplate.queryForObject("""
                SELECT claim_token = ? AND lease_until > CURRENT_TIMESTAMP
                FROM tb_worklog_ai_post_process_outbox WHERE worklog_id = 501
                """, Boolean.class, original.token())).isTrue();
        expireLease();
        Claim retry = claimJob();
        assertThat(retry.event()).isEqualTo(event(501L));
        assertThat(retry.completedStages()).containsExactly("pipeline");
    }

    @Test
    @DisplayName("명시적 재요청 승격은 최신 본문과 실행 중 성공한 파일을 보존하고 요약·색인만 다시 실행한다")
    void queued_retry_keeps_latest_input_and_file_checkpoints_from_current_execution() {
        repository.enqueue(event(501L));
        Claim initial = claimJob();
        repository.enqueue(new WorklogAiPostProcessRequestedEvent(501L, "첫 재요청", "요청", "첫 본문",
                101L, 21L, 9L, List.of()));
        repository.enqueue(new WorklogAiPostProcessRequestedEvent(501L, "최신 재요청", "요청", "최신 본문",
                101L, 21L, 9L, List.of()));
        assertThat(repository.checkpoint(initial, "FILE_SUMMARY(9001)")).isTrue();
        assertThat(repository.checkpoint(initial, "WORKLOG_PIPELINE")).isTrue();
        assertThat(repository.checkpoint(initial, "LIGHT_INDEX")).isTrue();

        assertThat(new TransactionTemplate(transactionManager).<Boolean>execute(status -> {
            assertThat(repository.lockClaim(initial)).isTrue();
            return repository.activateQueuedRetry(initial);
        })).isTrue();

        assertThat(repository.checkpoint(initial, "stale")).isFalse();
        repository.complete(initial);
        Claim retry = claimJob();
        assertThat(retry.event().workContent()).isEqualTo("최신 본문");
        assertThat(retry.event().files()).hasSize(1);
        assertThat(retry.event().files().getFirst().fileId()).isEqualTo(9001L);
        assertThat(retry.completedStages()).containsExactly("FILE_SUMMARY(9001)");
        assertThat(retry.attempts()).isZero();
        assertThat(retry.token()).isNotEqualTo(initial.token());
    }

    /** 테스트가 만드는 작업만 집계해 외부 처리 여부를 DB에서 확인한다. */
    private int jobCount() {
        return jdbcTemplate.queryForObject("SELECT COUNT(*) FROM tb_worklog_ai_post_process_outbox", Integer.class);
    }

    /** 선점 실패를 후속 검증과 구분하기 위해 단일 작업 획득을 먼저 단언한다. */
    private Claim claimJob() {
        List<Claim> claims = repository.claim(501L, UUID.randomUUID());
        assertThat(claims).hasSize(1);
        return claims.getFirst();
    }

    /** 실제 대기 없이 백오프 이후의 DB 상태를 재현한다. */
    private void makeDue() {
        jdbcTemplate.update("""
                UPDATE tb_worklog_ai_post_process_outbox
                SET next_attempt_at = CURRENT_TIMESTAMP - INTERVAL '1 second' WHERE worklog_id = 501
                """);
    }

    /** sleep 없이 장애로 선점이 만료된 상태를 재현한다. */
    private void expireLease() {
        jdbcTemplate.update("""
                UPDATE tb_worklog_ai_post_process_outbox
                SET lease_until = CURRENT_TIMESTAMP - INTERVAL '1 second' WHERE worklog_id = 501
                """);
    }

    /** 본문과 파일 snapshot이 함께 보존되는 등록 입력이다. */
    private static WorklogAiPostProcessRequestedEvent event(Long worklogId) {
        return new WorklogAiPostProcessRequestedEvent(worklogId, "업무 제목", "요청 내용", "업무 내용",
                101L, 21L, 9L, List.of(new WorklogAiPostProcessRequestedEvent.FileSummaryTarget(
                        9001L, "worklog/501/report.txt", "report.txt", "txt")));
    }
}
