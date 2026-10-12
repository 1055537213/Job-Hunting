package com.jobhunting.platform.security;

import org.springframework.boot.context.properties.ConfigurationProperties;

@ConfigurationProperties("platform.web-security")
public record WebSecuritySettings(
        boolean enabled, String environment, boolean csrfEnabled, boolean cookieSecure,
        String publicBaseUrl, String keyPrefix, int windowSeconds,
        int authRequests, int adminRequests, int writeRequests, int defaultRequests) {
    public WebSecuritySettings {
        environment = environment == null ? "development" : environment.strip().toLowerCase(java.util.Locale.ROOT);
        if (!java.util.Set.of("development", "test", "production").contains(environment)) {
            throw new IllegalArgumentException("Unknown platform environment");
        }
        if (keyPrefix == null || !keyPrefix.matches("[A-Za-z0-9:_-]{1,96}")
                || windowSeconds < 1 || windowSeconds > 86400
                || authRequests < 1 || adminRequests < 1 || writeRequests < 1 || defaultRequests < 1) {
            throw new IllegalArgumentException("Invalid platform web security settings");
        }
        if ("production".equals(environment)
                && (!enabled || !csrfEnabled || !cookieSecure || publicBaseUrl == null
                    || !publicBaseUrl.startsWith("https://"))) {
            throw new IllegalArgumentException("Production requires web security, CSRF, Secure cookies and HTTPS");
        }
    }

    public int limit(String group) {
        return switch (group) {
            case "auth" -> authRequests;
            case "admin" -> adminRequests;
            case "write" -> writeRequests;
            default -> defaultRequests;
        };
    }
}
