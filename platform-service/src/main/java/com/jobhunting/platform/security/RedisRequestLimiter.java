package com.jobhunting.platform.security;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.HexFormat;
import java.util.List;
import java.util.UUID;

import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.script.DefaultRedisScript;

/** Redis TIME and one atomic script keep the sliding window consistent across replicas. */
public class RedisRequestLimiter {
    private static final DefaultRedisScript<Long> SCRIPT = new DefaultRedisScript<>("""
            local key = KEYS[1]
            local window = tonumber(ARGV[1])
            local limit = tonumber(ARGV[2])
            local time = redis.call('TIME')
            local now = tonumber(time[1]) * 1000 + math.floor(tonumber(time[2]) / 1000)
            redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
            if redis.call('ZCARD', key) >= limit then
                local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
                redis.call('PEXPIRE', key, window)
                return math.max(1, math.ceil((tonumber(oldest[2]) + window - now) / 1000))
            end
            redis.call('ZADD', key, now, ARGV[3])
            redis.call('PEXPIRE', key, window)
            return 0
            """, Long.class);
    private final StringRedisTemplate redis;
    private final WebSecuritySettings settings;

    public RedisRequestLimiter(StringRedisTemplate redis, WebSecuritySettings settings) {
        this.redis = redis;
        this.settings = settings;
    }

    public long check(String identity, String group) {
        try {
            String digest = HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256")
                    .digest(identity.getBytes(StandardCharsets.UTF_8))).substring(0, 32);
            Long result = redis.execute(SCRIPT, List.of(settings.keyPrefix() + ":" + group + ":" + digest),
                    Integer.toString(settings.windowSeconds() * 1000),
                    Integer.toString(settings.limit(group)), UUID.randomUUID().toString());
            if (result == null || result < 0 || result > settings.windowSeconds()) {
                throw new IllegalStateException("Invalid limiter response");
            }
            return result;
        } catch (RuntimeException | NoSuchAlgorithmException error) {
            // Do not include Redis URLs, credentials or request identities in exceptions/logs.
            throw new Unavailable(error);
        }
    }

    public static class Unavailable extends RuntimeException {
        public Unavailable() { super("Request protection unavailable"); }
        public Unavailable(Throwable cause) { super("Request protection unavailable", cause); }
    }
}
