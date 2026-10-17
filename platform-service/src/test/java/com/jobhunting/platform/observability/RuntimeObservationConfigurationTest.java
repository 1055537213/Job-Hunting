package com.jobhunting.platform.observability;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.mock;

import com.jobhunting.platform.auth.AccountAdministrationService;
import com.jobhunting.platform.security.WebSecurityConfiguration;
import io.micrometer.core.instrument.MeterRegistry;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import org.junit.jupiter.api.Test;
import org.springframework.boot.test.context.runner.ApplicationContextRunner;
import org.springframework.boot.web.servlet.FilterRegistrationBean;
import org.springframework.core.Ordered;
import org.springframework.data.redis.core.StringRedisTemplate;
import tools.jackson.databind.ObjectMapper;

class RuntimeObservationConfigurationTest {
    final ApplicationContextRunner runner = new ApplicationContextRunner()
            .withUserConfiguration(RuntimeRequestMetrics.class, RuntimeObservationConfiguration.class);

    @Test void authMustBeExplicitlyEnabled() {
        runner.run(context -> {
            assertThat(context).doesNotHaveBean(RuntimeRequestMetrics.class);
            assertThat(context).doesNotHaveBean("runtimeObservationFilter");
        });
        runner.withPropertyValues("platform.auth.enabled=false").run(context -> {
            assertThat(context).doesNotHaveBean(RuntimeRequestMetrics.class);
            assertThat(context).doesNotHaveBean("runtimeObservationFilter");
        });
        runner.withPropertyValues("platform.auth.enabled=true").run(context -> {
            assertThat(context).hasSingleBean(RuntimeRequestMetrics.class);
            var registration = context.getBean("runtimeObservationFilter", FilterRegistrationBean.class);
            assertThat(registration.getFilter()).isInstanceOf(RuntimeObservationFilter.class);
            assertThat(registration.getOrder()).isEqualTo(Ordered.HIGHEST_PRECEDENCE);
            assertThat(registration.getUrlPatterns()).containsExactlyInAnyOrder("/api/*", "/internal/*");
            assertThat(registration.isAsyncSupported()).isTrue();
        });
    }

    @Test void observationIsRegisteredAheadOfExistingSecurityFilter() {
        runner.withUserConfiguration(WebSecurityConfiguration.class)
                .withPropertyValues("platform.auth.enabled=true", "platform.web-security.enabled=true",
                        "platform.web-security.key-prefix=test:runtime", "platform.web-security.window-seconds=60",
                        "platform.web-security.auth-requests=20", "platform.web-security.admin-requests=120",
                        "platform.web-security.write-requests=120", "platform.web-security.default-requests=240")
                .withBean(AccountAdministrationService.class, () -> mock(AccountAdministrationService.class))
                .withBean(StringRedisTemplate.class, () -> mock(StringRedisTemplate.class))
                .withBean(ObjectMapper.class, ObjectMapper::new)
                .withBean(MeterRegistry.class, SimpleMeterRegistry::new)
                .run(context -> {
                    assertThat(context).hasNotFailed();
                    var observation = context.getBean("runtimeObservationFilter", FilterRegistrationBean.class);
                    var security = context.getBean("webSecurityFilter", FilterRegistrationBean.class);
                    assertThat(observation.getOrder()).isEqualTo(Integer.MIN_VALUE);
                    assertThat(security.getOrder()).isEqualTo(Integer.MIN_VALUE + 1);
                    assertThat(security.getUrlPatterns()).containsExactly("/api/*");
                });
    }
}
