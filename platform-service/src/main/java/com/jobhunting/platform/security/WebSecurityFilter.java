package com.jobhunting.platform.security;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Map;
import java.util.Set;
import java.util.UUID;

import com.jobhunting.platform.auth.AccountAdministrationService;
import com.jobhunting.platform.auth.AuthException;
import io.micrometer.core.instrument.MeterRegistry;
import io.micrometer.core.instrument.Timer;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.Cookie;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletRequestWrapper;
import jakarta.servlet.http.HttpServletResponse;
import jakarta.servlet.http.HttpServletResponseWrapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.slf4j.MDC;
import org.springframework.web.filter.OncePerRequestFilter;
import tools.jackson.databind.ObjectMapper;

public class WebSecurityFilter extends OncePerRequestFilter {
    public static final String ACCOUNT_ATTRIBUTE = WebSecurityFilter.class.getName() + ".account";
    private static final Logger LOG = LoggerFactory.getLogger(WebSecurityFilter.class);
    private static final Set<String> SAFE = Set.of("GET", "HEAD", "OPTIONS");
    private static final Set<String> ANONYMOUS = Set.of(
            "/api/auth/login", "/api/auth/register", "/api/auth/config", "/api/auth/me", "/api/auth/logout",
            "/api/auth/verify-email", "/api/auth/verification/request", "/api/auth/password-reset/request",
            "/api/auth/password-reset/confirm", "/api/auth/change-email/confirm");
    private final AccountAdministrationService accounts;
    private final RedisRequestLimiter limiter;
    private final WebSecuritySettings settings;
    private final ObjectMapper mapper;
    private final MeterRegistry metrics;
    private final ClientAddressResolver addresses;

    public WebSecurityFilter(AccountAdministrationService accounts, RedisRequestLimiter limiter,
            WebSecuritySettings settings, ObjectMapper mapper, MeterRegistry metrics, ClientAddressResolver addresses) {
        this.accounts = accounts;
        this.limiter = limiter;
        this.settings = settings;
        this.mapper = mapper;
        this.metrics = metrics;
        this.addresses = addresses;
    }

    @Override
    protected void doFilterInternal(HttpServletRequest raw, HttpServletResponse response, FilterChain chain)
            throws IOException, ServletException {
        String id = requestId(raw.getHeader("X-Request-ID"), raw.getHeader("X-Trace-Id"));
        String previous = MDC.get("request_id");
        MDC.put("request_id", id);
        headers(response, id);
        var guardedResponse = new HttpServletResponseWrapper(response) {
            @Override public void sendError(int status) throws IOException { sendError(status, null); }
            @Override public void sendError(int status, String ignored) throws IOException {
                reject(response, status, "HTTP_ERROR", "请求无法处理。", id, 0);
            }
        };
        String group = group(raw);
        String outcome = "handled";
        Timer.Sample timer = Timer.start(metrics);
        try {
            String path = raw.getRequestURI();
            if (path.contains("%") || path.contains(";") || path.contains("\\") || path.contains("//")) {
                outcome = "invalid_path";
                reject(response, 400, "INVALID_REQUEST", "请求路径无效。", id, 0);
                return;
            }
            String clientAddress = addresses.resolve(raw);
            var request = new HttpServletRequestWrapper(raw) {
                @Override public String getRemoteAddr() { return clientAddress; }
                @Override public String getHeader(String name) {
                    if ("X-Request-ID".equalsIgnoreCase(name) || "X-Trace-Id".equalsIgnoreCase(name)) return id;
                    return super.getHeader(name);
                }
            };
            long retry = limiter.check("ip:" + request.getRemoteAddr(), group);
            if (retry > 0) {
                outcome = "rate_limited";
                reject(response, 429, "RATE_LIMITED", "请求过于频繁，请稍后再试。", id, retry);
                return;
            }
            String token = cookie(request, "job_agent_session");
            boolean resolve = !ANONYMOUS.contains(path) || path.equals("/api/auth/me") || path.equals("/api/auth/logout");
            AccountAdministrationService.AccountView account = null;
            if (resolve && token != null && token.length() <= 128) {
                try {
                    account = accounts.current(token).account();
                } catch (AuthException error) {
                    if (!"SESSION_EXPIRED".equals(error.code()) || !ANONYMOUS.contains(path)) throw error;
                }
            }
            if (account == null && !ANONYMOUS.contains(path)) {
                outcome = "unauthenticated";
                reject(response, 401, "SESSION_EXPIRED", "登录状态已过期，请重新登录。", id, 0);
                return;
            }
            if (account != null) {
                request.setAttribute(ACCOUNT_ATTRIBUTE, account);
                retry = limiter.check("account:" + account.account_id(), group);
                if (retry > 0) {
                    outcome = "rate_limited";
                    reject(response, 429, "RATE_LIMITED", "请求过于频繁，请稍后再试。", id, retry);
                    return;
                }
            }
            if ((path.equals("/api/admin") || path.startsWith("/api/admin/"))
                    && (account == null || !"admin".equals(account.role()))) {
                outcome = "forbidden";
                reject(response, 403, "ADMIN_REQUIRED", "需要管理员权限。", id, 0);
                return;
            }
            if (settings.csrfEnabled() && token != null && !SAFE.contains(request.getMethod())
                    && !path.equals("/api/auth/login") && !path.equals("/api/auth/register")
                    && !validCsrf(request)) {
                outcome = "csrf_rejected";
                reject(response, 403, "CSRF_REJECTED", "CSRF 校验失败，请刷新页面后重试。", id, 0);
                return;
            }
            chain.doFilter(request, guardedResponse);
        } catch (ClientAddressResolver.InvalidAddress error) {
            outcome = "invalid_address";
            reject(response, 400, "INVALID_REQUEST", "请求来源地址无效。", id, 0);
        } catch (RedisRequestLimiter.Unavailable error) {
            outcome = "protection_unavailable";
            reject(response, 503, "PROTECTION_UNAVAILABLE", "请求保护服务暂时不可用，请稍后重试。", id, 1);
        } catch (AuthException error) {
            outcome = "auth_rejected";
            reject(response, error.status().value(), error.code(), error.getMessage(), id, 0);
        } catch (Exception error) {
            outcome = "exception";
            LOG.error("Platform request failed request_id={} error_type={}", id, error.getClass().getSimpleName());
            if (!response.isCommitted()) reject(response, 503, "PLATFORM_UNAVAILABLE", "平台服务暂时不可用，请稍后重试。", id, 0);
        } finally {
            // Only finite groups/status families are tags, never raw paths, account IDs or tokens.
            String status = (response.getStatus() / 100) + "xx";
            metrics.counter("platform.web.requests", "group", group, "status", status, "outcome", outcome).increment();
            timer.stop(metrics.timer("platform.web.duration", "group", group));
            LOG.info("Platform request request_id={} group={} status={} outcome={}", id, group, response.getStatus(), outcome);
            if (previous == null) MDC.remove("request_id"); else MDC.put("request_id", previous);
        }
    }

    private boolean validCsrf(HttpServletRequest request) {
        String cookie = cookie(request, "job_agent_csrf");
        String header = request.getHeader("X-CSRF-Token");
        return cookie != null && header != null && !cookie.isBlank() && cookie.length() <= 128 && header.length() <= 128
                && MessageDigest.isEqual(cookie.getBytes(StandardCharsets.UTF_8), header.getBytes(StandardCharsets.UTF_8));
    }

    private void reject(HttpServletResponse response, int status, String code, String detail, String id, long retry)
            throws IOException {
        response.setStatus(status);
        response.setContentType("application/json;charset=UTF-8");
        if (retry > 0) response.setHeader("Retry-After", Long.toString(retry));
        response.getWriter().write(mapper.writeValueAsString(Map.of(
                "code", code, "message", detail, "detail", detail, "request_id", id, "trace_id", id)));
    }

    private void headers(HttpServletResponse response, String id) {
        response.setHeader("X-Request-ID", id);
        response.setHeader("X-Trace-Id", id);
        response.setHeader("Cache-Control", "no-store");
        response.setHeader("X-Content-Type-Options", "nosniff");
        response.setHeader("X-Frame-Options", "DENY");
        response.setHeader("Referrer-Policy", "same-origin");
        response.setHeader("Permissions-Policy", "camera=(), microphone=(), geolocation=()");
        response.setHeader("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'");
        if (settings.cookieSecure()) response.setHeader("Strict-Transport-Security", "max-age=31536000");
    }

    public static String requestId(String request, String trace) {
        String value = request == null ? trace : request;
        return value != null && value.matches("[A-Za-z0-9._-]{8,128}") ? value : UUID.randomUUID().toString();
    }

    private static String cookie(HttpServletRequest request, String name) {
        if (request.getCookies() != null) {
            for (Cookie cookie : request.getCookies()) if (name.equals(cookie.getName())) return cookie.getValue();
        }
        return null;
    }

    private static String group(HttpServletRequest request) {
        String path = request.getRequestURI();
        if (path.startsWith("/api/auth/")) return "auth";
        if (path.startsWith("/api/admin/")) return "admin";
        return SAFE.contains(request.getMethod()) ? "default" : "write";
    }
}
