package com.jobhunting.platform.usage;

import java.util.Map;
import com.jobhunting.platform.auth.AccountAdministrationService;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Service;

@Service
@ConditionalOnProperty(prefix = "platform.auth", name = "enabled", havingValue = "true")
public class AdminToolTraceService {
    private final AccountAdministrationService accounts;
    private final AdminToolTraceQueryService queries;

    public AdminToolTraceService(AccountAdministrationService accounts, AdminToolTraceQueryService queries) {
        this.accounts = accounts;
        this.queries = queries;
    }

    public AdminToolTraceQueryService.TracePage list(String session, Long accountId, int limit, long offset) {
        accounts.authorizeAdministrator(session);
        return queries.list(accountId, limit, offset);
    }

    public Map<String, Object> detail(String session, String rootRequestId) {
        accounts.authorizeAdministrator(session);
        return queries.detail(rootRequestId);
    }
}
