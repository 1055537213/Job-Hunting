package com.jobhunting.platform.billing;

import java.math.BigDecimal;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.validation.Valid;
import jakarta.validation.constraints.DecimalMax;
import jakarta.validation.constraints.DecimalMin;
import jakarta.validation.constraints.Digits;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Size;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.web.bind.annotation.*;

@RestController
@ConditionalOnProperty(prefix = "platform.auth", name = "enabled", havingValue = "true")
public class PersonalRechargeController {
    private final PersonalRechargeService service;
    private final InternalTokenVerifier tokens;

    public PersonalRechargeController(PersonalRechargeService service, InternalTokenVerifier tokens) {
        this.service = service;
        this.tokens = tokens;
    }

    @PostMapping("/api/me/balance/recharge")
    public BillingQueryService.RechargeResult recharge(@Valid @RequestBody RechargeRequest body, HttpServletRequest request) {
        String token = null;
        if (request.getCookies() != null) for (var cookie : request.getCookies()) {
            if ("job_agent_session".equals(cookie.getName())) { token = cookie.getValue(); break; }
        }
        if (token == null || token.isBlank()) throw new com.jobhunting.platform.auth.AuthException(
                "SESSION_EXPIRED", "登录状态已过期，请重新登录。", org.springframework.http.HttpStatus.UNAUTHORIZED);
        return service.recharge(token, body.amount_yuan(), body.note(), body.idempotency_key());
    }

    @PostMapping("/internal/v1/billing/me/recharge")
    public BillingQueryService.RechargeResult internal(@Valid @RequestBody InternalRechargeRequest body,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String token) {
        tokens.verify(token);
        return service.recharge(body.session_token(), body.payload().amount_yuan(), body.payload().note(), body.payload().idempotency_key());
    }

    public record RechargeRequest(@NotNull @DecimalMin(value = "0", inclusive = false) @DecimalMax("1000000")
            @Digits(integer = 7, fraction = 6) BigDecimal amount_yuan,
            @Size(max = 120) String note, @Size(min = 16, max = 128) String idempotency_key) { }
    public record InternalRechargeRequest(@NotBlank @Size(max = 128) String session_token, @NotNull @Valid RechargeRequest payload) {
        @Override public String toString() { return "InternalRechargeRequest[redacted]"; }
    }
}
