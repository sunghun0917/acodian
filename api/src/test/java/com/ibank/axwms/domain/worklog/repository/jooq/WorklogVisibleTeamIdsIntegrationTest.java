package com.ibank.axwms.domain.worklog.repository.jooq;

import com.ibank.axwms.domain.worklog.policy.WorklogVisibilityScope;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;
import org.testcontainers.junit.jupiter.Container;
import org.testcontainers.junit.jupiter.Testcontainers;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;

import static org.assertj.core.api.Assertions.assertThat;

@Testcontainers
class WorklogVisibleTeamIdsIntegrationTest {

    @Container
    private static final PostgreSQLContainer<?> POSTGRES =
            new PostgreSQLContainer<>("pgvector/pgvector:pg17");

    private Connection connection;
    private DSLContext dsl;
    private WorklogJooqRepositoryImpl repository;

    @BeforeEach
    void setUp() throws SQLException {
        connection = DriverManager.getConnection(
                POSTGRES.getJdbcUrl(), POSTGRES.getUsername(), POSTGRES.getPassword());
        dsl = DSL.using(connection, SQLDialect.POSTGRES);
        dsl.execute("create table if not exists tb_team (team_id bigint primary key, department_id bigint, deleted_at timestamp)");
        dsl.execute("create table if not exists tb_worklog (worklog_id bigint primary key, team_id bigint, is_deleted boolean)");
        dsl.execute("create table if not exists tb_user_team (user_id bigint, team_id bigint, status_code text)");
        dsl.execute("create table if not exists tb_team_admin (user_id bigint, team_id bigint)");
        dsl.execute("truncate tb_team, tb_worklog, tb_user_team, tb_team_admin");
        repository = new WorklogJooqRepositoryImpl(dsl);
    }

    @AfterEach
    void closeConnection() throws SQLException {
        if (connection != null) {
            connection.close();
        }
    }

    @Test
    void emptyDepartmentTeamsAreNotSentToAi() {
        saveTeam(10, 7, false);
        saveTeam(20, 7, false);

        assertThat(repository.findVisibleTeamIds(new WorklogVisibilityScope.Department(7L, 5L)))
                .isEmpty();
    }

    @Test
    void mixedScopeKeepsOnlyTeamsWithNonDeletedWorklogs() {
        saveTeam(10, 7, false);
        saveTeam(20, 7, false);
        saveTeam(30, 7, false);
        saveWorklog(10, false);
        saveWorklog(30, true);

        assertThat(repository.findVisibleTeamIds(new WorklogVisibilityScope.Department(7L, 5L)))
                .containsExactly(10L);
    }

    @Test
    void departmentScopeStillExcludesForeignAndDeletedTeams() {
        saveTeam(10, 7, false);
        saveTeam(20, 8, false);
        saveTeam(30, 7, true);
        saveWorklog(10, false);
        saveWorklog(20, false);
        saveWorklog(30, false);

        assertThat(repository.findVisibleTeamIds(new WorklogVisibilityScope.Department(7L, 5L)))
                .containsExactly(10L);
    }

    @Test
    void memberAndAdminScopeStillRequiresWorklogsAndPermission() {
        for (long id : new long[]{10, 20, 30, 40}) {
            saveTeam(id, 7, false);
        }
        saveWorklog(10, false);
        saveWorklog(30, false);
        saveWorklog(40, false);
        dsl.execute("insert into tb_user_team values (5, 10, 'ACTIVE'), (5, 20, 'ACTIVE')");
        dsl.execute("insert into tb_team_admin values (5, 30)");

        assertThat(repository.findVisibleTeamIds(new WorklogVisibilityScope.MyTeams(5L)))
                .containsExactlyInAnyOrder(10L, 30L);
    }

    /** 삭제된 팀까지 포함한 권한 경계를 운영 DB가 아닌 전용 컨테이너에서 검증한다. */
    private void saveTeam(long teamId, long departmentId, boolean deleted) {
        dsl.execute("insert into tb_team values (?, ?, case when ? then current_timestamp else null end)",
                teamId, departmentId, deleted);
    }

    /** 본문이나 색인 완료 여부가 아닌 미삭제 업무의 존재만으로 검색 대상 계약을 검증한다. */
    private void saveWorklog(long teamId, boolean deleted) {
        dsl.execute("insert into tb_worklog values (?, ?, ?)", teamId, teamId, deleted);
    }
}
