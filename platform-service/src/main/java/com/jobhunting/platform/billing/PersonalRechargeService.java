package com.jobhunting.platform.billing;

import java.math.BigDecimal;
import java.util.UUID;
import com.jobhunting.platform.auth.AccountAdministrationService;
import com.jobhunting.platform.auth.AuthException;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
@ConditionalOnProperty(prefix = "platform.auth", name = "enabled", havingValue = "true")
public class PersonalRechargeService {
    private final AccountAdministrationService accounts;
    private final BillingQueryService queries;
    private final BillingReadSettings settings;
    private final ObjectProvider<BillingService> billing;

    public PersonalRechargeService(AccountAdministrationService accounts, BillingQueryService queries,
            BillingReadSettings settings, ObjectProvider<BillingService> billing) {
        this.accounts = accounts;
        this.queries = queries;
        this.settings = settings;
        this.billing = billing;
    }

    @Transactional
    public BillingQueryService.RechargeResult recharge(String sessionToken, BigDecimal amountYuan, String note, String key) {
        if (sessionToken == null || sessionToken.isBlank() || sessionToken.length() > 128) {
            throw new AuthException("SESSION_EXPIRED", "登录状态已过期，请重新登录。", HttpStatus.UNAUTHORIZED);
        }
        // Revalidate inside the money transaction so disable/password revocation cannot race settlement.
        var account = accounts.current(sessionToken).account();
        if (!settings.demoEnabled()) {
            throw new BillingException("DEMO_RECHARGE_DISABLED", "演示充值当前未开放，请联系管理员。", HttpStatus.SERVICE_UNAVAILABLE);
        }
        var writer = billing.getIfAvailable();
        if (writer == null) throw new BillingException("BILLING_UNAVAILABLE", "平台账务服务暂时不可用，请稍后重试。", HttpStatus.SERVICE_UNAVAILABLE);
        long amount;
        try {
            amount = amountYuan.movePointRight(6).longValueExact();
            if (amount <= 0 || amount > 1_000_000_000_000L || note != null && note.length() > 120) throw new ArithmeticException();
        } catch (ArithmeticException | NullPointerException error) {
            throw new BillingException("INVALID_REQUEST", "充值金额或备注无效。", HttpStatus.UNPROCESSABLE_ENTITY);
        }
        String normalizedKey = key == null || key.isEmpty() ? UUID.randomUUID().toString().replace("-", "") : key.strip();
        if (normalizedKey.length() < 16 || normalizedKey.length() > 128
                || !normalizedKey.codePoints().allMatch(c -> Character.isLetterOrDigit(c) || c == '-' || c == '_' || c == ':')) {
            throw new BillingException("INVALID_REQUEST", "充值幂等键无效。", HttpStatus.BAD_REQUEST);
        }
        String description = note == null || note.isBlank() ? "个人中心模拟充值" : note.strip();
        try {
            writer.recharge(new BillingDtos.RechargeRequest(account.account_id(), amount, normalizedKey, account.account_id(),
                    microLimit(settings.demoRechargeMaxAmountYuan()), microLimit(settings.demoRechargeMaxTotalYuan()), description), normalizedKey);
        } catch (BillingException error) {
            if ("INVALID_REQUEST".equals(error.code())) throw new BillingException(error.code(), error.getMessage(), HttpStatus.BAD_REQUEST);
            throw error;
        }
        return queries.rechargeResult(account, normalizedKey);
    }

    private static long microLimit(BigDecimal value) {
        // Limit rounding must never allow more money than configured.
        return value.movePointRight(6).setScale(0, java.math.RoundingMode.DOWN).longValueExact();
    }
}
