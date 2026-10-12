package com.jobhunting.platform.security;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import io.micrometer.prometheusmetrics.PrometheusMeterRegistry;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RestController;

@RestController
@ConditionalOnProperty(prefix = "platform.metrics", name = "enabled", havingValue = "true")
public class InternalMetricsController {
    private final PrometheusMeterRegistry registry;
    private final byte[] expected;

    public InternalMetricsController(PrometheusMeterRegistry registry,
            @Value("${platform.metrics.token:}") String token,
            @Value("${platform.metrics.token-file:}") String tokenFile,
            @Value("${platform.internal-token:}") String internalToken) {
        this.registry = registry;
        if (!tokenFile.isBlank()) {
            try {
                token = Files.readString(Path.of(tokenFile), StandardCharsets.UTF_8).strip();
            } catch (IOException error) {
                throw new IllegalArgumentException("Cannot read metrics credential");
            }
        }
        if (!token.matches("[A-Za-z0-9._-]{32,256}") || token.equals(internalToken)) {
            throw new IllegalArgumentException("Metrics require an independent token of at least 32 characters");
        }
        expected = ("Bearer " + token).getBytes(StandardCharsets.UTF_8);
    }

    @GetMapping(value = "/internal/metrics", produces = "text/plain;version=0.0.4;charset=utf-8")
    public ResponseEntity<String> scrape(@RequestHeader(value = "Authorization", required = false) String authorization) {
        if (authorization == null || authorization.length() > 263
                || !MessageDigest.isEqual(expected, authorization.getBytes(StandardCharsets.UTF_8))) {
            return ResponseEntity.status(HttpStatus.UNAUTHORIZED).header("WWW-Authenticate", "Bearer")
                    .header("Cache-Control", "no-store").body("Metrics authentication required\n");
        }
        return ResponseEntity.ok().header("Cache-Control", "no-store")
                .header("X-Content-Type-Options", "nosniff").body(registry.scrape());
    }
}
