package com.jobhunting.platform.auth;

import java.security.SecureRandom;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Email;
import jakarta.validation.constraints.Size;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseCookie;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import com.jobhunting.platform.security.WebSecurityFilter;

/** Browser account lifecycle contract backed by Java-owned account and email transactions. */
@RestController
@RequestMapping("/api/auth")
@ConditionalOnProperty(prefix = "platform.auth", name = "enabled", havingValue = "true")
public class PublicAuthLifecycleController {
    private static final SecureRandom RANDOM = new SecureRandom();
    private static final long CSRF_MAX_AGE_SECONDS = 30L * 24 * 60 * 60;

    private final RegistrationService registrations;
    private final AccountActionEmailService emails;
    private final AccountAdministrationService accounts;
    private final JdbcTemplate jdbc;
    private final boolean registrationEnabled, verificationRequired, consentRequired, cookieSecure;
    private final String termsVersion, privacyVersion;

    public PublicAuthLifecycleController(RegistrationService registrations, AccountActionEmailService emails,
            AccountAdministrationService accounts, JdbcTemplate jdbc,
            @Value("${JOB_AGENT_REGISTRATION_ENABLED:true}") boolean registrationEnabled,
            @Value("${JOB_AGENT_EMAIL_VERIFICATION_REQUIRED:false}") boolean verificationRequired,
            @Value("${JOB_AGENT_CONSENT_REQUIRED:false}") boolean consentRequired,
            @Value("${JOB_AGENT_TERMS_VERSION:development}") String termsVersion,
            @Value("${JOB_AGENT_PRIVACY_VERSION:development}") String privacyVersion,
            @Value("${JOB_AGENT_COOKIE_SECURE:false}") boolean cookieSecure) {
        this.registrations = registrations;
        this.emails = emails;
        this.accounts = accounts;
        this.jdbc = jdbc;
        this.registrationEnabled = registrationEnabled;
        this.verificationRequired = verificationRequired;
        this.consentRequired = consentRequired;
        this.termsVersion = termsVersion;
        this.privacyVersion = privacyVersion;
        this.cookieSecure = cookieSecure;
    }

    @GetMapping("/config")
    public Map<String, Object> config() {
        return Map.of("registration_enabled", registrationEnabled,
                "email_verification_required", verificationRequired, "consent_required", consentRequired,
                "terms_version", termsVersion, "privacy_version", privacyVersion);
    }

    @PostMapping("/register")
    public Map<String, Object> register(@Valid @RequestBody RegisterRequest body, HttpServletRequest request) {
        if (!registrationEnabled) throw invalid("当前未开放新账号注册。", HttpStatus.FORBIDDEN);
        List<RegistrationController.Consent> consents = List.of();
        if (consentRequired) {
            if (!termsVersion.equals(body.accepted_terms_version())) {
                throw invalid("请阅读并同意当前版本的服务条款。", HttpStatus.BAD_REQUEST);
            }
            if (!privacyVersion.equals(body.accepted_privacy_version())) {
                throw invalid("请阅读并同意当前版本的隐私政策。", HttpStatus.BAD_REQUEST);
            }
            String source = request.getRemoteAddr();
            String agent = header(request, "User-Agent", 512);
            consents = List.of(new RegistrationController.Consent("terms", termsVersion, source, agent),
                    new RegistrationController.Consent("privacy", privacyVersion, source, agent));
        }
        long id = registrations.register(body.email(), body.password(), body.display_name(),
                !verificationRequired, consents);
        return Map.of("account", PublicAuthController.publicAccount(accounts.accountById(id)),
                "verification_required", verificationRequired);
    }

    @GetMapping("/me")
    public Map<String, Object> me(HttpServletRequest request, HttpServletResponse response) {
        var account = (AccountAdministrationService.AccountView) request.getAttribute(WebSecurityFilter.ACCOUNT_ATTRIBUTE);
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("authenticated", account != null);
        result.put("account", account == null ? null : PublicAuthController.publicAccount(account));
        result.put("csrf_token", null);
        result.put("billing", null);
        if (account != null) {
            String csrf = PublicAuthController.cookie(request, PublicAuthController.CSRF_COOKIE);
            if (csrf == null || csrf.isBlank() || csrf.length() > 128) {
                byte[] bytes = new byte[32];
                RANDOM.nextBytes(bytes);
                csrf = Base64.getUrlEncoder().withoutPadding().encodeToString(bytes);
            }
            response.addHeader(HttpHeaders.SET_COOKIE, ResponseCookie.from(PublicAuthController.CSRF_COOKIE, csrf)
                    .httpOnly(false).secure(cookieSecure).sameSite("Lax").path("/")
                    .maxAge(CSRF_MAX_AGE_SECONDS).build().toString());
            result.put("csrf_token", csrf);
            result.put("billing", balance(account));
        }
        return result;
    }

    @PostMapping("/verify-email")
    public Map<String, Object> verify(@Valid @RequestBody TokenRequest body) {
        return Map.of("account", PublicAuthController.publicAccount(accounts.accountById(emails.verify(body.token()))));
    }

    @PostMapping("/change-email/confirm")
    public Map<String, Object> changeEmail(@Valid @RequestBody TokenRequest body) {
        return Map.of("account", PublicAuthController.publicAccount(accounts.accountById(emails.changeEmail(body.token()))));
    }

    @PostMapping("/verification/request")
    public Map<String, String> requestVerification(@Valid @RequestBody EmailRequest body, HttpServletRequest request) {
        emails.request(AccountActionEmailService.Purpose.VERIFY_EMAIL, body.email(), request.getRemoteAddr());
        return Map.of("message", "如果账号需要验证，验证邮件已发送。");
    }

    @PostMapping("/password-reset/request")
    public Map<String, String> requestReset(@Valid @RequestBody EmailRequest body, HttpServletRequest request) {
        emails.request(AccountActionEmailService.Purpose.RESET_PASSWORD, body.email(), request.getRemoteAddr());
        return Map.of("message", "如果该邮箱已注册，密码重置邮件已发送。");
    }

    @PostMapping("/password-reset/confirm")
    public Map<String, Boolean> confirmReset(@Valid @RequestBody ResetRequest body) {
        emails.resetPassword(body.token(), body.new_password());
        return Map.of("ok", true);
    }

    private Map<String, Object> balance(AccountAdministrationService.AccountView account) {
        return jdbc.queryForObject("""
                SELECT b.account_id, b.balance_micro_yuan, b.total_recharge_micro_yuan,
                       b.total_consumed_micro_yuan, b.low_balance_threshold_micro_yuan,
                       (SELECT COUNT(*) FROM account_balance_ledger WHERE account_id = b.account_id) AS ledger_entry_count
                FROM account_balances b WHERE b.account_id = ?
                """, (row, index) -> {
                    long amount = row.getLong("balance_micro_yuan");
                    long threshold = row.getLong("low_balance_threshold_micro_yuan");
                    String state = !"active".equals(account.status()) || amount <= 0 ? "suspended"
                            : amount <= threshold ? "low_balance" : "balance";
                    return Map.of("account_id", row.getLong("account_id"), "balance_micro_yuan", amount,
                            "total_recharge_micro_yuan", row.getLong("total_recharge_micro_yuan"),
                            "total_consumed_micro_yuan", row.getLong("total_consumed_micro_yuan"),
                            "ledger_entry_count", row.getLong("ledger_entry_count"),
                            "low_balance_threshold_micro_yuan", threshold, "state", state,
                            "state_label", switch (state) {
                                case "balance" -> "余额";
                                case "low_balance" -> "低余额";
                                default -> "停用";
                            });
                }, account.account_id());
    }

    private static String header(HttpServletRequest request, String name, int limit) {
        String value = request.getHeader(name);
        return value == null ? null : value.substring(0, Math.min(limit, value.length()));
    }

    private static AuthException invalid(String detail, HttpStatus status) {
        return new AuthException("INVALID_REQUEST", detail, status);
    }

    public record RegisterRequest(@NotBlank @Email @Size(max = 254) String email,
            @NotBlank @Size(min = 8, max = 1024) String password,
            @Size(max = 128) String display_name,
            @Size(max = 64) String accepted_terms_version,
            @Size(max = 64) String accepted_privacy_version) {
        @Override public String toString() { return "RegisterRequest[redacted]"; }
    }

    public record TokenRequest(@NotBlank @Size(min = 32, max = 256) String token) {
        @Override public String toString() { return "TokenRequest[redacted]"; }
    }

    public record EmailRequest(@NotBlank @Email @Size(min = 3, max = 254) String email) { }

    public record ResetRequest(@NotBlank @Size(min = 32, max = 256) String token,
            @NotBlank @Size(min = 8, max = 1024) String new_password) {
        @Override public String toString() { return "ResetRequest[redacted]"; }
    }
}
