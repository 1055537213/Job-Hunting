package com.jobhunting.platform.billing;

import javax.sql.DataSource;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnExpression;
import org.springframework.boot.jdbc.DataSourceBuilder;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration
@ConditionalOnExpression("${platform.billing.enabled:false} or ${platform.auth.enabled:false}")
public class BillingConfiguration {

    @Bean
    DataSource billingDataSource(
            @Value("${spring.datasource.url}") String url,
            @Value("${spring.datasource.username}") String username,
            @Value("${spring.datasource.password}") String password) {
        if (url.isBlank() || username.isBlank()) {
            throw new IllegalStateException(
                    "启用平台账务前必须配置 SPRING_DATASOURCE_URL 和 SPRING_DATASOURCE_USERNAME。");
        }
        return DataSourceBuilder.create()
                .url(url)
                .username(username)
                .password(password)
                .driverClassName("org.postgresql.Driver")
                .build();
    }
}
