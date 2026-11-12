package com.ibank.axwms.domain.worklog.repository;

import java.util.Optional;
import lombok.RequiredArgsConstructor;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

/** 입력 수정·삭제와 일반 AI 콜백의 업무 잠금 순서를 공유한다. */
@Repository
@RequiredArgsConstructor
public class WorklogAiPostProcessOutboxRepository {
    private final JdbcTemplate jdbcTemplate;

    /** 업무를 먼저 잠가 대상별 Outbox 교체·결과·수동 재시도를 직렬화한다. */
    public Optional<Boolean> lockWorklog(Long worklogId) {
        return jdbcTemplate.queryForList("SELECT is_deleted FROM tb_worklog WHERE worklog_id = ? FOR UPDATE",
                Boolean.class, worklogId).stream().findFirst();
    }

}
