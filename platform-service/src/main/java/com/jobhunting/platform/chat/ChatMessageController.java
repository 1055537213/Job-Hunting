package com.jobhunting.platform.chat;

import java.util.Set;
import com.jobhunting.platform.auth.AccountAdministrationService;
import com.jobhunting.platform.auth.AuthException;
import com.jobhunting.platform.billing.InternalTokenVerifier;
import com.jobhunting.platform.security.WebSecurityFilter;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.*;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.node.ObjectNode;

@RestController
@ConditionalOnProperty(name="platform.auth.enabled", havingValue="true")
public class ChatMessageController {
    private final AccountAdministrationService accounts;
    private final ChatSessionClient python;
    private final InternalTokenVerifier tokens;
    public ChatMessageController(AccountAdministrationService accounts, ChatSessionClient python, InternalTokenVerifier tokens) {
        this.accounts = accounts; this.python = python; this.tokens = tokens;
    }
    @PostMapping("/api/chat")
    public JsonNode chat(@CookieValue(name="job_agent_session", required=false) String session, @RequestBody JsonNode body,
            HttpServletRequest request, HttpServletResponse response) {
        return submit(session, body, request, response);
    }
    @PostMapping("/internal/v1/chat/invoke")
    public JsonNode internal(@RequestHeader(name="X-Internal-Service-Token", required=false) String token,
            @RequestBody JsonNode body, HttpServletRequest request, HttpServletResponse response) {
        tokens.verify(token);
        if (!body.isObject() || !body.path("session_token").isTextual()) throw ChatSessionClient.invalid();
        var data = (ObjectNode) body.deepCopy();
        String session = data.remove("session_token").asText();
        return submit(session, data, request, response);
    }
    private JsonNode submit(String session, JsonNode input, HttpServletRequest request, HttpServletResponse response) {
        response.setHeader("Cache-Control", "no-store");
        if (session == null || session.isBlank() || session.length() > 128)
            throw new AuthException("SESSION_EXPIRED", "登录状态已过期，请重新登录。", HttpStatus.UNAUTHORIZED);
        if (!input.isObject() || !Set.of("candidate_id", "message", "session_id", "use_env_llm", "auto_rag").containsAll(input.propertyNames())
                || !ChatSessionClient.id(input.get("candidate_id")) || !input.path("message").isTextual()) throw ChatSessionClient.invalid();
        String message = input.get("message").asText();
        if (message.isBlank() || message.codePointCount(0, message.length()) > 16000) throw ChatSessionClient.invalid();
        if (input.hasNonNull("session_id") && !ChatSessionClient.sessionKey(input.get("session_id"))) throw ChatSessionClient.invalid();
        var data = (ObjectNode) input.deepCopy();
        for (String flag : Set.of("use_env_llm", "auto_rag")) {
            if (!data.has(flag)) data.put(flag, true);
            if (!data.get(flag).isBoolean()) throw ChatSessionClient.invalid();
        }
        long owner = accounts.current(session).account().account_id();
        return python.invoke(session, owner, data,
            WebSecurityFilter.requestId(request.getHeader("X-Request-ID"), request.getHeader("X-Trace-Id")));
    }
}
