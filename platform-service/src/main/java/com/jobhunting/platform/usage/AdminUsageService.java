package com.jobhunting.platform.usage;

import java.util.Map;
import com.jobhunting.platform.auth.AccountAdministrationService;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Service;

@Service
@ConditionalOnProperty(prefix = "platform.auth", name = "enabled", havingValue = "true")
public class AdminUsageService {
    private final AccountAdministrationService accounts;
    private final AdminUsageQueryService queries;

    public AdminUsageService(AccountAdministrationService accounts, AdminUsageQueryService queries) {
        this.accounts = accounts;
        this.queries = queries;
    }

    public Map<String, Object> summary(String session) {
        accounts.authorizeAdministrator(session);
        return queries.summary();
    }

    public AdminUsageQueryService.UsagePage events(String session, Long accountId, Long candidateId, String sessionId, int limit, long offset) {
        accounts.authorizeAdministrator(session);
        return queries.events(accountId, candidateId, sessionId, limit, offset);
    }
}
