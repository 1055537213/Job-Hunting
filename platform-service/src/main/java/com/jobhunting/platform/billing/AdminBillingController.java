package com.jobhunting.platform.billing;

import java.math.BigDecimal;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.validation.Valid;
import jakarta.validation.constraints.*;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.web.bind.annotation.*;
import com.jobhunting.platform.auth.AuthException;
import org.springframework.http.HttpStatus;

@RestController
@ConditionalOnProperty(prefix = "platform.auth", name = "enabled", havingValue = "true")
public class AdminBillingController {
    private final AdminBillingService service;
    private final InternalTokenVerifier tokens;

    public AdminBillingController(AdminBillingService service, InternalTokenVerifier tokens) {
        this.service = service;
        this.tokens = tokens;
    }

    @GetMapping("/api/admin/balance/events")
    public BillingQueryService.LedgerPage ledger(HttpServletRequest request,
            @RequestParam(required = false) Long account_id,
            @RequestParam(defaultValue = "100") @Min(1) @Max(100) int limit,
            @RequestParam(defaultValue = "0") @Min(0) long offset) {
        return service.ledger(session(request), account_id, limit, offset);
    }

    @GetMapping("/api/admin/recharge/orders")
    public BillingQueryService.AdminOrdersPage orders(HttpServletRequest request,
            @RequestParam(required = false) Long account_id,
            @RequestParam(defaultValue = "100") @Min(1) @Max(100) int limit,
            @RequestParam(defaultValue = "0") @Min(0) long offset) {
        return service.orders(session(request), account_id, limit, offset);
    }

    @GetMapping("/api/admin/recharge/orders/{orderId}/events")
    public BillingQueryService.OrderEvents events(HttpServletRequest request, @PathVariable long orderId) {
        return service.events(session(request), orderId);
    }

    @GetMapping("/api/admin/recharge/reconciliation")
    public BillingQueryService.ReconciliationReport reconciliation(HttpServletRequest request,
            @RequestParam(required = false) Long account_id) {
        return service.reconciliation(session(request), account_id);
    }

    @PostMapping("/api/admin/recharge/orders/{orderId}/refund")
    public BillingDtos.RefundResponse refund(HttpServletRequest request, @PathVariable long orderId,
            @Valid @RequestBody RefundRequest body) {
        return service.refund(session(request), orderId, body.reason(), body.idempotency_key(),
                request.getHeader("X-Request-ID"));
    }

    @PostMapping("/api/admin/accounts/{accountId}/balance/credit")
    public BillingQueryService.CreditResult credit(HttpServletRequest request, @PathVariable long accountId,
            @Valid @RequestBody CreditRequest body) {
        return service.credit(session(request), accountId, body.amount_yuan(), body.reason(), body.idempotency_key(), request.getHeader("X-Request-ID"));
    }

    @PostMapping("/internal/v1/billing/admin/ledger")
    public BillingQueryService.LedgerPage internalLedger(@Valid @RequestBody PageRequest body,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String token) {
        tokens.verify(token);
        return service.ledger(body.session_token(), body.account_id(), body.limit(), body.offset());
    }

    @PostMapping("/internal/v1/billing/admin/orders")
    public BillingQueryService.AdminOrdersPage internalOrders(@Valid @RequestBody PageRequest body,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String token) {
        tokens.verify(token);
        return service.orders(body.session_token(), body.account_id(), body.limit(), body.offset());
    }

    @PostMapping("/internal/v1/billing/admin/orders/{orderId}/events")
    public BillingQueryService.OrderEvents internalEvents(@PathVariable long orderId, @Valid @RequestBody SessionRequest body,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String token) {
        tokens.verify(token);
        return service.events(body.session_token(), orderId);
    }

    @PostMapping("/internal/v1/billing/admin/reconciliation")
    public BillingQueryService.ReconciliationReport internalReconciliation(@Valid @RequestBody ReconciliationRequest body,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String token) {
        tokens.verify(token);
        return service.reconciliation(body.session_token(), body.account_id());
    }

    @PostMapping("/internal/v1/billing/admin/orders/{orderId}/refund")
    public BillingDtos.RefundResponse internalRefund(@PathVariable long orderId, @Valid @RequestBody InternalRefund body,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String token,
            @RequestHeader(name = "X-Trace-Id", required = false) String traceId) {
        tokens.verify(token);
        return service.refund(body.session_token(), orderId, body.payload().reason(),
                body.payload().idempotency_key(), traceId);
    }

    @PostMapping("/internal/v1/billing/admin/accounts/{accountId}/credit")
    public BillingQueryService.CreditResult internalCredit(@PathVariable long accountId, @Valid @RequestBody InternalCredit body,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String token,
            @RequestHeader(name = "X-Trace-Id", required = false) String traceId) {
        tokens.verify(token);
        return service.credit(body.session_token(), accountId, body.payload().amount_yuan(), body.payload().reason(), body.payload().idempotency_key(), traceId);
    }

    private static String session(HttpServletRequest request) {
        if (request.getCookies() != null) for (var cookie : request.getCookies()) {
            if ("job_agent_session".equals(cookie.getName()) && !cookie.getValue().isBlank()) return cookie.getValue();
        }
        throw new AuthException("SESSION_EXPIRED", "登录状态已过期，请重新登录。", HttpStatus.UNAUTHORIZED);
    }

    public record CreditRequest(@NotNull @DecimalMin(value = "0", inclusive = false) @DecimalMax("1000000")
            @Digits(integer = 7, fraction = 6) BigDecimal amount_yuan,
            @NotNull @Size(min = 2, max = 500) String reason, @NotNull @Size(min = 16, max = 128) String idempotency_key) { }
    public record RefundRequest(@NotBlank @Size(min = 2, max = 500) String reason,
            @NotBlank @Size(min = 16, max = 128) String idempotency_key) { }
    public record PageRequest(@NotBlank @Size(max = 128) String session_token, Long account_id,
            @NotNull @Min(1) @Max(100) Integer limit, @NotNull @Min(0) Long offset) {
        @Override public String toString() { return "AdminPageRequest[redacted]"; }
    }
    public record SessionRequest(@NotBlank @Size(max = 128) String session_token) {
        @Override public String toString() { return "AdminSessionRequest[redacted]"; }
    }
    public record ReconciliationRequest(@NotBlank @Size(max = 128) String session_token, Long account_id) {
        @Override public String toString() { return "AdminReconciliationRequest[redacted]"; }
    }
    public record InternalRefund(@NotBlank @Size(max = 128) String session_token,
            @NotNull @Valid RefundRequest payload) {
        @Override public String toString() { return "AdminRefundRequest[redacted]"; }
    }
    public record InternalCredit(@NotBlank @Size(max = 128) String session_token, @NotNull @Valid CreditRequest payload) {
        @Override public String toString() { return "AdminCreditRequest[redacted]"; }
    }
}
