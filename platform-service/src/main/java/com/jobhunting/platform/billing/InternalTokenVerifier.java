package com.jobhunting.platform.billing;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

@Component
public class InternalTokenVerifier {

    private final byte[] configuredToken;

    public InternalTokenVerifier(@Value("${platform.internal-token:}") String configuredToken) {
        this.configuredToken = configuredToken.getBytes(StandardCharsets.UTF_8);
    }

    public void verify(String receivedToken) {
        byte[] candidate = receivedToken == null
                ? new byte[0]
                : receivedToken.getBytes(StandardCharsets.UTF_8);
        if (configuredToken.length == 0
                || !MessageDigest.isEqual(configuredToken, candidate)) {
            throw new BillingException(
                    "INTERNAL_UNAUTHORIZED",
                    "内部服务认证失败。",
                    org.springframework.http.HttpStatus.UNAUTHORIZED);
        }
    }
}
