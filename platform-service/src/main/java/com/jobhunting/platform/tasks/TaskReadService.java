package com.jobhunting.platform.tasks;

import java.util.Map;
import com.jobhunting.platform.auth.AccountAdministrationService;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Service;

@Service
@ConditionalOnProperty(prefix = "platform.auth", name = "enabled", havingValue = "true")
public class TaskReadService {
    private final AccountAdministrationService accounts;
    private final TaskQueryService queries;

    public TaskReadService(AccountAdministrationService accounts, TaskQueryService queries) {
        this.accounts = accounts;
        this.queries = queries;
    }

    public TaskQueryService.TaskPage adminList(String session, Long accountId, String type, String status, int limit, long offset) {
        accounts.authorizeAdministrator(session);
        return queries.adminList(accountId, type, status, limit, offset);
    }

    public Map<String, Object> personal(String session, String key) {
        var account = accounts.current(session).account();
        return queries.personal(account.account_id(), key);
    }
}
