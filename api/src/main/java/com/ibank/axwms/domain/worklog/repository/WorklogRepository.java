package com.ibank.axwms.domain.worklog.repository;

import com.ibank.axwms.domain.worklog.entity.Worklog;
import com.ibank.axwms.domain.worklog.repository.jooq.WorklogJooqRepository;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

public interface WorklogRepository extends JpaRepository<Worklog, Long>, WorklogJooqRepository {

    /** 선행 후보의 상태·버전이 조회 시점과 같을 때만 버전을 점유해 동시 변경을 감지한다. */
    @Modifying
    @Query(value = """
            UPDATE tb_worklog SET version = version + 1
            WHERE worklog_id = :worklogId AND version = :version
              AND team_id = :teamId AND is_deleted = false AND status_code <> 'COMPLETED'
            """, nativeQuery = true)
    int claimPredecessorVersion(@Param("worklogId") Long worklogId,
                                @Param("version") Long version,
                                @Param("teamId") Long teamId);
}
