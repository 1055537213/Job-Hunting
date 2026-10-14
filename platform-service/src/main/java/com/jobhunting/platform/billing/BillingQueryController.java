package com.jobhunting.platform.billing;

import com.jobhunting.platform.auth.AccountAdministrationService;
import com.jobhunting.platform.auth.AuthException;
import com.jobhunting.platform.security.WebSecurityFilter;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.validation.Valid;
import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Size;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.*;

@RestController
@ConditionalOnProperty(prefix = "platform.auth", name = "enabled", havingValue = "true")
public class BillingQueryController {
    private final BillingQueryService queries;
    private final AccountAdministrationService accounts;
    private final InternalTokenVerifier tokens;

    public BillingQueryController(BillingQueryService queries, AccountAdministrationService accounts, InternalTokenVerifier tokens) {
        this.queries = queries;
        this.accounts = accounts;
        this.tokens = tokens;
    }

    @GetMapping("/api/me/balance")
    public BillingQueryService.BalancePage balance(HttpServletRequest request,
            @RequestParam(defaultValue = "100") @Min(1) @Max(100) int limit,
            @RequestParam(defaultValue = "0") @Min(0) long offset) {
        return queries.balance(current(request), limit, offset);
    }

    @GetMapping("/api/me/recharge/orders")
    public BillingQueryService.OrdersPage orders(HttpServletRequest request,
            @RequestParam(defaultValue = "100") @Min(1) @Max(100) int limit,
            @RequestParam(defaultValue = "0") @Min(0) long offset) {
        return queries.orders(current(request), limit, offset);
    }

    @PostMapping("/internal/v1/billing/me/balance")
    public BillingQueryService.BalancePage internalBalance(@Valid @RequestBody PageRequest body,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String token) {
        tokens.verify(token);
        return queries.balance(accounts.current(body.session_token()).account(), body.limit(), body.offset());
    }

    @PostMapping("/internal/v1/billing/me/recharge/orders")
    public BillingQueryService.OrdersPage internalOrders(@Valid @RequestBody PageRequest body,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String token) {
        tokens.verify(token);
        return queries.orders(accounts.current(body.session_token()).account(), body.limit(), body.offset());
    }

    private static AccountAdministrationService.AccountView current(HttpServletRequest request) {
        var account = (AccountAdministrationService.AccountView) request.getAttribute(WebSecurityFilter.ACCOUNT_ATTRIBUTE);
        if (account == null) throw new AuthException("SESSION_EXPIRED", "登录状态已过期，请重新登录。", HttpStatus.UNAUTHORIZED);
        return account;
    }

    public record PageRequest(@NotBlank @Size(max = 128) String session_token,
            @NotNull @Min(1) @Max(100) Integer limit, @NotNull @Min(0) Long offset) {
        @Override public String toString() { return "PageRequest[redacted]"; }
    }
}
