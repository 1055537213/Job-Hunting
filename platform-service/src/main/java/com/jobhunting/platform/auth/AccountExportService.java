package com.jobhunting.platform.auth;

import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Service;
import tools.jackson.databind.JsonNode;

@Service
@ConditionalOnProperty(name = "platform.auth.enabled", havingValue = "true")
public class AccountExportService {
    private final AccountAdministrationService accounts;
    private final AccountExportClient python;

    public AccountExportService(AccountAdministrationService accounts, AccountExportClient python) {
        this.accounts = accounts;
        this.python = python;
    }

    public JsonNode export(String session) {
        // Finish the authentication transaction before waiting on the Python callback.
        var account = accounts.current(session).account();
        return python.export(session, account.account_id());
    }
}
