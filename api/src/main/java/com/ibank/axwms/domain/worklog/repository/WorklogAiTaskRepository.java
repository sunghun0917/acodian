package com.ibank.axwms.domain.worklog.repository;

import com.ibank.axwms.domain.worklog.config.AiTaskExecutionBudgetProperties;
import com.ibank.axwms.domain.worklog.dto.AiTaskApiDto;
import com.ibank.axwms.domain.worklog.event.WorklogAiPostProcessRequestedEvent;
import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.util.Optional;
import java.util.Arrays;
import java.util.HexFormat;
import java.util.List;
import java.util.UUID;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.transaction.annotation.Propagation;

/** 작업별 현재 요청과 단일 시도 횟수·lease를 PostgreSQL 원자 연산으로 관리한다. */
@Repository
public class WorklogAiTaskRepository {

    private static final ObjectMapper OBJECT_MAPPER = new ObjectMapper();

    /** 아직 결과가 오지 않은 작업만 삭제·파일 제거 시 종결 대상으로 삼는다. */
    private static final String IN_FLIGHT_CONDITION = """
            (execution_state IN ('QUEUED', 'RUNNING', 'RETRY_READY')
             OR (execution_state = 'UNKNOWN' AND delivery_state <> 'EXHAUSTED'))
            """;

    /**
     * 입력 수정 시 다시 요청할 작업이다. 진행 중 작업에 더해 최종 실패(FAILED)와 전달·결과 확인불가(UNKNOWN+EXHAUSTED)를 포함하고,
     * 이미 성공했거나 교체·종결(SUPERSEDED)된 작업은 제외해 완료 결과를 수정마다 다시 만들지 않는다.
     */
    private static final String REREQUESTABLE_ON_EDIT_CONDITION = """
            execution_state NOT IN ('SUCCEEDED', 'SUPERSEDED')
            """;

    /** 종결 시 옛 실행 식별자와 선점 lease을 함께 지워 늦은 콜백과 복구 스케줄러가 행을 되살리지 못하게 한다. */
    private static final String SUPERSEDE_ASSIGNMENTS = """
            execution_state = 'SUPERSEDED', execution_id = NULL, claim_token = NULL, lease_until = NULL,
            updated_at = CURRENT_TIMESTAMP
            """;

    private final JdbcTemplate jdbcTemplate;
    private final AiTaskExecutionBudgetProperties budget;

    /** Light와 일반 작업이 같은 시도 한도와 단일 lease 설정을 사용한다. */
    public WorklogAiTaskRepository(JdbcTemplate jdbcTemplate, AiTaskExecutionBudgetProperties budget) {
        this.jdbcTemplate = jdbcTemplate;
        this.budget = budget;
    }

    /** 최초 요청을 대상별로 보관하며 중복 등록이 기존 진행 작업을 교체하지 못하게 한다. */
    @Transactional
    public boolean enqueueInitialTasks(WorklogAiPostProcessRequestedEvent event) {
        try {
            String payload = OBJECT_MAPPER.writeValueAsString(event);
            boolean initial = insertInitialTask(event.worklogId(), "SUMMARY", event.worklogId(), payload,
                    Arrays.asList(event.requestContent(), event.workContent()));
            insertInitialTask(event.worklogId(), "TAG", event.worklogId(), payload,
                    Arrays.asList(event.workContent()));
            for (var file : event.files()) {
                insertInitialTask(event.worklogId(), "FILE_SUMMARY", file.fileId(), payload,
                        Arrays.asList(file.fileId(), file.storageKey(), file.originalName(), file.fileExtension()));
            }
            return initial;
        } catch (JsonProcessingException e) {
            throw new IllegalStateException("AI 작업 snapshot 직렬화 실패", e);
        }
    }

    /** 수정 중 추가된 파일만 독립 작업으로 보존하고 기존 요약·태그 작업은 변경하지 않는다. */
    @Transactional
    public void enqueueFileTask(WorklogAiPostProcessRequestedEvent event,
                                WorklogAiPostProcessRequestedEvent.FileSummaryTarget file) {
        try {
            insertInitialTask(event.worklogId(), "FILE_SUMMARY", file.fileId(), OBJECT_MAPPER.writeValueAsString(event),
                    Arrays.asList(file.fileId(), file.storageKey(), file.originalName(), file.fileExtension()));
        } catch (JsonProcessingException e) {
            throw new IllegalStateException("AI 파일 snapshot 직렬화 실패", e);
        }
    }

    /** 특정 작업의 입력만 해시해 관련 없는 제목이나 다른 파일 변경과 구분한다. */
    private boolean insertInitialTask(Long worklogId, String kind, Long targetId, String payload, Object input)
            throws JsonProcessingException {
        try {
            String hash = hashInput(input);
            return jdbcTemplate.update("""
                    INSERT INTO tb_worklog_ai_post_process_outbox (worklog_id, task_kind, target_id, request_id, input_hash, payload)
                    VALUES (?, ?, ?, ?, ?, CAST(? AS jsonb))
                    ON CONFLICT (worklog_id, task_kind, target_id) DO NOTHING
                    """, worklogId, kind, targetId, UUID.randomUUID(), hash, payload) == 1;
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("SHA-256 사용 불가", e);
        }
    }

    /** 현재 본문의 의미상 입력이 작업 접수 당시 입력과 같은지 확인한다. */
    public boolean isCurrentWorklogInput(String kind, String expectedHash, String requestContent, String workContent) {
        try {
            return hashInput(worklogTaskInput(kind, requestContent, workContent)).equals(expectedHash);
        } catch (JsonProcessingException | NoSuchAlgorithmException e) {
            throw new IllegalStateException("AI 작업 입력 해시 실패", e);
        }
    }

    /** 요약은 요청·업무 내용, 태그는 업무 내용만 입력으로 보아 제목 등 무관한 수정이 작업을 교체하지 않게 한다. */
    private static Object worklogTaskInput(String kind, String requestContent, String workContent) {
        return switch (kind) {
            case "SUMMARY" -> Arrays.asList(requestContent, workContent);
            case "TAG" -> Arrays.asList(workContent);
            default -> throw new IllegalArgumentException("Unsupported worklog task kind");
        };
    }

    /**
     * 업무 행을 잠근 수정 트랜잭션에서 입력이 바뀐 미종결·실패 요약·태그를 최신 snapshot 의 새 요청으로 교체한다.
     * 단일 행 교체로 옛 requestId·실행 식별자·선점이 사라지므로 옛 시작·결과·재시도는 current 불일치로 거절된다.
     * 실패·확인불가 작업은 입력 수정을 사용자의 재요청으로 보고(2026-11-11 사용자 결정) 다시 요청하지만,
     * 성공한 작업은 입력이 바뀌어도 자동 재실행하지 않는다. 새 요청의 예산은 0부터 시작한다.
     *
     * @return 새 요청으로 교체된 작업 종류
     */
    @Transactional(propagation = Propagation.MANDATORY)
    public List<String> supersedeChangedWorklogTasks(WorklogAiPostProcessRequestedEvent snapshot) {
        try {
            String payload = OBJECT_MAPPER.writeValueAsString(snapshot);
            List<String> superseded = new java.util.ArrayList<>();
            for (String kind : List.of("SUMMARY", "TAG")) {
                String hash = hashInput(worklogTaskInput(kind, snapshot.requestContent(), snapshot.workContent()));
                int changed = jdbcTemplate.update("""
                        UPDATE tb_worklog_ai_post_process_outbox
                        SET request_id = ?, input_hash = ?, payload = CAST(? AS jsonb), retried_from_request_id = NULL,
                            execution_id = NULL, delivery_state = 'PENDING', execution_state = 'UNKNOWN',
                            result_digest = NULL,
                            result_error_code = NULL, attempts = 0, claim_token = NULL,
                            lease_until = NULL, last_error = NULL, next_attempt_at = CURRENT_TIMESTAMP,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE worklog_id = ? AND task_kind = ? AND target_id = ? AND input_hash IS DISTINCT FROM ?
                        """ + " AND " + REREQUESTABLE_ON_EDIT_CONDITION,
                        UUID.randomUUID(), hash, payload, snapshot.worklogId(), kind, snapshot.worklogId(), hash);
                if (changed == 1) {
                    superseded.add(kind);
                }
            }
            return superseded;
        } catch (JsonProcessingException | NoSuchAlgorithmException e) {
            throw new IllegalStateException("AI 작업 교체 snapshot 생성 실패", e);
        }
    }

    /**
     * 제거된 파일의 미종결 요약 작업만 SUPERSEDED 로 종결한다. 파일 ID 는 재사용되지 않으므로 새 요청을 만들지 않고,
     * 실행 식별자와 선점 lease를 지워 발행·복구·결과 반영 대상에서 빠지게 한다.
     */
    @Transactional(propagation = Propagation.MANDATORY)
    public int supersedeFileTasks(Long worklogId, java.util.Collection<Long> fileIds) {
        if (fileIds == null || fileIds.isEmpty()) {
            return 0;
        }
        return jdbcTemplate.update("UPDATE tb_worklog_ai_post_process_outbox SET " + SUPERSEDE_ASSIGNMENTS
                        + " WHERE worklog_id = ? AND task_kind = 'FILE_SUMMARY' AND target_id = ANY (?) AND " + IN_FLIGHT_CONDITION,
                worklogId, fileIds.toArray(Long[]::new));
    }

    /** 소프트 삭제된 업무의 미종결 일반 작업을 모두 종결해 삭제 뒤 AI 발행·결과 반영을 막는다. */
    @Transactional(propagation = Propagation.MANDATORY)
    public int supersedeAllTasks(Long worklogId) {
        return jdbcTemplate.update("UPDATE tb_worklog_ai_post_process_outbox SET " + SUPERSEDE_ASSIGNMENTS
                        + " WHERE worklog_id = ? AND task_kind IN ('SUMMARY', 'TAG', 'FILE_SUMMARY') AND " + IN_FLIGHT_CONDITION,
                worklogId);
    }

    /** 파일 연결과 스토리지 식별 정보가 최초 요청 snapshot과 같은지 확인한다. */
    public boolean isCurrentFileInput(String expectedHash, Long fileId, String storageKey, String originalName, String extension) {
        try {
            return hashInput(Arrays.asList(fileId, storageKey, originalName, extension)).equals(expectedHash);
        } catch (JsonProcessingException | NoSuchAlgorithmException e) {
            throw new IllegalStateException("AI 파일 입력 해시 실패", e);
        }
    }

    /** 식별된 입력의 직렬화 방식을 생성·결과 검사에서 동일하게 유지한다. */
    private String hashInput(Object input) throws JsonProcessingException, NoSuchAlgorithmException {
        return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256")
                .digest(OBJECT_MAPPER.writeValueAsBytes(input)));
    }

    /** 이미 잠근 현재 요청의 최초 사용자 표시 문맥만 복원한다. */
    @Transactional(propagation = Propagation.MANDATORY)
    public WorklogAiPostProcessRequestedEvent resultSnapshot(Long worklogId, AiTaskApiDto.Identity identity) {
        String payload = jdbcTemplate.queryForObject("""
                SELECT payload::text FROM tb_worklog_ai_post_process_outbox
                WHERE worklog_id = ? AND task_kind = ? AND target_id = ? AND request_id = ?
                """, String.class, worklogId, identity.taskKind(), identity.targetId(), identity.requestId());
        try {
            return OBJECT_MAPPER.readValue(payload, WorklogAiPostProcessRequestedEvent.class);
        } catch (JsonProcessingException e) {
            throw new IllegalStateException("AI 작업 snapshot 역직렬화 실패", e);
        }
    }


    /** 비활성 작업은 선점하지 않으며 큐 대기부터 결과까지 같은 lease로 회수한다. */
    public Optional<DeliveryClaim> claimDelivery(Long worklogId, UUID token, boolean pipelineEnabled, boolean fileEnabled) {
        return jdbcTemplate.query("""
                UPDATE tb_worklog_ai_post_process_outbox o
                SET claim_token = ?, lease_until = CURRENT_TIMESTAMP + (? * INTERVAL '1 second'),
                    attempts = o.attempts + 1, execution_state = 'QUEUED', execution_id = NULL,
                    updated_at = CURRENT_TIMESTAMP
                FROM (
                    SELECT worklog_id, task_kind, target_id FROM tb_worklog_ai_post_process_outbox
                    WHERE ((task_kind IN ('SUMMARY', 'TAG') AND ?) OR (task_kind = 'FILE_SUMMARY' AND ?))
                      AND (CAST(? AS BIGINT) IS NULL OR worklog_id = ?)
                      AND execution_state IN ('UNKNOWN', 'RETRY_READY') AND attempts < ?
                      AND input_hash IS NOT NULL AND next_attempt_at <= CURRENT_TIMESTAMP AND lease_until IS NULL
                    ORDER BY next_attempt_at, worklog_id, task_kind, target_id
                    FOR UPDATE SKIP LOCKED LIMIT 1
                ) due
                WHERE o.worklog_id = due.worklog_id AND o.task_kind = due.task_kind AND o.target_id = due.target_id
                RETURNING o.worklog_id, o.task_kind, o.target_id, o.request_id, o.attempts, o.input_hash, o.payload::text
                """, (rs, rowNum) -> new DeliveryClaim(rs.getLong("worklog_id"), rs.getString("task_kind"),
                rs.getLong("target_id"), rs.getObject("request_id", UUID.class), rs.getInt("attempts"),
                rs.getString("input_hash"), rs.getString("payload"), token),
                token, budget.timeout().toSeconds(), pipelineEnabled, fileEnabled, worklogId, worklogId,
                budget.maxAttempts()).stream().findFirst();
    }

    /** 빠른 시작·결과를 덮지 않고 동일 선점의 접수만 확인한다. */
    public boolean deliveryAccepted(DeliveryClaim claim) {
        return jdbcTemplate.update("""
                UPDATE tb_worklog_ai_post_process_outbox SET delivery_state = 'ACCEPTED', claim_token = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE worklog_id = ? AND task_kind = ? AND target_id = ? AND request_id = ? AND claim_token = ?
                  AND execution_state IN ('QUEUED', 'RUNNING')
                """, claim.worklogId(), claim.taskKind(), claim.targetId(), claim.requestId(), claim.token()) == 1;
    }

    /** 시작이 먼저 확인되면 전달 오류를 무시하고 나머지는 backoff 또는 최종 실패로 전환한다. */
    public boolean deliveryFailed(DeliveryClaim claim, String error) {
        return jdbcTemplate.update("""
                UPDATE tb_worklog_ai_post_process_outbox
                SET execution_state = CASE WHEN attempts >= ? THEN 'FAILED' ELSE 'RETRY_READY' END,
                    delivery_state = CASE WHEN attempts >= ? THEN 'EXHAUSTED' ELSE 'PENDING' END,
                    next_attempt_at = CURRENT_TIMESTAMP + (POWER(2, LEAST(attempts - 1, 8)) * INTERVAL '1 second'),
                    claim_token = NULL, lease_until = NULL, last_error = LEFT(?, 1000), updated_at = CURRENT_TIMESTAMP
                WHERE worklog_id = ? AND task_kind = ? AND target_id = ? AND request_id = ?
                  AND claim_token = ? AND execution_state = 'QUEUED'
                """, budget.maxAttempts(), budget.maxAttempts(), error, claim.worklogId(), claim.taskKind(),
                claim.targetId(), claim.requestId(), claim.token()) == 1;
    }

    /** 최종 전달 실패 알림도 같은 요청의 상태 전이와 함께 커밋한다. */
    @Transactional(propagation = Propagation.MANDATORY)
    public boolean isFailed(DeliveryClaim claim) {
        return Boolean.TRUE.equals(jdbcTemplate.queryForObject("""
                SELECT EXISTS (SELECT 1 FROM tb_worklog_ai_post_process_outbox
                    WHERE worklog_id = ? AND task_kind = ? AND target_id = ? AND request_id = ?
                      AND execution_state = 'FAILED')
                """, Boolean.class, claim.worklogId(), claim.taskKind(), claim.targetId(), claim.requestId()));
    }

    /** 외부 호출은 트랜잭션 밖에서 실행하되 현재 요청과 선점은 고정한다. */
    public record DeliveryClaim(Long worklogId, String taskKind, Long targetId, UUID requestId, int attempts,
                                String inputHash, String payload, UUID token) {}

    /** lease 하나가 만료된 큐 대기·실행 작업만 실패 정리 대상으로 조회한다. */
    public List<RecoveryCandidate> recoveryCandidates(int limit) {
        return jdbcTemplate.query("""
                SELECT worklog_id, task_kind, target_id, request_id FROM tb_worklog_ai_post_process_outbox
                WHERE task_kind IN ('SUMMARY', 'TAG', 'FILE_SUMMARY')
                  AND execution_state IN ('QUEUED', 'RUNNING') AND lease_until <= CURRENT_TIMESTAMP
                ORDER BY lease_until, worklog_id LIMIT ?
                """, (rs, rowNum) -> new RecoveryCandidate(rs.getLong("worklog_id"), rs.getString("task_kind"),
                rs.getLong("target_id"), rs.getObject("request_id", UUID.class)), limit);
    }

    /** 만료된 최신 요청을 업무 잠금 후 다시 확인하기 위한 식별자다. */
    public record RecoveryCandidate(Long worklogId, String taskKind, Long targetId, UUID requestId) {}

    /** 업무 잠금 후 최신 요청의 만료를 다시 확인해 갱신된 작업은 건드리지 않는다. */
    @Transactional(propagation = Propagation.MANDATORY)
    public Optional<String> failTimedOut(RecoveryCandidate candidate) {
        return jdbcTemplate.query("""
                UPDATE tb_worklog_ai_post_process_outbox SET execution_state = 'FAILED', delivery_state = 'EXHAUSTED',
                    claim_token = NULL, lease_until = NULL, last_error = 'TASK_TIMEOUT', updated_at = CURRENT_TIMESTAMP
                WHERE worklog_id = ? AND task_kind = ? AND target_id = ? AND request_id = ?
                  AND execution_state IN ('QUEUED', 'RUNNING') AND lease_until <= CURRENT_TIMESTAMP
                RETURNING payload::text
                """, (rs, rowNum) -> rs.getString(1), candidate.worklogId(), candidate.taskKind(),
                candidate.targetId(), candidate.requestId()).stream().findFirst();
    }

    /** 현재 결과의 적용·중복 승인·거절을 구분한다. */
    public enum ResultAuthority { APPLY, DUPLICATE, REJECTED }

    /** 업무 잠금 뒤 실패 요청 또는 직전 수동 재시도 관계만 조회한다. */
    @Transactional(propagation = Propagation.MANDATORY)
    public Optional<RetryCandidate> lockRetryCandidate(Long worklogId, UUID oldRequestId) {
        return jdbcTemplate.query("""
                SELECT task_kind, target_id, request_id, retried_from_request_id, input_hash, execution_state, delivery_state
                FROM tb_worklog_ai_post_process_outbox
                WHERE worklog_id = ? AND task_kind IN ('SUMMARY', 'TAG', 'FILE_SUMMARY')
                  AND (request_id = ? OR retried_from_request_id = ?) FOR UPDATE
                """, (rs, rowNum) -> new RetryCandidate(rs.getString("task_kind"), rs.getLong("target_id"),
                rs.getObject("request_id", UUID.class), rs.getObject("retried_from_request_id", UUID.class),
                rs.getString("input_hash"), rs.getString("execution_state"), rs.getString("delivery_state")),
                worklogId, oldRequestId, oldRequestId).stream().findFirst();
    }

    /** 검증된 실패 작업만 새 요청·시도 횟수 0으로 교체한다. */
    @Transactional(propagation = Propagation.MANDATORY)
    public UUID createRetry(Long worklogId, RetryCandidate candidate) {
        UUID next = UUID.randomUUID();
        int changed = jdbcTemplate.update("""
                UPDATE tb_worklog_ai_post_process_outbox
                SET retried_from_request_id = request_id, request_id = ?, execution_id = NULL,
                    delivery_state = 'PENDING', execution_state = 'UNKNOWN', result_digest = NULL,
                    result_error_code = NULL, attempts = 0, claim_token = NULL,
                    lease_until = NULL, last_error = NULL, next_attempt_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE worklog_id = ? AND task_kind = ? AND target_id = ? AND request_id = ? AND execution_state = 'FAILED'
                """, next, worklogId, candidate.taskKind(), candidate.targetId(), candidate.requestId());
        if (changed != 1) throw new IllegalStateException("AI 작업 재시도 상태가 변경되었습니다");
        return next;
    }

    /** 수동 재시도 대상과 이전 요청 관계를 확인한다. */
    public record RetryCandidate(String taskKind, Long targetId, UUID requestId, UUID retriedFromRequestId,
                                 String inputHash, String executionState, String deliveryState) {}

    /** 상세 화면 재시도도 현재 요약 요청을 통해 같은 검증을 거친다. */
    public Optional<UUID> currentSummaryRequest(Long worklogId) {
        return jdbcTemplate.query("""
                SELECT request_id FROM tb_worklog_ai_post_process_outbox
                WHERE worklog_id = ? AND task_kind = 'SUMMARY' AND target_id = ?
                """, (rs, rowNum) -> rs.getObject(1, UUID.class), worklogId, worklogId).stream().findFirst();
    }

    /** 현재 실행의 첫 결과만 적용하며 이미 저장한 동일 결과는 멱등 ACK한다. */
    @Transactional(propagation = Propagation.MANDATORY)
    public ResultAuthority lockResult(Long worklogId, AiTaskApiDto.Identity identity, String status, String digest) {
        return jdbcTemplate.query("""
                SELECT execution_state, result_digest, lease_until > CURRENT_TIMESTAMP AS lease_valid
                FROM tb_worklog_ai_post_process_outbox
                WHERE worklog_id = ? AND task_kind = ? AND target_id = ? AND request_id = ?
                  AND input_hash = ? AND execution_id = ? FOR UPDATE
                """, (rs, rowNum) -> {
            String state = rs.getString("execution_state");
            if ("RUNNING".equals(state) && rs.getBoolean("lease_valid")) return ResultAuthority.APPLY;
            if (("COMPLETED".equals(status) ? "SUCCEEDED" : "FAILED").equals(state) && digest.equals(rs.getString("result_digest"))) {
                return ResultAuthority.DUPLICATE;
            }
            return ResultAuthority.REJECTED;
        }, worklogId, identity.taskKind(), identity.targetId(), identity.requestId(), identity.inputHash(), identity.executionId())
                .stream().findFirst().orElse(ResultAuthority.REJECTED);
    }

    /** 결과·도메인·알림을 한 트랜잭션에서 종결해 모순된 재통보를 막는다. */
    @Transactional(propagation = Propagation.MANDATORY)
    public void completeResult(Long worklogId, AiTaskApiDto.Identity identity, String status, String digest, String errorCode) {
        int changed = jdbcTemplate.update("""
                UPDATE tb_worklog_ai_post_process_outbox SET execution_state = ?, result_digest = ?, result_error_code = ?,
                    delivery_state = 'ACCEPTED', claim_token = NULL, lease_until = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE worklog_id = ? AND task_kind = ? AND target_id = ? AND request_id = ?
                  AND input_hash = ? AND execution_id = ? AND execution_state = 'RUNNING' AND lease_until > CURRENT_TIMESTAMP
                """, "COMPLETED".equals(status) ? "SUCCEEDED" : "FAILED", digest, errorCode,
                worklogId, identity.taskKind(), identity.targetId(), identity.requestId(), identity.inputHash(), identity.executionId());
        if (changed != 1) throw new IllegalStateException("AI 작업 결과 기한이 만료되었습니다");
    }

    /** 같은 실행의 시작 재전송만 허용하고 최신 요청의 다른 동시 실행은 거절한다. */
    public Optional<UUID> start(Long worklogId, AiTaskApiDto.Identity identity) {
        return jdbcTemplate.query("""
                UPDATE tb_worklog_ai_post_process_outbox SET execution_id = ?, execution_state = 'RUNNING',
                    delivery_state = 'ACCEPTED', updated_at = CURRENT_TIMESTAMP
                WHERE worklog_id = ? AND task_kind = ? AND target_id = ? AND request_id = ? AND input_hash = ?
                  AND lease_until > CURRENT_TIMESTAMP
                  AND (execution_state = 'QUEUED' OR (execution_state = 'RUNNING' AND execution_id = ?))
                RETURNING execution_id
                """, (rs, rowNum) -> rs.getObject(1, UUID.class), identity.executionId(), worklogId, identity.taskKind(),
                identity.targetId(), identity.requestId(), identity.inputHash(), identity.executionId()).stream().findFirst();
    }

    /** 동일 실패는 backoff를 바꾸지 않으며 한도 소진 시 최종 실패 콜백을 기다린다. */
    public AiTaskApiDto.TransitionResponse retryableFailure(Long worklogId, AiTaskApiDto.Identity identity, String errorCode) {
        return jdbcTemplate.query("""
                UPDATE tb_worklog_ai_post_process_outbox
                SET execution_state = CASE WHEN attempts < ? THEN 'RETRY_READY' ELSE execution_state END,
                    delivery_state = CASE WHEN attempts < ? THEN 'PENDING' ELSE delivery_state END,
                    next_attempt_at = CASE WHEN execution_state = 'RUNNING' AND attempts < ?
                        THEN CURRENT_TIMESTAMP + (POWER(2, LEAST(attempts - 1, 8)) * INTERVAL '1 second') ELSE next_attempt_at END,
                    lease_until = CASE WHEN attempts < ? THEN NULL ELSE lease_until END,
                    claim_token = NULL, result_error_code = ?, updated_at = CURRENT_TIMESTAMP
                WHERE worklog_id = ? AND task_kind = ? AND target_id = ? AND request_id = ? AND input_hash = ? AND execution_id = ?
                  AND ((execution_state = 'RUNNING' AND lease_until > CURRENT_TIMESTAMP) OR execution_state = 'RETRY_READY')
                RETURNING execution_state = 'RETRY_READY'
                """, (rs, rowNum) -> new AiTaskApiDto.TransitionResponse(true, rs.getBoolean(1)),
                budget.maxAttempts(), budget.maxAttempts(), budget.maxAttempts(), budget.maxAttempts(), errorCode,
                worklogId, identity.taskKind(), identity.targetId(), identity.requestId(), identity.inputHash(), identity.executionId())
                .stream().findFirst().orElse(new AiTaskApiDto.TransitionResponse(false, false));
    }
}
