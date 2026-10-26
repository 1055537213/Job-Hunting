package com.jobhunting.platform.projects;

import java.util.Set;
import com.jobhunting.platform.auth.AccountAdministrationService;
import com.jobhunting.platform.billing.InternalTokenVerifier;
import com.jobhunting.platform.security.WebSecurityFilter;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.web.bind.annotation.*;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

@RestController
@ConditionalOnProperty(name="platform.auth.enabled", havingValue="true")
public class ProjectController {
    private final AccountAdministrationService accounts;
    private final ProjectClient python;
    private final InternalTokenVerifier tokens;
    private final ObjectMapper mapper;
    public ProjectController(AccountAdministrationService accounts, ProjectClient python, InternalTokenVerifier tokens, ObjectMapper mapper) {
        this.accounts=accounts; this.python=python; this.tokens=tokens; this.mapper=mapper;
    }
    @GetMapping("/api/projects")
    public JsonNode list(@CookieValue(name="job_agent_session",required=false) String session,
            @RequestParam("candidate_id") long candidate, HttpServletRequest request, HttpServletResponse response) {
        return submit("list",session,mapper.createObjectNode().put("candidate_id",candidate),request,response);
    }
    @PostMapping("/api/projects/{recordId}/confirm")
    public JsonNode confirm(@CookieValue(name="job_agent_session",required=false) String session,
            @PathVariable("recordId") long record, @RequestBody JsonNode body, HttpServletRequest request, HttpServletResponse response) {
        if (!body.isObject() || !Set.of("confirmed_summary","root_request_id").containsAll(body.propertyNames())) throw ProjectClient.invalid();
        return submit("confirm",session,((ObjectNode)body.deepCopy()).put("record_id",record),request,response);
    }
    @DeleteMapping("/api/projects/{recordId}")
    public JsonNode delete(@CookieValue(name="job_agent_session",required=false) String session,
            @PathVariable("recordId") long record, HttpServletRequest request, HttpServletResponse response) {
        return submit("delete",session,mapper.createObjectNode().put("record_id",record),request,response);
    }
    @PostMapping("/api/projects/github")
    public JsonNode github(@CookieValue(name="job_agent_session",required=false) String session,
            @RequestBody JsonNode body, HttpServletRequest request, HttpServletResponse response) {
        return submit("github",session,body,request,response);
    }
    @PostMapping("/api/projects/local/manifest")
    public JsonNode manifest(@CookieValue(name="job_agent_session",required=false) String session,
            @RequestBody JsonNode body,HttpServletRequest request,HttpServletResponse response){
        return submit("local-manifest",session,body,request,response);
    }
    @GetMapping("/api/projects/local/{collectionId}")
    public JsonNode collection(@CookieValue(name="job_agent_session",required=false) String session,
            @PathVariable("collectionId") long id,HttpServletRequest request,HttpServletResponse response){
        return submit("local-get",session,mapper.createObjectNode().put("collection_id",id),request,response);
    }
    @DeleteMapping("/api/projects/local/{collectionId}")
    public JsonNode cancelCollection(@CookieValue(name="job_agent_session",required=false) String session,
            @PathVariable("collectionId") long id,HttpServletRequest request,HttpServletResponse response){
        return submit("local-cancel",session,mapper.createObjectNode().put("collection_id",id),request,response);
    }
    @PostMapping("/internal/v1/projects/{operation}")
    public JsonNode internal(@PathVariable("operation") String operation, @RequestBody JsonNode body,
            @RequestHeader(name="X-Internal-Service-Token",required=false) String token,
            HttpServletRequest request, HttpServletResponse response) {
        tokens.verify(token);
        if (!body.isObject() || !body.path("session_token").isTextual()) throw ProjectClient.invalid();
        var data=(ObjectNode)body.deepCopy(); String session=data.remove("session_token").asText();
        return submit(operation,session,data,request,response);
    }
    private JsonNode submit(String operation,String session,JsonNode input,HttpServletRequest request,HttpServletResponse response) {
        response.setHeader("Cache-Control","no-store");
        if (session==null || session.isBlank() || session.length()>128) throw ProjectClient.invalid();
        var fields=switch(operation) {
            case "list" -> Set.of("candidate_id");
            case "confirm" -> Set.of("record_id","confirmed_summary","root_request_id");
            case "delete" -> Set.of("record_id");
            case "github" -> Set.of("candidate_id","repository_url","idempotency_key");
            case "local-manifest" -> Set.of("candidate_id","project_name","files");
            case "local-get","local-cancel" -> Set.of("collection_id");
            default -> throw ProjectClient.invalid();
        };
        if (!input.isObject() || !fields.containsAll(input.propertyNames())
                || !ProjectClient.id(input.get(Set.of("list","github","local-manifest").contains(operation)?"candidate_id":
                    Set.of("local-get","local-cancel").contains(operation)?"collection_id":"record_id"))) throw ProjectClient.invalid();
        if("local-manifest".equals(operation))LocalCollectionProjection.manifest(input);
        if ("github".equals(operation)) {
            if (!input.path("repository_url").isTextual() || input.get("repository_url").asText().length()>512) throw ProjectClient.invalid();
            if (!input.get("repository_url").asText().strip().matches("(?i)https://(?:www\\.)?github\\.com/[a-z0-9][a-z0-9._-]{0,99}/[a-z0-9][a-z0-9._-]{0,103}/?"))
                throw new com.jobhunting.platform.auth.AuthException("PROJECT_REPOSITORY_INVALID","请填写有效的公开 GitHub 仓库首页链接。",org.springframework.http.HttpStatus.BAD_REQUEST);
            if(input.hasNonNull("idempotency_key") && (!input.get("idempotency_key").isTextual()
                || !input.get("idempotency_key").asText().matches("[A-Za-z0-9:_-]{16,128}"))) throw ProjectClient.invalid();
        }
        if (input.hasNonNull("confirmed_summary") && (!input.get("confirmed_summary").isTextual()
                || input.get("confirmed_summary").asText().codePointCount(0,input.get("confirmed_summary").asText().length())>20000)) throw ProjectClient.invalid();
        if (input.hasNonNull("root_request_id") && (!input.get("root_request_id").isTextual()
                || !input.get("root_request_id").asText().matches("[A-Fa-f0-9]{32}"))) throw ProjectClient.invalid();
        long owner=accounts.current(session).account().account_id();
        return python.execute(operation,session,owner,(ObjectNode)input,
            WebSecurityFilter.requestId(request.getHeader("X-Request-ID"),request.getHeader("X-Trace-Id")));
    }
}
