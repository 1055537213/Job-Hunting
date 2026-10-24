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
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

@RestController
@ConditionalOnProperty(name = "platform.auth.enabled", havingValue = "true")
public class ChatSessionController {
    private final AccountAdministrationService accounts;
    private final ChatSessionClient python;
    private final InternalTokenVerifier tokens;
    private final ObjectMapper mapper;
    public ChatSessionController(AccountAdministrationService accounts, ChatSessionClient python, InternalTokenVerifier tokens, ObjectMapper mapper) {
        this.accounts = accounts; this.python = python; this.tokens = tokens; this.mapper = mapper;
    }
    @GetMapping("/api/chat/sessions")
    public JsonNode list(@CookieValue(name="job_agent_session", required=false) String session,
            @RequestParam(name="candidate_id", required=false) Long candidate,
            @RequestParam(name="include_archived", defaultValue="false") boolean archived,
            HttpServletRequest request, HttpServletResponse response) {
        var data = mapper.createObjectNode().put("include_archived", archived);
        if (candidate != null) data.put("candidate_id", candidate);
        return submit("list", session, data, request, response);
    }
    @GetMapping("/api/chat/history")
    public JsonNode history(@CookieValue(name="job_agent_session", required=false) String session,
            @RequestParam(name="candidate_id") Long candidate, @RequestParam(name="session_id", required=false) String key,
            @RequestParam(name="limit", defaultValue="100") int limit, HttpServletRequest request, HttpServletResponse response) {
        var data = mapper.createObjectNode().put("candidate_id", candidate).put("limit", limit);
        if (key != null) data.put("session_id", key);
        return submit("history", session, data, request, response);
    }
    @PostMapping("/api/chat/sessions")
    public JsonNode create(@CookieValue(name="job_agent_session", required=false) String session,
            @RequestBody JsonNode body, HttpServletRequest request, HttpServletResponse response) {
        return submit("create", session, body, request, response);
    }
    @PostMapping("/api/chat/sessions/{sessionId}/archive")
    public JsonNode archive(@CookieValue(name="job_agent_session", required=false) String session,
            @PathVariable("sessionId") String key, HttpServletRequest request, HttpServletResponse response) {
        return submit("archive", session, mapper.createObjectNode().put("session_id", key), request, response);
    }
    @DeleteMapping("/api/chat/sessions/{sessionId}")
    public JsonNode delete(@CookieValue(name="job_agent_session", required=false) String session,
            @PathVariable("sessionId") String key, HttpServletRequest request, HttpServletResponse response) {
        return submit("delete", session, mapper.createObjectNode().put("session_id", key), request, response);
    }
    @PostMapping("/internal/v1/chat/sessions/{operation}")
    public JsonNode internal(@PathVariable("operation") String operation, @RequestBody JsonNode body,
            @RequestHeader(name="X-Internal-Service-Token", required=false) String token,
            HttpServletRequest request, HttpServletResponse response) {
        tokens.verify(token);
        if (!body.isObject() || !body.path("session_token").isTextual()) throw ChatSessionClient.invalid();
        var data = (ObjectNode) body.deepCopy(); String session = data.remove("session_token").asText();
        return submit(operation, session, data, request, response);
    }
    private JsonNode submit(String operation, String session, JsonNode input, HttpServletRequest request, HttpServletResponse response) {
        response.setHeader("Cache-Control", "no-store");
        if (session == null || session.isBlank() || session.length() > 128)
            throw new AuthException("SESSION_EXPIRED", "登录状态已过期，请重新登录。", HttpStatus.UNAUTHORIZED);
        Set<String> allowed = switch (operation) {
            case "list" -> Set.of("candidate_id", "include_archived");
            case "create" -> Set.of("candidate_id", "title", "job_id");
            case "archive", "delete" -> Set.of("session_id");
            case "history" -> Set.of("candidate_id", "session_id", "limit");
            default -> throw ChatSessionClient.invalid();
        };
        if (!input.isObject() || !allowed.containsAll(input.propertyNames())) throw ChatSessionClient.invalid();
        var data = (ObjectNode) input.deepCopy();
        for (String id : Set.of("candidate_id", "job_id"))
            if (data.hasNonNull(id) && !ChatSessionClient.id(data.get(id))) throw ChatSessionClient.invalid();
        if (Set.of("create", "history").contains(operation) && !ChatSessionClient.id(data.get("candidate_id"))) throw ChatSessionClient.invalid();
        if (data.hasNonNull("session_id") && !ChatSessionClient.sessionKey(data.get("session_id"))) throw ChatSessionClient.invalid();
        if (Set.of("archive", "delete").contains(operation) && !ChatSessionClient.sessionKey(data.get("session_id"))) throw ChatSessionClient.invalid();
        if (data.has("include_archived") && !data.get("include_archived").isBoolean()) throw ChatSessionClient.invalid();
        if (data.has("limit") && (!ChatSessionClient.id(data.get("limit")) || data.get("limit").asLong() > 500)) throw ChatSessionClient.invalid();
        if ("create".equals(operation)) {
            if (!data.has("title")) data.put("title", "新对话");
            if (!data.get("title").isTextual() || data.get("title").asText().length() > 200) throw ChatSessionClient.invalid();
            if (!data.has("job_id")) data.putNull("job_id");
        }
        // Authentication commits before Python revalidates this same opaque session.
        long owner = accounts.current(session).account().account_id();
        return python.execute(operation, session, owner, data,
                WebSecurityFilter.requestId(request.getHeader("X-Request-ID"), request.getHeader("X-Trace-Id")));
    }
}
