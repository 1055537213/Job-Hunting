package com.jobhunting.platform.auth;

import java.util.List;
import java.util.Locale;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
@ConditionalOnProperty(prefix = "platform.auth", name = "enabled", havingValue = "true")
public class RegistrationService {
    private final JdbcTemplate jdbc;
    private final PasswordHasher passwords;
    private final EmailVerificationService verification;
    private final long lowBalanceThresholdMicroYuan;

    public RegistrationService(
            JdbcTemplate jdbc,
            PasswordHasher passwords,
            EmailVerificationService verification,
            @Value("${platform.billing.low-balance-threshold-micro-yuan:10000000}")
            long lowBalanceThresholdMicroYuan) {
        this.jdbc = jdbc;
        this.passwords = passwords;
        this.verification = verification;
        this.lowBalanceThresholdMicroYuan = lowBalanceThresholdMicroYuan;
    }

    @Transactional
    public long register(
            String email,
            String password,
            String displayName,
            boolean emailVerified,
            List<RegistrationController.Consent> consents) {
        String normalizedEmail = normalizeEmail(email);
        String normalizedDisplayName = normalizeDisplayName(displayName, password);
        validateConsents(consents);
        try {
            long accountId = jdbc.queryForObject(
                    """
                    INSERT INTO accounts (
                        email, password_hash, display_name, role, status,
                        must_change_password, email_verified_at, deleted_at,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, 'user', 'active', FALSE,
                              CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END,
                              NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    RETURNING id
                    """,
                    Long.class,
                    normalizedEmail,
                    passwords.encode(password),
                    normalizedDisplayName,
                    emailVerified);
            jdbc.update(
                    """
                    INSERT INTO account_balances (
                        account_id, balance_micro_yuan, total_recharge_micro_yuan,
                        total_consumed_micro_yuan, low_balance_threshold_micro_yuan,
                        created_at, updated_at
                    ) VALUES (?, 0, 0, 0, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """,
                    accountId,
                    lowBalanceThresholdMicroYuan);
            for (RegistrationController.Consent consent : consents) {
                jdbc.update(
                        """
                        INSERT INTO account_consents (
                            account_id, document_type, version, accepted_at,
                            ip_address, user_agent
                        ) VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?, ?)
                        """,
                        accountId,
                        consent.document_type(),
                        consent.version().strip(),
                        consent.ip_address(),
                        consent.user_agent());
            }
            if (!emailVerified) verification.enqueue(accountId, null);
            return accountId;
        } catch (DuplicateKeyException exception) {
            throw new AuthException(
                    "ACCOUNT_ALREADY_EXISTS", "该邮箱已经注册。",
                    org.springframework.http.HttpStatus.CONFLICT);
        }
    }

    private static String normalizeEmail(String email) {
        String normalized = email == null ? "" : email.strip().toLowerCase(Locale.ROOT);
        if (normalized.isBlank() || !normalized.contains("@") || normalized.length() > 254) {
            throw new AuthException(
                    "INVALID_REQUEST", "请输入有效的邮箱地址。",
                    org.springframework.http.HttpStatus.BAD_REQUEST);
        }
        return normalized;
    }

    private static String normalizeDisplayName(String displayName, String password) {
        if (displayName == null || displayName.isBlank() || displayName.strip().equals(password)) {
            return null;
        }
        String normalized = displayName.strip();
        if (normalized.length() > 128) {
            throw new AuthException(
                    "INVALID_REQUEST", "显示名不能超过 128 个字符。",
                    org.springframework.http.HttpStatus.BAD_REQUEST);
        }
        return normalized;
    }

    private static void validateConsents(List<RegistrationController.Consent> consents) {
        for (RegistrationController.Consent consent : consents) {
            if (consent == null || consent.document_type() == null
                    || (!"terms".equals(consent.document_type())
                        && !"privacy".equals(consent.document_type()))
                    || consent.version() == null || consent.version().isBlank()) {
                throw new AuthException(
                        "INVALID_REQUEST", "协议同意记录无效。",
                        org.springframework.http.HttpStatus.BAD_REQUEST);
            }
        }
    }
}
