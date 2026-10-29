package com.ibank.axwms;

import com.ibank.axwms.testsupport.IntegrationTestSupport;
import java.io.IOException;
import org.junit.jupiter.api.Test;
import org.springframework.boot.env.YamlPropertySourceLoader;
import org.springframework.core.io.FileSystemResource;

import static org.assertj.core.api.Assertions.assertThat;

class AxWmsApplicationTests extends IntegrationTestSupport {

    @Test
    void contextLoads() {
    }

    @Test
    void mainApplicationYamlLoadsLightIndexSettings() throws IOException {
        // 테스트 클래스패스의 동명 설정이 실제 기동 설정의 문법 오류를 가리지 않도록 원본을 읽는다.
        var sources = new YamlPropertySourceLoader().load("main-application",
                new FileSystemResource("src/main/resources/application.yml"));

        assertThat(sources).hasSize(1);
        var source = sources.getFirst();
        assertThat(source.containsProperty("ai.worklog-light-index.enabled")).isFalse();
        assertThat(source.containsProperty("ai.worklog-light-index.update-enabled")).isFalse();
        assertThat(source.containsProperty("ai.worklog-light-index.delete-enabled")).isFalse();
        assertThat(source.containsProperty("ai.worklog-light-index.read-timeout")).isTrue();
        assertThat(source.containsProperty("ai.worklog-light-index.internal-token")).isTrue();
    }

}
