package com.ibank.axwms.domain.file.repository;

import com.ibank.axwms.domain.file.entity.File;
import com.ibank.axwms.domain.file.repository.jooq.FileJooqRepository;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.Collection;
import java.util.List;
import java.util.Optional;
import jakarta.persistence.LockModeType;
import org.springframework.data.jpa.repository.Lock;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

public interface FileRepository extends JpaRepository<File, Long>, FileJooqRepository {

    /** 삭제 검증용: 요청된 fileId 들이 해당 worklog 에 속하면서 아직 살아있는 (isDeleted=false) 행만 조회한다. */
    List<File> findAllByIdInAndWorklogIdAndIsDeletedFalse(Collection<Long> ids, Long worklogId);

    /** 파일 삭제와 AI 결과 적용이 교차하지 않도록 현재 첨부 행을 잠근다. */
    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @Query("select file from File file where file.id = :fileId")
    Optional<File> findByIdForUpdate(@Param("fileId") Long fileId);
}
