package com.ibank.axwms.domain.worklog.dto;

import static org.assertj.core.api.Assertions.assertThat;

import jakarta.validation.Validation;
import java.util.UUID;
import java.util.Arrays;
import java.util.List;
import org.junit.jupiter.api.Test;

class AiTaskApiDtoTest {
    private final jakarta.validation.Validator validator = Validation.buildDefaultValidatorFactory().getValidator();

    @Test
    void validIdentityAndProviderReservationAreAccepted() {
        var request = new AiTaskApiDto.Identity(UUID.randomUUID(), UUID.randomUUID(), "a".repeat(64), "SUMMARY", 1L);
        assertThat(validator.validate(request)).isEmpty();
    }

    @Test
    void invalidIdentityDoesNotPassValidation() {
        var request = new AiTaskApiDto.Identity(null, null, "invalid", "OTHER", 0L);
        assertThat(validator.validate(request)).isNotEmpty();
    }

    @Test
    void resultRejectsNullOrExcessiveTagEntries() {
        var withNull = new AiTaskApiDto.Result(UUID.randomUUID(), UUID.randomUUID(), "a".repeat(64), "TAG", 1L, "COMPLETED", null, null, Arrays.asList(1L, null), List.of());
        assertThat(validator.validate(withNull)).isNotEmpty();
        var tooMany = new AiTaskApiDto.Result(UUID.randomUUID(), UUID.randomUUID(), "a".repeat(64), "TAG", 1L, "COMPLETED", null, null, List.of(1L, 2L, 3L, 4L, 5L, 6L), List.of());
        assertThat(validator.validate(tooMany)).isNotEmpty();
    }
}
