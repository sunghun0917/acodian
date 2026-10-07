package com.ibank.axwms.domain.worklog.service;

import com.ibank.axwms.domain.worklog.WorklogImportance;
import com.ibank.axwms.domain.worklog.WorklogStatus;
import com.ibank.axwms.domain.worklog.entity.Worklog;
import com.ibank.axwms.domain.worklog.repository.WorklogDependencyRepository;
import com.ibank.axwms.domain.worklog.repository.WorklogRepository;
import com.ibank.axwms.global.error.BusinessException;
import com.ibank.axwms.global.error.ErrorCode;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.InjectMocks;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.http.HttpStatus;
import org.springframework.test.util.ReflectionTestUtils;

import java.util.List;
import java.util.Map;
import java.util.Set;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.BDDMockito.given;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;

@ExtendWith(MockitoExtension.class)
class WorklogDependencyServiceTest {

    @Mock private WorklogDependencyRepository dependencyRepository;
    @Mock private WorklogRepository worklogRepository;
    @InjectMocks private WorklogDependencyService service;

    @Test
    @DisplayName("완료된 업무는 새 선행 업무로 등록하지 않는다")
    void completed_predecessor_is_rejected() {
        Worklog completed = Worklog.create(11L, 21L, "완료 업무", null, "내용",
                WorklogStatus.COMPLETED, WorklogImportance.NORMAL, null, null, null);
        ReflectionTestUtils.setField(completed, "id", 31L);
        given(worklogRepository.findTeamIdsByWorklogIds(Set.of(31L))).willReturn(Map.of(31L, 21L));
        given(worklogRepository.findAllById(Set.of(31L))).willReturn(List.of(completed));

        assertThatThrownBy(() -> service.registerPredecessor(41L, 21L, List.of(31L)))
                .isInstanceOf(BusinessException.class)
                .satisfies(error -> assertThat(((BusinessException) error).getErrorCode().getStatus())
                        .isEqualTo(HttpStatus.CONFLICT));
        verify(dependencyRepository, never()).saveAll(any());
    }

    @Test
    @DisplayName("조회 후 선행 업무가 바뀌면 연결을 저장하지 않는다")
    void changed_predecessor_is_rejected_before_link_save() {
        Worklog inProgress = Worklog.create(11L, 21L, "진행 중 업무", null, "내용",
                WorklogStatus.IN_PROGRESS, WorklogImportance.NORMAL, null, null, null);
        ReflectionTestUtils.setField(inProgress, "id", 31L);
        given(worklogRepository.findTeamIdsByWorklogIds(Set.of(31L))).willReturn(Map.of(31L, 21L));
        given(worklogRepository.findAllById(Set.of(31L))).willReturn(List.of(inProgress));

        assertThatThrownBy(() -> service.registerPredecessor(41L, 21L, List.of(31L)))
                .isInstanceOf(BusinessException.class)
                .satisfies(error -> assertThat(((BusinessException) error).getErrorCode().getStatus())
                        .isEqualTo(HttpStatus.CONFLICT));
        verify(dependencyRepository, never()).saveAll(any());
    }

    @Test
    @DisplayName("여러 선행 후보 중 하나가 충돌하면 연결을 한 건도 저장하지 않는다")
    void one_conflict_rejects_all_new_links() {
        Worklog first = Worklog.create(11L, 21L, "첫 선행", null, "내용",
                WorklogStatus.IN_PROGRESS, WorklogImportance.NORMAL, null, null, null);
        Worklog second = Worklog.create(11L, 21L, "둘째 선행", null, "내용",
                WorklogStatus.IN_PROGRESS, WorklogImportance.NORMAL, null, null, null);
        ReflectionTestUtils.setField(first, "id", 30L);
        ReflectionTestUtils.setField(second, "id", 31L);
        ReflectionTestUtils.setField(first, "version", 0L);
        ReflectionTestUtils.setField(second, "version", 0L);
        given(worklogRepository.findTeamIdsByWorklogIds(Set.of(30L, 31L)))
                .willReturn(Map.of(30L, 21L, 31L, 21L));
        given(worklogRepository.findAllById(Set.of(30L, 31L))).willReturn(List.of(second, first));
        given(worklogRepository.claimPredecessorVersion(30L, 0L, 21L)).willReturn(1);

        assertThatThrownBy(() -> service.registerPredecessor(41L, 21L, List.of(31L, 30L)))
                .isInstanceOf(BusinessException.class)
                .extracting("errorCode").isEqualTo(ErrorCode.WORKLOG_PREDECESSOR_CONFLICT);
        verify(dependencyRepository, never()).saveAll(any());
    }

    @Test
    @DisplayName("수정에서 이미 연결된 선행 업무는 완료되어도 새 연결 검증을 하지 않는다")
    void unchanged_existing_predecessor_is_not_reclaimed() {
        given(dependencyRepository.findDependsOnWorklogIdsByWorklogId(41L)).willReturn(Set.of(31L));

        service.replacePredecessors(41L, 21L, List.of(31L));

        verify(dependencyRepository, never()).saveAll(any());
        verifyNoInteractions(worklogRepository);
    }
}
