package com.jobhunting.platform.profiles;

import com.jobhunting.platform.auth.AccountAdministrationService;
import com.jobhunting.platform.auth.AuthException;
import com.jobhunting.platform.security.WebSecurityFilter;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import tools.jackson.databind.JsonNode;

@Service
@ConditionalOnProperty(name = "platform.auth.enabled", havingValue = "true")
public class ProfileReadService {
    private final AccountAdministrationService accounts;
    private final ProfileQueryClient python;
    public ProfileReadService(AccountAdministrationService accounts, ProfileQueryClient python) { this.accounts = accounts; this.python = python; }

    public JsonNode query(String session, Long candidateId, String requestId) {
        long owner = owner(session, candidateId);
        return python.query(session, owner, candidateId, WebSecurityFilter.requestId(requestId, null));
    }

    public JsonNode delete(String session, Long candidateId, String requestId) {
        if (candidateId == null) throw new AuthException("INVALID_REQUEST", "请求参数无效。", HttpStatus.UNPROCESSABLE_CONTENT);
        long owner = owner(session, candidateId);
        return python.delete(session, owner, candidateId, WebSecurityFilter.requestId(requestId, null));
    }

    private long owner(String session, Long candidateId) {
        if (session == null || session.isBlank() || session.length() > 128) {
            throw new AuthException("SESSION_EXPIRED", "登录状态已过期，请重新登录。", HttpStatus.UNAUTHORIZED);
        }
        if (candidateId != null && candidateId <= 0) throw new AuthException("INVALID_REQUEST", "请求参数无效。", HttpStatus.UNPROCESSABLE_CONTENT);
        // Finish the authentication transaction before the callback authenticates again.
        return accounts.current(session).account().account_id();
    }
}
