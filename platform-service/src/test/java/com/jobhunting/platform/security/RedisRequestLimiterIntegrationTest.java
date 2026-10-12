package com.jobhunting.platform.security;

import static org.assertj.core.api.Assertions.*;
import java.util.UUID;
import java.util.concurrent.Executors;
import java.util.stream.IntStream;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfSystemProperty;
import org.springframework.data.redis.connection.RedisStandaloneConfiguration;
import org.springframework.data.redis.connection.jedis.JedisConnectionFactory;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.testcontainers.containers.GenericContainer;
import org.testcontainers.junit.jupiter.Container;
import org.testcontainers.junit.jupiter.Testcontainers;

@Testcontainers
@EnabledIfSystemProperty(named = "run.integration.tests", matches = "true")
class RedisRequestLimiterIntegrationTest {
    @Container static final GenericContainer<?> redis = new GenericContainer<>("redis:7.4-alpine").withExposedPorts(6379);

    @Test void twoReplicasShareAtomicWindowAndExpiredKeysRecover() throws Exception {
        var factory = new JedisConnectionFactory(new RedisStandaloneConfiguration(redis.getHost(), redis.getMappedPort(6379)));
        factory.afterPropertiesSet();
        factory.start();
        try {
            var template = new StringRedisTemplate(factory);
            var settings = new WebSecuritySettings(true, "test", true, false, "http://localhost",
                    "test:limit:" + UUID.randomUUID(), 1, 3, 3, 3, 3);
            var first = new RedisRequestLimiter(template, settings);
            var second = new RedisRequestLimiter(template, settings);
            // Use a long separate window to avoid time-dependent concurrent boundary tests.
            var concurrent = new WebSecuritySettings(true, "test", true, false, "http://localhost",
                    "test:concurrent:" + UUID.randomUUID(), 60, 3, 3, 3, 3);
            var replicas = java.util.List.of(new RedisRequestLimiter(template, concurrent), new RedisRequestLimiter(template, concurrent));
            try (var pool = Executors.newFixedThreadPool(8)) {
                var tasks = IntStream.range(0, 20).<java.util.concurrent.Callable<Long>>mapToObj(i ->
                        () -> replicas.get(i % 2).check("ip:shared", "auth")).toList();
                var results = pool.invokeAll(tasks);
                long allowed = 0;
                for (var result : results) if (result.get() == 0) allowed++;
                assertThat(allowed).isEqualTo(3);
            }
            assertThat(first.check("account:2", "auth")).isZero();
            assertThat(second.check("account:2", "auth")).isZero();
            assertThat(first.check("account:2", "auth")).isZero();
            assertThat(second.check("account:2", "auth")).isEqualTo(1);
            Thread.sleep(1100);
            assertThat(first.check("account:2", "auth")).isZero();
            assertThat(second.check("account:other", "auth")).isZero();
        } finally {
            factory.destroy();
        }
    }
}
