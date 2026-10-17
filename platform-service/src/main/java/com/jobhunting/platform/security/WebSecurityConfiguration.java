package com.jobhunting.platform.security;

import com.jobhunting.platform.auth.AccountAdministrationService;
import io.micrometer.core.instrument.MeterRegistry;
import org.springframework.boot.autoconfigure.condition.ConditionalOnExpression;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.boot.web.servlet.FilterRegistrationBean;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.core.Ordered;
import org.springframework.data.redis.core.StringRedisTemplate;
import tools.jackson.databind.ObjectMapper;

@Configuration
@EnableConfigurationProperties(WebSecuritySettings.class)
public class WebSecurityConfiguration {
    public WebSecurityConfiguration(WebSecuritySettings settings,
            @Value("${spring.data.redis.password:}") String redisPassword,
            @Value("${platform.internal-token:}") String token) {
        if ("production".equals(settings.environment()) && (redisPassword.isBlank() || token.length() < 32)) {
            throw new IllegalArgumentException("Production requires Redis authentication and a strong internal service token");
        }
    }

    @Bean
    ClientAddressResolver clientAddressResolver(@Value("${platform.ingress.trusted-proxy-cidrs:}") String cidrs,
            @Value("${server.forward-headers-strategy:none}") String strategy) {
        if (!"none".equalsIgnoreCase(strategy.strip())) {
            throw new IllegalArgumentException("Automatic forwarding must be disabled; use explicit trusted proxies");
        }
        return new ClientAddressResolver(cidrs);
    }

    @Bean
    @ConditionalOnExpression("${platform.web-security.enabled:false} and ${platform.auth.enabled:false}")
    RedisRequestLimiter requestLimiter(StringRedisTemplate redis, WebSecuritySettings settings) {
        return new RedisRequestLimiter(redis, settings);
    }

    @Bean
    @ConditionalOnExpression("${platform.web-security.enabled:false} and ${platform.auth.enabled:false}")
    FilterRegistrationBean<WebSecurityFilter> webSecurityFilter(
            AccountAdministrationService accounts, RedisRequestLimiter limiter,
            WebSecuritySettings settings, ObjectMapper mapper, MeterRegistry metrics, ClientAddressResolver addresses) {
        var registration = new FilterRegistrationBean<>(new WebSecurityFilter(accounts, limiter, settings, mapper, metrics, addresses));
        registration.addUrlPatterns("/api/*");
        registration.setOrder(Ordered.HIGHEST_PRECEDENCE + 1);
        return registration;
    }
}
