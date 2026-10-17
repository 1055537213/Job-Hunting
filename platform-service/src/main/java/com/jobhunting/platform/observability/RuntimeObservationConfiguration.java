package com.jobhunting.platform.observability;

import jakarta.servlet.DispatcherType;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.boot.web.servlet.FilterRegistrationBean;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.core.Ordered;

@Configuration(proxyBeanMethods = false)
@ConditionalOnProperty(name = "platform.auth.enabled", havingValue = "true")
public class RuntimeObservationConfiguration {
    @Bean
    FilterRegistrationBean<RuntimeObservationFilter> runtimeObservationFilter(RuntimeRequestMetrics metrics) {
        var registration = new FilterRegistrationBean<>(new RuntimeObservationFilter(metrics));
        registration.addUrlPatterns("/api/*", "/internal/*");
        registration.setDispatcherTypes(DispatcherType.REQUEST);
        registration.setAsyncSupported(true);
        registration.setOrder(Ordered.HIGHEST_PRECEDENCE);
        return registration;
    }
}
