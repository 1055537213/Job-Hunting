package com.jobhunting.platform.billing;

import java.math.BigDecimal;
import com.jobhunting.platform.auth.AccountAdministrationService;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
@ConditionalOnProperty(prefix = "platform.auth", name = "enabled", havingValue = "true")
public class AdminBillingService {
    private final AccountAdministrationService accounts;
    private final BillingQueryService queries;
    private final ObjectProvider<BillingService> billing;

    public AdminBillingService(AccountAdministrationService accounts, BillingQueryService queries, ObjectProvider<BillingService> billing) {
        this.accounts = accounts;
        this.queries = queries;
        this.billing = billing;
    }

    public BillingQueryService.LedgerPage ledger(String session, Long accountId, int limit, long offset) {
        accounts.authorizeAdministrator(session);
        return queries.adminLedger(accountId, limit, offset);
    }

    public BillingQueryService.AdminOrdersPage orders(String session, Long accountId, int limit, long offset) {
        accounts.authorizeAdministrator(session);
        return queries.adminOrders(accountId, limit, offset);
    }

    public BillingQueryService.OrderEvents events(String session, long orderId) {
        accounts.authorizeAdministrator(session);
        return queries.orderEvents(orderId);
    }

    @Transactional
    public BillingQueryService.CreditResult credit(String session, long accountId, BigDecimal amountYuan,
            String reason, String key, String traceId) {
        // Policy lock must precede actor/target locks, including self-credit and cross-admin credit.
        var actor = accounts.authorizeAdministrator(session);
        var writer = billing.getIfAvailable();
        if (writer == null) throw new BillingException("BILLING_UNAVAILABLE", "平台账务服务暂时不可用，请稍后重试。", HttpStatus.SERVICE_UNAVAILABLE);
        long amount;
        try {
            amount = amountYuan.movePointRight(6).longValueExact();
            if (amount < 1 || amount > 1_000_000_000_000L) throw new ArithmeticException();
        } catch (ArithmeticException | NullPointerException error) {
            throw new BillingException("INVALID_REQUEST", "补款金额无效。", HttpStatus.UNPROCESSABLE_ENTITY);
        }
        if (reason == null || key == null) throw new BillingException("INVALID_REQUEST", "补款原因或幂等键无效。", HttpStatus.BAD_REQUEST);
        BillingDtos.CreditResponse credited;
        try {
            credited = writer.credit(new BillingDtos.CreditRequest(accountId, actor.account_id(), amount, key, reason, session), key.strip(), traceId);
        } catch (BillingException error) {
            if ("INVALID_REQUEST".equals(error.code())) throw new BillingException(error.code(), error.getMessage(), HttpStatus.BAD_REQUEST);
            throw error;
        }
        return queries.creditResult(accountId, credited.ledger_entry_id());
    }
}
