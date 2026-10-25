package com.jobhunting.platform.chat;

import com.jobhunting.platform.auth.AccountAdministrationService;
import com.jobhunting.platform.billing.InternalTokenVerifier;
import com.jobhunting.platform.security.WebSecurityFilter;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.web.bind.annotation.*;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.node.ObjectNode;

@RestController
@ConditionalOnProperty(name="platform.auth.enabled", havingValue="true")
public class ChatStreamController {
    private final AccountAdministrationService accounts;
    private final ChatStreamClient python;
    private final InternalTokenVerifier tokens;
    public ChatStreamController(AccountAdministrationService accounts, ChatStreamClient python, InternalTokenVerifier tokens) {
        this.accounts=accounts; this.python=python; this.tokens=tokens;
    }
    @PostMapping("/api/chat/stream")
    public void stream(@CookieValue(name="job_agent_session", required=false) String session, @RequestBody JsonNode body,
            HttpServletRequest request, HttpServletResponse response) {
        submit(session, body, request, response);
    }
    @PostMapping("/internal/v1/chat/stream")
    public void internal(@RequestHeader(name="X-Internal-Service-Token", required=false) String token, @RequestBody JsonNode body,
            HttpServletRequest request, HttpServletResponse response) {
        tokens.verify(token);
        if (!body.isObject() || !body.path("session_token").isTextual()) throw ChatSessionClient.invalid();
        var data=(ObjectNode)body.deepCopy(); String session=data.remove("session_token").asText();
        submit(session, data, request, response);
    }
    private void submit(String session, JsonNode body, HttpServletRequest request, HttpServletResponse response) {
        response.setHeader("Cache-Control", "no-store");
        var data=ChatMessageController.validateInput(session, body);
        long owner=accounts.current(session).account().account_id();
        python.relay(session, owner, data, WebSecurityFilter.requestId(request.getHeader("X-Request-ID"), request.getHeader("X-Trace-Id")), response);
    }
}
