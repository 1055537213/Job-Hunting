package com.jobhunting.platform.security;

import java.util.Map;
import org.springframework.boot.autoconfigure.condition.ConditionalOnExpression;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

/** Protected by WebSecurityFilter; only non-sensitive active protection settings are projected. */
@RestController
@ConditionalOnExpression("${platform.web-security.enabled:false} and ${platform.auth.enabled:false}")
public class WebSecurityStatusController {
    private final WebSecuritySettings settings;
    private final ClientAddressResolver addresses;
    public WebSecurityStatusController(WebSecuritySettings settings, ClientAddressResolver addresses) {
        this.settings = settings;
        this.addresses = addresses;
    }

    @GetMapping("/api/admin/security")
    public Map<String, Object> status() {
        return Map.of("csrf_enabled", settings.csrfEnabled(), "cookie_secure", settings.cookieSecure(),
                "rate_limit_backend", "redis", "rate_limit_window_seconds", settings.windowSeconds(),
                "trusts_forwarded_headers", addresses.configured());
    }
}
