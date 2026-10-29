package com.ibank.axwms.domain.worklog.service;

import static org.assertj.core.api.Assertions.assertThat;

import com.ibank.axwms.domain.organization.department.DepartmentStatus;
import com.ibank.axwms.domain.organization.department.entity.Department;
import com.ibank.axwms.domain.organization.department.repository.DepartmentRepository;
import com.ibank.axwms.domain.organization.team.TeamStatus;
import com.ibank.axwms.domain.organization.team.entity.Team;
import com.ibank.axwms.domain.organization.team.repository.TeamRepository;
import com.ibank.axwms.domain.organization.user.EmploymentStatus;
import com.ibank.axwms.domain.organization.user.UserRole;
import com.ibank.axwms.domain.organization.user.entity.User;
import com.ibank.axwms.domain.organization.user.repository.UserRepository;
import com.ibank.axwms.domain.worklog.WorklogImportance;
import com.ibank.axwms.domain.worklog.WorklogStatus;
import com.ibank.axwms.domain.worklog.entity.Worklog;
import com.ibank.axwms.domain.worklog.event.WorklogAiPostProcessRequestedEvent;
import com.ibank.axwms.domain.worklog.repository.WorklogAiPostProcessOutboxRepository;
import com.ibank.axwms.domain.worklog.repository.WorklogLightOutboxRepository;
import com.ibank.axwms.domain.worklog.repository.WorklogRepository;
import com.ibank.axwms.global.enums.AiProcessingStatus;
import com.ibank.axwms.testsupport.IntegrationTestSupport;
import java.math.BigDecimal;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.time.LocalDate;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.server.LocalServerPort;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.data.redis.connection.lettuce.LettuceConnectionFactory;
import org.springframework.test.context.TestPropertySource;

@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@EnabledIfEnvironmentVariable(named = "WORKLOG_CALLBACK_E2E_PYTHON", matches = ".+")
@TestPropertySource(properties = {
        "server.servlet.context-path=/api",
        "ai.worklog-post-process.outbox-poll-delay-ms=3600000",
        "ai.worklog-light-index.outbox-poll-delay-ms=3600000",
        "ai.worklog-pipeline.enabled=false",
        "ai.worklog-light-index.base-url=http://127.0.0.1:1/ai",
        "ai.worklog-light-index.internal-token=callback-e2e-internal-token"
})
class WorklogLightCallbackE2ETest extends IntegrationTestSupport {

    private static final String INTERNAL_TOKEN = "callback-e2e-internal-token";
    private static final LocalDate TODAY = LocalDate.of(2026, 10, 27);
    private static final long PROCESS_TIMEOUT_SECONDS = 45;

    @LocalServerPort
    private int port;

    @Autowired
    private JdbcTemplate jdbcTemplate;
    @Autowired
    private DepartmentRepository departmentRepository;
    @Autowired
    private UserRepository userRepository;
    @Autowired
    private TeamRepository teamRepository;
    @Autowired
    private WorklogRepository worklogRepository;
    @Autowired
    private WorklogAiPostProcessOutboxRepository registrationOutbox;
    @Autowired
    private WorklogLightOutboxRepository mutationOutbox;
    @Autowired
    private WorklogAiPostProcessOutboxWorker registrationWorker;
    @Autowired
    private LettuceConnectionFactory redisConnectionFactory;

    /** 전용 Testcontainers DB만 정리하고 스케줄러와의 경쟁은 긴 폴링 주기로 차단한다. */
    @BeforeEach
    void clearOutboxes() {
        jdbcTemplate.execute("TRUNCATE TABLE tb_worklog_ai_post_process_outbox, tb_worklog_light_outbox");
    }

    @Test
    @DisplayName("실제 AI 클라이언트 HTTP 완료 콜백이 등록 색인을 확정하고 후처리 Outbox를 정리한다")
    void actual_python_callback_completes_registration() throws Exception {
        Fixture fixture = seedWorklog();
        registrationOutbox.enqueue(event(fixture));
        mutationOutbox.enqueue(fixture.worklog().getId(), "INDEX");
        UUID request = currentRequest(fixture.worklog().getId());
        UUID execution = UUID.randomUUID();

        assertThat(callback(fixture.worklog().getId(), request, execution, "COMPLETED")).contains("\"execute\":true");

        assertThat(lightState(fixture.worklog().getId())).isEqualTo("COMPLETED");
        assertThat(registrationWorker.processOne(fixture.worklog().getId())).isTrue();
        assertThat(registrationCount(fixture.worklog().getId())).isZero();
        assertThat(worklogRepository.findById(fixture.worklog().getId()).orElseThrow().getAiProcessingStatus())
                .isEqualTo(AiProcessingStatus.COMPLETED);
        assertThat(driver(fixture.worklog().getId(), request, execution, "COMPLETED", "result", INTERNAL_TOKEN, 200))
                .contains("\"httpStatus\":200");
        assertThat(registrationCount(fixture.worklog().getId())).isZero();
    }

    @ParameterizedTest
    @ValueSource(strings = {"REINDEX", "DELETE"})
    @DisplayName("실제 HTTP 재색인·삭제 완료 콜백이 해당 수정 Outbox만 제거한다")
    void actual_python_callback_completes_mutation(String operation) throws Exception {
        Fixture fixture = seedWorklog();
        Worklog worklog = fixture.worklog();
        if ("DELETE".equals(operation)) {
            worklog.softDelete();
            worklogRepository.saveAndFlush(worklog);
        }
        mutationOutbox.enqueue(worklog.getId(), operation);

        assertThat(callback(worklog.getId(), currentRequest(worklog.getId()), UUID.randomUUID(), "COMPLETED"))
                .contains("\"execute\":true");

        assertThat(lightState(worklog.getId())).isEqualTo("COMPLETED");
        assertThat(jdbcTemplate.queryForObject("SELECT COUNT(*) FROM tb_worklog_light_outbox WHERE worklog_id = ?", Integer.class, worklog.getId()))
                .isZero();
    }

    @Test
    @DisplayName("최종 실패 HTTP 콜백은 수동 복구 대기를 유지하고 모순된 재통보·중복 시작을 차단한다")
    void actual_failed_callback_preserves_manual_recovery() throws Exception {
        Fixture fixture = seedWorklog();
        Long id = fixture.worklog().getId();
        registrationOutbox.enqueue(event(fixture));
        mutationOutbox.enqueue(fixture.worklog().getId(), "INDEX");
        UUID request = currentRequest(id);
        UUID execution = UUID.randomUUID();
        callback(id, request, execution, "FAILED");
        assertThat(registrationWorker.processOne(id)).isTrue();

        driver(id, request, execution, "COMPLETED", "result", INTERNAL_TOKEN, 200);

        assertThat(lightState(id)).isEqualTo("FAILED");
        assertThat(worklogRepository.findById(id).orElseThrow().getAiProcessingStatus()).isEqualTo(AiProcessingStatus.FAILED);
        assertThat(registrationWorker.processOne(id)).isFalse();
        assertThat(registrationCount(id)).isEqualTo(1);
        assertThat(callback(id, request, UUID.randomUUID(), "COMPLETED")).contains("\"execute\":false");
    }

    @Test
    @DisplayName("과거 요청의 실제 HTTP 시작은 거절되고 최신 삭제 의도는 유지된다")
    void actual_stale_request_does_not_change_latest_intent() throws Exception {
        Worklog worklog = seedWorklog().worklog();
        mutationOutbox.enqueue(worklog.getId(), "REINDEX");
        UUID obsolete = currentRequest(worklog.getId());
        mutationOutbox.enqueue(worklog.getId(), "DELETE");
        UUID latest = currentRequest(worklog.getId());

        assertThat(callback(worklog.getId(), obsolete, UUID.randomUUID(), "COMPLETED")).contains("\"execute\":false");

        assertThat(currentRequest(worklog.getId())).isEqualTo(latest);
        assertThat(lightState(worklog.getId())).isEqualTo("PENDING");
        assertThat(jdbcTemplate.queryForObject("SELECT operation FROM tb_worklog_light_outbox WHERE worklog_id = ?", String.class, worklog.getId()))
                .isEqualTo("DELETE");
    }

    @ParameterizedTest
    @ValueSource(strings = {"start", "result"})
    @DisplayName("잘못된 내부 토큰은 실제 HTTP에서 403으로 거절되며 DB 상태를 변경하지 않는다")
    void actual_wrong_token_is_rejected(String mode) throws Exception {
        Worklog worklog = seedWorklog().worklog();
        mutationOutbox.enqueue(worklog.getId(), "REINDEX");

        assertThat(driver(worklog.getId(), currentRequest(worklog.getId()), UUID.randomUUID(), "COMPLETED", mode, "wrong-token", 403))
                .contains("\"httpStatus\":403");

        assertThat(lightState(worklog.getId())).isEqualTo("PENDING");
    }

    @ParameterizedTest
    @ValueSource(strings = {"INDEX", "REINDEX", "DELETE"})
    @DisplayName("실제 FastAPI 202→Redis→Celery→Spring HTTP 콜백이 등록·수정·삭제 결과를 반영한다")
    void actual_queue_pipeline_applies_callback(String operation) throws Exception {
        Fixture fixture = seedWorklog();
        Long id = fixture.worklog().getId();
        if ("INDEX".equals(operation)) {
            registrationOutbox.enqueue(event(fixture));
            mutationOutbox.enqueue(fixture.worklog().getId(), "INDEX");
        } else {
            if ("DELETE".equals(operation)) {
                fixture.worklog().softDelete();
                worklogRepository.saveAndFlush(fixture.worklog());
            }
            mutationOutbox.enqueue(id, operation);
        }

        String result = driver(id, currentRequest(id), UUID.randomUUID(), "COMPLETED", "pipeline", INTERNAL_TOKEN, 200, operation);

        assertThat(result).contains("\"httpStatus\":202", "\"indexCalls\":1");
        assertThat(lightState(id)).isEqualTo("COMPLETED");
        assertThat(jdbcTemplate.queryForObject("""
                SELECT light_index_execution_id IS NOT NULL AND light_index_completed_at IS NOT NULL
                FROM tb_worklog WHERE worklog_id = ?
                """, Boolean.class, id)).isTrue();
        if ("INDEX".equals(operation)) {
            assertThat(registrationWorker.processOne(id)).isTrue();
            assertThat(registrationCount(id)).isZero();
        } else {
            assertThat(jdbcTemplate.queryForObject("SELECT COUNT(*) FROM tb_worklog_light_outbox WHERE worklog_id = ?", Integer.class, id)).isZero();
        }
    }

    @ParameterizedTest
    @ValueSource(strings = {"COMPLETED", "FAILED"})
    @DisplayName("최초 콜백 HTTP 장애 후 AI가 보관 결과만 재전송하며 색인은 한 번만 실행한다")
    void callback_retry_does_not_repeat_indexing(String status) throws Exception {
        Fixture fixture = seedWorklog();
        Long id = fixture.worklog().getId();
        registrationOutbox.enqueue(event(fixture));
        mutationOutbox.enqueue(fixture.worklog().getId(), "INDEX");

        String result = driver(id, currentRequest(id), UUID.randomUUID(), status, "pipeline-retry", INTERNAL_TOKEN, 200, "INDEX");

        assertThat(result).contains("\"httpStatus\":202", "\"indexCalls\":1", "\"callbackRetried\":true");
        assertThat(lightState(id)).isEqualTo(status);
        assertThat(registrationWorker.processOne(id)).isTrue();
        if ("COMPLETED".equals(status)) {
            assertThat(registrationCount(id)).isZero();
        } else {
            assertThat(registrationCount(id)).isEqualTo(1);
            assertThat(registrationWorker.processOne(id)).isFalse();
            assertThat(worklogRepository.findById(id).orElseThrow().getAiProcessingStatus()).isEqualTo(AiProcessingStatus.FAILED);
        }
    }

    /** 외부 AI 연산 없이 콜백 경계의 실제 소켓·보안·DB 처리를 검증한다. */
    private String callback(Long id, UUID request, UUID execution, String status) throws Exception {
        return driver(id, request, execution, status, "complete", INTERNAL_TOKEN, 200);
    }

    /** AI에는 테스트 서버 주소·토큰만 전달하고 DB는 접근 불가능한 주소로 격리한다. */
    private String driver(Long id, UUID request, UUID execution, String status, String mode, String token, int expectedStatus) throws Exception {
        return driver(id, request, execution, status, mode, token, expectedStatus, "INDEX");
    }

    /** Testcontainers Redis 외의 브로커에 접근하지 않고 생산 태스크의 실제 큐 처리를 검증한다. */
    private String driver(Long id, UUID request, UUID execution, String status, String mode, String token, int expectedStatus, String operation) throws Exception {
        Path working = Path.of(System.getProperty("user.dir")).toAbsolutePath();
        Path repo = working.getFileName().toString().equals("api") ? working.getParent() : working;
        ProcessBuilder builder = new ProcessBuilder(System.getenv("WORKLOG_CALLBACK_E2E_PYTHON"),
                repo.resolve("ai/tests/e2e_callback_driver.py").toString(),
                "--worklog-id", id.toString(), "--request-id", request.toString(),
                "--execution-id", execution.toString(), "--status", status, "--mode", mode,
                "--expected-http-status", Integer.toString(expectedStatus), "--operation", operation);
        builder.directory(repo.resolve("ai").toFile()).redirectErrorStream(true);
        builder.environment().put("PYTHONPATH", repo.resolve("ai").toString());
        builder.environment().put("API_BASE_URL", "http://127.0.0.1:" + port + "/api");
        builder.environment().put("AI_INTERNAL_TOKEN", token);
        builder.environment().put("PGVECTOR_DSN", "postgresql://unused:unused@127.0.0.1:1/never_used");
        builder.environment().put("LANGFUSE_TRACING_ENABLED", "false");
        builder.environment().put("GEMINI_API_KEY", "unused");
        String redisUrl = "redis://" + redisConnectionFactory.getHostName() + ":" + redisConnectionFactory.getPort() + "/" + redisConnectionFactory.getDatabase();
        builder.environment().put("REDIS_URL", redisUrl);
        builder.environment().put("CELERY_BROKER_URL", redisUrl);
        builder.environment().put("CELERY_RESULT_BACKEND", redisUrl);
        builder.environment().put("CELERY_TASK_ALWAYS_EAGER", "false");
        Process process = builder.start();
        boolean finished = process.waitFor(PROCESS_TIMEOUT_SECONDS, TimeUnit.SECONDS);
        if (!finished) {
            process.destroyForcibly();
            process.waitFor(5, TimeUnit.SECONDS);
        }
        assertThat(finished).as("AI callback driver must terminate within its deadline").isTrue();
        String output = new String(process.getInputStream().readAllBytes(), StandardCharsets.UTF_8);
        assertThat(process.exitValue()).as("AI callback driver output: %s", output).isZero();
        return output;
    }

    /** 실제 FK를 충족하는 조직과 원본을 만들고 테스트 간 자연키 충돌을 피한다. */
    private Fixture seedWorklog() {
        String suffix = UUID.randomUUID().toString().substring(0, 8);
        Department department = Department.create("콜백부서-" + suffix, "HTTP E2E");
        department.changeStatus(DepartmentStatus.ACTIVE);
        department = departmentRepository.saveAndFlush(department);
        User user = userRepository.saveAndFlush(User.create(department.getId(), "작성자", suffix + "@e2e.test",
                "$2a$10$abcdefghijklmnopqrstuv", UserRole.MEMBER, EmploymentStatus.ACTIVE,
                "사원", null, TODAY, null, null));
        Team team = teamRepository.saveAndFlush(Team.create(department.getId(), "콜백팀-" + suffix,
                TeamStatus.ACTIVE, "HTTP E2E", TODAY, null));
        Worklog worklog = Worklog.create(user.getId(), team.getId(), "콜백 검증", "요청", "수행",
                WorklogStatus.PENDING, WorklogImportance.NORMAL, BigDecimal.ZERO, TODAY, TODAY.plusDays(1));
        worklog.startAiProcessing();
        return new Fixture(department, user, team, worklogRepository.saveAndFlush(worklog));
    }

    /** 파일 요약 인프라 없이 등록 색인의 통합 후처리 경계를 검증한다. */
    private WorklogAiPostProcessRequestedEvent event(Fixture fixture) {
        return new WorklogAiPostProcessRequestedEvent(fixture.worklog().getId(), fixture.worklog().getTitle(),
                fixture.worklog().getRequestContent(), fixture.worklog().getWorkContent(), fixture.user().getId(),
                fixture.team().getId(), fixture.department().getId(), List.of());
    }

    /** JPA 캐시에 의존하지 않고 현재 요청 권위를 DB에서 확인한다. */
    private UUID currentRequest(Long id) {
        return jdbcTemplate.queryForObject("SELECT light_index_request_id FROM tb_worklog WHERE worklog_id = ?", UUID.class, id);
    }

    /** HTTP 트랜잭션이 실제 커밋한 실행 상태를 확인한다. */
    private String lightState(Long id) {
        return jdbcTemplate.queryForObject("SELECT light_index_status FROM tb_worklog WHERE worklog_id = ?", String.class, id);
    }

    /** 다른 테스트 원본과 구분하여 등록 후처리의 완료 여부를 확인한다. */
    private int registrationCount(Long id) {
        return jdbcTemplate.queryForObject("SELECT COUNT(*) FROM tb_worklog_ai_post_process_outbox WHERE worklog_id = ?", Integer.class, id);
    }

    private record Fixture(Department department, User user, Team team, Worklog worklog) {
    }
}
