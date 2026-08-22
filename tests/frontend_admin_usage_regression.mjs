import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const appPath = new URL(
  "../src/job_hunting_agent/web_static/app.js",
  import.meta.url,
);
const indexPath = new URL(
  "../src/job_hunting_agent/web_static/index.html",
  import.meta.url,
);
const stylesPath = new URL(
  "../src/job_hunting_agent/web_static/styles.css",
  import.meta.url,
);
const source = readFileSync(appPath, "utf8");
const template = readFileSync(indexPath, "utf8");
const styles = readFileSync(stylesPath, "utf8");

globalThis.PINYIN_COLLATOR = new Intl.Collator("zh-CN-u-co-pinyin");
globalThis.ADMIN_LEDGER_PAGE_SIZE = 100;
globalThis.ADMIN_LEDGER_MAX_PAGES = 5;

function extractMethod(signature, functionSignature) {
  const start = source.indexOf(signature);
  const end = source.indexOf("      /**", start + 1);
  assert.notEqual(start, -1, `${signature} must exist`);
  assert.notEqual(end, -1, `${signature} must be followed by another method`);
  return new Function(
    `return (${source.slice(start, end).trim().replace(signature, functionSignature).replace(/,\s*$/, "")})`,
  )();
}

assert.match(template, /class="panel admin-panel admin-account-ledger"/);
assert.doesNotMatch(template, /class="admin-grid"/);
assert.match(template, /class="admin-account-workspace"/);
assert.match(template, /class="admin-account-directory"/);
assert.match(template, /v-for="account in admin\.accounts"/);
assert.match(template, /@click="selectAdminAccount\(account\.id\)"/);
assert.match(template, /aria-controls="adminUsageDetail"/);
assert.match(template, /v-if="selectedAdminAccount"/);
assert.doesNotMatch(template, /adminAccountUsage-\$\{account\.id\}/);
assert.match(template, /@click\.stop="toggleAccountStatus\(account\)"/);
assert.match(template, /v-for="event in admin\.events"/);
assert.doesNotMatch(template, /<th>账号<\/th>/);
assert.doesNotMatch(template, /class="admin-account-event-count"/);
assert.doesNotMatch(template, /accountBillingStateLabel\(account\.id\)[\s\S]*?accountToolCallCount\(account\.id\)/);
assert.match(template, /class="admin-account-usage-meta"[\s\S]*?selectedAdminAccount\.role[\s\S]*?selectedAdminAccount\.status[\s\S]*?accountBalanceLedgerCount\(selectedAdminAccount\.id\)[\s\S]*?accountUsageEventCount\(selectedAdminAccount\.id\)[\s\S]*?个工具任务/);
assert.match(template, /选择一个账号/);
assert.match(template, /styles\.css\?v=20260823-cleanup-v1/);
assert.match(template, /app\.js\?v=20260823-project-collapse-v1/);
assert.match(template, /工具调用/);
assert.match(template, /class="panel admin-panel admin-observability"/);
assert.match(template, /请求观测/);
assert.match(template, /class="admin-observability-summary"/);
assert.match(template, /class="admin-observability-grid"/);
assert.match(template, /class="admin-observability-errors"/);
assert.match(template, /adminRequestStatusRows/);
assert.match(template, /adminRequestMethodRows/);
assert.match(template, /adminRequestEndpointRows/);
assert.match(template, /adminRecentRequestErrors/);
assert.match(template, /class="panel admin-panel admin-audit-panel"/);
assert.match(template, /管理员审计/);
assert.match(template, /v-for="event in adminAuditEvents"/);
assert.match(template, /adminAuditActionLabel\(event\.action\)/);
assert.match(template, /adminAuditTargetLabel\(event\)/);
assert.match(template, /class="admin-tool-workspace"/);
assert.match(template, /class="admin-tool-list"/);
assert.match(template, /class="admin-tool-detail"/);
assert.match(template, /class="admin-ledger-pagination"/);
assert.match(template, /aria-label="Token 明细分页"/);
assert.match(template, /aria-label="工具调用分页"/);
assert.match(template, /adminLedgerPageInfo\(admin\.usageTotal\)/);
assert.match(template, /adminLedgerPageInfo\(admin\.toolTraceTotal\)/);
assert.match(template, /adminLedgerPageNumbers\(admin\.usageTotal\)/);
assert.match(template, /adminLedgerPageNumbers\(admin\.toolTraceTotal\)/);
assert.match(template, /v-for="trace in admin\.toolTraces"/);
assert.match(template, /@click="selectAdminToolTrace\(trace\.root_request_id\)"/);
assert.match(template, /selectedAdminToolTraceDetail\.trace\?\.steps/);
assert.doesNotMatch(template, /selectedAdminToolTrace\.trace\?\.steps/);

assert.match(source, /selectedAccountId:\s*0/);
assert.match(source, /loadingEvents:\s*false/);
assert.match(source, /toolTraces:\s*\[\]/);
assert.match(source, /auditEvents:\s*\[\]/);
assert.match(source, /requestMetrics:\s*\{\}/);
assert.match(source, /selectedToolTraceId:\s*""/);
assert.match(source, /usageTotal:\s*0/);
assert.match(source, /usagePage:\s*1/);
assert.match(source, /ledgerPageSize:\s*ADMIN_LEDGER_PAGE_SIZE/);
assert.match(source, /ledgerMaxPages:\s*ADMIN_LEDGER_MAX_PAGES/);
assert.match(source, /toolTracePage:\s*1/);
assert.match(source, /short:\s*"用量"/);
assert.match(source, /short:\s*"观测"/);
assert.match(source, /short:\s*"审计"/);
assert.match(source, /selectAdminAccount\(accountId\)/);
assert.match(source, /loadAdminUsageEvents\(accountId = this\.admin\.selectedAccountId, page = this\.admin\.usagePage\)/);
assert.match(source, /loadAdminToolTraces\(accountId = this\.admin\.selectedAccountId, page = this\.admin\.toolTracePage\)/);
assert.match(source, /selectAdminToolTrace\(rootRequestId\)/);
assert.match(source, /adminRequestStatusRows\(\)/);
assert.match(source, /adminRequestMethodRows\(\)/);
assert.match(source, /adminRequestEndpointRows\(\)/);
assert.match(source, /adminRecentRequestErrors\(\)/);
assert.match(source, /adminAuditEvents\(\)/);
assert.match(source, /adminLedgerPageCount\(total\)/);
assert.match(source, /adminLedgerPageNumbers\(total\)/);
assert.match(source, /adminLedgerPageInfo\(total\)/);
assert.match(source, /label:\s*"HTTP 请求"/);
assert.match(source, /label:\s*"错误请求"/);
assert.match(source, /label:\s*"平均耗时"/);
assert.match(source, /label:\s*"安全拦截"/);
assert.match(source, /loadAdminAuditEvents\(\)/);
assert.match(source, /adminAccountLabel\(accountId\)/);
assert.match(source, /adminAuditTargetLabel\(event\)/);
assert.match(source, /adminAuditActionLabel\(action\)/);
assert.match(source, /sortedMetricRows\(source, limit = Infinity\)/);
assert.match(source, /\/api\/admin\/usage\/events\?account_id=\$\{encodeURIComponent\(selectedAccountId\)\}&limit=\$\{encodeURIComponent\(pageSize\)\}&offset=\$\{encodeURIComponent\(\(requestedPage - 1\) \* pageSize\)\}/);
assert.match(source, /\/api\/admin\/observability\/requests/);
assert.match(source, /\/api\/admin\/audit\/events\?limit=30/);
assert.match(source, /\/api\/admin\/tools\/traces\?account_id=\$\{encodeURIComponent\(selectedAccountId\)\}&limit=\$\{encodeURIComponent\(pageSize\)\}&offset=\$\{encodeURIComponent\(\(requestedPage - 1\) \* pageSize\)\}/);
assert.match(source, /\/api\/admin\/tools\/traces\/\$\{encodeURIComponent\(traceId\)\}/);
assert.match(source, /usageRequestVersion/);
assert.match(source, /toolTraceRequestVersion/);
assert.match(source, /toolTraceDetailRequestVersion/);
assert.match(source, /accountUsageEventCount\(accountId\)/);
assert.match(source, /accountToolCallCount\(accountId\)/);
assert.match(source, /accountToolCallFailureCount\(accountId\)/);
assert.doesNotMatch(source, /requestJson\("\/api\/admin\/usage\/events\?limit=200"\)/);

assert.match(styles, /\.admin-account-ledger\s*\{[\s\S]*?display:\s*grid;/);
assert.match(styles, /\.admin-account-select\s*\{[\s\S]*?grid-template-columns:\s*minmax\(0, 1fr\) var\(--space-lg\);/);
assert.doesNotMatch(styles, /\.admin-account-event-count\s*\{/);
assert.match(styles, /\.admin-account-usage-head\s*\{[\s\S]*?grid-template-columns:\s*minmax\(0, 1fr\);/);
assert.match(styles, /\.admin-account-usage-head h3\s*\{[\s\S]*?white-space:\s*nowrap;[\s\S]*?text-overflow:\s*ellipsis;/);
assert.match(styles, /\.admin-account-usage-meta\s*\{[\s\S]*?justify-self:\s*end;[\s\S]*?text-align:\s*end;[\s\S]*?white-space:\s*nowrap;[\s\S]*?overflow-x:\s*auto;/);
assert.match(styles, /\.admin-account-workspace\s*\{[\s\S]*?grid-template-columns:\s*minmax\(17rem, 0\.82fr\) minmax\(0, 1\.78fr\);/);
assert.match(styles, /\.admin-account-workspace\s*\{[\s\S]*?gap:\s*0;/);
assert.match(styles, /\.admin-account-directory\s*\{[\s\S]*?border-inline-end:\s*1px solid var\(--auth-rule\);/);
assert.match(styles, /\.admin-account-workspace\s*\{[\s\S]*?align-items:\s*start;/);
assert.match(styles, /\.admin-account-directory\s*\{[\s\S]*?grid-template-rows:\s*auto auto;[\s\S]*?align-self:\s*start;/);
assert.match(styles, /\.admin-account-usage\s*\{[\s\S]*?min-block-size:\s*0;[\s\S]*?grid-template-rows:\s*auto auto auto;[\s\S]*?align-self:\s*start;/);
assert.match(styles, /\.admin-sidebar\s*\{[\s\S]*?grid-template-rows:\s*auto auto auto;[\s\S]*?align-self:\s*start;/);
assert.match(styles, /\.admin-nav\s*\{[\s\S]*?align-content:\s*start;[\s\S]*?overflow:\s*visible;/);
assert.match(styles, /\.admin-observability\s*\{[\s\S]*?display:\s*grid;/);
assert.match(styles, /\.admin-observability-summary\s*\{[\s\S]*?grid-template-columns:\s*repeat\(3, minmax\(0, 1fr\)\);/);
assert.match(styles, /\.admin-observability-grid\s*\{[\s\S]*?grid-template-columns:\s*minmax\(0, 1fr\) minmax\(0, 1\.08fr\);/);
assert.match(styles, /\.admin-observability-section,[\s\S]*?border:\s*1px solid var\(--auth-rule\);/);
assert.match(styles, /\.admin-observability-table-wrap\s*\{[\s\S]*?max-block-size:\s*min\(19rem, 36svh\);/);
assert.match(styles, /\.admin-observability-error-list\s*\{[\s\S]*?display:\s*grid;/);
assert.match(styles, /\.admin-audit-panel\s*\{[\s\S]*?display:\s*grid;/);
assert.match(styles, /\.admin-audit-list\s*\{[\s\S]*?max-block-size:\s*min\(34rem, 56svh\);/);
assert.match(styles, /\.admin-audit-event\s*\{[\s\S]*?border:\s*1px solid var\(--auth-rule\);/);
assert.match(styles, /\.admin-audit-event-meta\s*\{[\s\S]*?grid-template-columns:\s*repeat\(4, minmax\(0, 1fr\)\);/);
assert.match(styles, /\.admin-detail-tabs\s*\{[\s\S]*?width:\s*100%;[\s\S]*?display:\s*grid;[\s\S]*?grid-template-columns:\s*repeat\(3, minmax\(0, 1fr\)\);[\s\S]*?align-items:\s*start;/);
assert.match(styles, /\.tab-button\s*\{[\s\S]*?min-height:\s*calc\(var\(--size-control\) - var\(--space-sm\)\);[\s\S]*?width:\s*100%;[\s\S]*?padding:\s*var\(--space-3xs\) var\(--space-sm\);[\s\S]*?text-align:\s*center;[\s\S]*?white-space:\s*nowrap;/);
assert.match(styles, /\.tab-button\[aria-selected="true"\]\s*\{[\s\S]*?border-color:\s*var\(--auth-rule-strong\);/);
assert.match(styles, /\.admin-account-chevron\s*\{[\s\S]*?place-items:\s*center;/);
assert.match(styles, /\.admin-table-wrap\s*\{[\s\S]*?max-block-size:\s*min\(38rem, calc\(100svh - 25rem\)\);[\s\S]*?overflow:\s*auto;[\s\S]*?overscroll-behavior:\s*contain;/);
assert.match(styles, /\.admin-table th\s*\{[\s\S]*?position:\s*sticky;[\s\S]*?inset-block-start:\s*0;/);
assert.match(styles, /\.admin-nav-item\s*\{[\s\S]*?grid-template-columns:\s*var\(--size-control\) minmax\(0, 1fr\) minmax\(4\.5rem, 5\.25rem\) var\(--space-lg\);/);
assert.match(styles, /\.admin-nav-item\s*\{[\s\S]*?block-size:\s*calc\(var\(--size-control\) \+ var\(--space-xl\)\);/);
assert.match(styles, /\.admin-nav-badge\s*\{[\s\S]*?text-overflow:\s*ellipsis;/);
assert.doesNotMatch(styles, /--admin-bar\s*:\s*oklch\(/);

const loadAdminData = extractMethod(
  "async loadAdminData() {",
  "async function loadAdminData() {",
);
const loadAdminAuditEvents = extractMethod(
  "async loadAdminAuditEvents() {",
  "async function loadAdminAuditEvents() {",
);
const selectAdminAccount = extractMethod(
  "async selectAdminAccount(accountId) {",
  "async function selectAdminAccount(accountId) {",
);
const isAdminAccountSelected = extractMethod(
  "isAdminAccountSelected(accountId) {",
  "function isAdminAccountSelected(accountId) {",
);
const clearAdminUsageSelection = extractMethod(
  "clearAdminUsageSelection() {",
  "function clearAdminUsageSelection() {",
);
const loadAdminUsageEvents = extractMethod(
  "async loadAdminUsageEvents(accountId = this.admin.selectedAccountId, page = this.admin.usagePage) {",
  "async function loadAdminUsageEvents(accountId = this.admin.selectedAccountId, page = this.admin.usagePage) {",
);
const loadAdminActiveDetail = extractMethod(
  "async loadAdminActiveDetail(accountId = this.admin.selectedAccountId) {",
  "async function loadAdminActiveDetail(accountId = this.admin.selectedAccountId) {",
);
const loadAdminToolTraces = extractMethod(
  "async loadAdminToolTraces(accountId = this.admin.selectedAccountId, page = this.admin.toolTracePage) {",
  "async function loadAdminToolTraces(accountId = this.admin.selectedAccountId, page = this.admin.toolTracePage) {",
);
const selectAdminToolTrace = extractMethod(
  "async selectAdminToolTrace(rootRequestId) {",
  "async function selectAdminToolTrace(rootRequestId) {",
);
const setAdminDetailTab = extractMethod(
  "async setAdminDetailTab(tab) {",
  "async function setAdminDetailTab(tab) {",
);
const sortedMetricRows = extractMethod(
  "sortedMetricRows(source, limit = Infinity) {",
  "function sortedMetricRows(source, limit = Infinity) {",
);
const adminLedgerPageCount = extractMethod(
  "adminLedgerPageCount(total) {",
  "function adminLedgerPageCount(total) {",
);
const adminLedgerPageNumbers = extractMethod(
  "adminLedgerPageNumbers(total) {",
  "function adminLedgerPageNumbers(total) {",
);
const adminLedgerPageInfo = extractMethod(
  "adminLedgerPageInfo(total) {",
  "function adminLedgerPageInfo(total) {",
);
const adminAccountLabel = extractMethod(
  "adminAccountLabel(accountId) {",
  "function adminAccountLabel(accountId) {",
);
const adminAuditTargetLabel = extractMethod(
  "adminAuditTargetLabel(event) {",
  "function adminAuditTargetLabel(event) {",
);
const adminAuditActionLabel = extractMethod(
  "adminAuditActionLabel(action) {",
  "function adminAuditActionLabel(action) {",
);

const requestUrls = [];
const selectionContext = {
  admin: {
    accounts: [],
    events: [],
    usageTotal: 0,
    usagePage: 1,
    ledgerPageSize: ADMIN_LEDGER_PAGE_SIZE,
    ledgerMaxPages: ADMIN_LEDGER_MAX_PAGES,
    summary: {},
    selectedAccountId: 0,
    loadingEvents: false,
    eventsError: "",
    loadError: "",
    usageRequestVersion: 0,
    activeDetailTab: "tokens",
    toolTraces: [],
    selectedToolTraceId: "",
    toolTraceDetail: null,
    loadingToolTraces: false,
    loadingToolTraceDetail: false,
    toolTracesError: "",
    toolTraceDetailError: "",
    toolTraceRequestVersion: 0,
    toolTraceDetailRequestVersion: 0,
    auditEvents: [],
    loadingAuditEvents: false,
    auditLoadError: "",
    auditRequestVersion: 0,
    toolTraceTotal: 0,
    toolTracePage: 1,
  },
  requestJson: async (url) => {
    requestUrls.push(url);
    return {
      events: [{ id: 11, account_id: 7, total_tokens: 42 }],
      total: 1,
      limit: ADMIN_LEDGER_PAGE_SIZE,
      offset: 0,
      page_size: ADMIN_LEDGER_PAGE_SIZE,
      max_pages: ADMIN_LEDGER_MAX_PAGES,
    };
  },
  adminLedgerPageCount,
  adminLedgerPageNumbers,
  adminLedgerPageInfo,
  isAdminAccountSelected,
  clearAdminUsageSelection,
  loadAdminActiveDetail,
  loadAdminUsageEvents,
  loadAdminToolTraces,
};

await selectAdminAccount.call(selectionContext, 7);
assert.equal(selectionContext.admin.selectedAccountId, 7);
assert.deepEqual(selectionContext.admin.events, [{ id: 11, account_id: 7, total_tokens: 42 }]);
assert.deepEqual(requestUrls, ["/api/admin/usage/events?account_id=7&limit=100&offset=0"]);

await selectAdminAccount.call(selectionContext, 7);
assert.equal(selectionContext.admin.selectedAccountId, 7);
assert.deepEqual(requestUrls, ["/api/admin/usage/events?account_id=7&limit=100&offset=0"]);

const adminLoadUrls = [];
let adminAuditLoaded = false;
const adminLoadContext = {
  admin: {
    accounts: [],
    events: [{ id: 99 }],
    usageTotal: 0,
    usagePage: 1,
    ledgerPageSize: ADMIN_LEDGER_PAGE_SIZE,
    ledgerMaxPages: ADMIN_LEDGER_MAX_PAGES,
    summary: {},
    requestMetrics: {},
    activeDetailTab: "tokens",
    selectedAccountId: 0,
    loadingEvents: false,
    loadingToolTraces: false,
    loadingToolTraceDetail: false,
    eventsError: "",
    toolTraces: [],
    selectedToolTraceId: "",
    toolTraceDetail: null,
    toolTracesError: "",
    toolTraceDetailError: "",
    auditEvents: [],
    loadingAuditEvents: false,
    auditLoadError: "",
    loadError: "",
    usageRequestVersion: 0,
    toolTraceRequestVersion: 0,
    toolTraceDetailRequestVersion: 0,
    auditRequestVersion: 0,
    toolTraceTotal: 0,
    toolTracePage: 1,
  },
  requestJson: async (url) => {
    adminLoadUrls.push(url);
    if (url === "/api/admin/accounts") return { accounts: [{ id: 7, email: "admin@example.com" }] };
    if (url === "/api/admin/usage/summary") {
      return {
        summary: { event_count: 1 },
        by_account: [],
        tool_calls_by_account: [],
        billing: {
          settings: {
            configured: true,
            price_per_million_tokens_yuan: 25,
            starting_balance_yuan: 100,
            low_balance_threshold_yuan: 10,
          },
          summary: {
            configured: true,
            price_per_million_tokens_yuan: 25,
            starting_balance_yuan: 100,
            low_balance_threshold_yuan: 10,
            account_count: 1,
            healthy_account_count: 1,
            low_balance_account_count: 0,
            suspended_account_count: 0,
            total_balance_micro_yuan: 100000000,
            total_recharge_micro_yuan: 100000000,
            total_consumed_micro_yuan: 0,
            total_ledger_entry_count: 1,
          },
          by_account: [
            {
              account_id: 7,
              email: "admin@example.com",
              role: "admin",
              status: "active",
              balance_micro_yuan: 100000000,
              total_recharge_micro_yuan: 100000000,
              total_consumed_micro_yuan: 0,
              ledger_entry_count: 1,
              low_balance_threshold_micro_yuan: 10000000,
              state: "balance",
              state_label: "余额",
            },
          ],
        },
        page_size: ADMIN_LEDGER_PAGE_SIZE,
        max_pages: ADMIN_LEDGER_MAX_PAGES,
      };
    }
    if (url === "/api/admin/observability/requests") {
      return { requests: { total_requests: 12, error_requests: 1, average_duration_ms: 8.5 } };
    }
    throw new Error(`unexpected URL ${url}`);
  },
  clearAdminUsageSelection,
  loadAdminAuditEvents: async function () {
    adminAuditLoaded = true;
    this.admin.auditEvents = [{ id: 17, action: "account.status_updated" }];
  },
  adminLedgerPageCount,
  loadAdminActiveDetail,
  loadAdminUsageEvents: async () => {
    throw new Error("details should not load before an account is selected");
  },
};

await loadAdminData.call(adminLoadContext);
assert.deepEqual(adminLoadUrls.sort(), [
  "/api/admin/accounts",
  "/api/admin/observability/requests",
  "/api/admin/usage/summary",
]);
assert.equal(adminLoadContext.admin.selectedAccountId, 0);
assert.deepEqual(adminLoadContext.admin.events, []);
assert.equal(adminLoadContext.admin.ledgerPageSize, ADMIN_LEDGER_PAGE_SIZE);
assert.equal(adminLoadContext.admin.ledgerMaxPages, ADMIN_LEDGER_MAX_PAGES);
assert.equal(adminAuditLoaded, true);
assert.deepEqual(adminLoadContext.admin.auditEvents, [{ id: 17, action: "account.status_updated" }]);
assert.deepEqual(adminLoadContext.admin.requestMetrics, {
  total_requests: 12,
  error_requests: 1,
  average_duration_ms: 8.5,
});
assert.equal(adminLoadContext.admin.billing.summary.account_count, 1);
assert.equal(adminLoadContext.admin.billing.by_account[0].state_label, "余额");

const auditUrls = [];
const auditContext = {
  admin: {
    auditEvents: [],
    loadingAuditEvents: false,
    auditLoadError: "",
    auditRequestVersion: 0,
  },
  requestJson: async (url) => {
    auditUrls.push(url);
    return {
      events: [
        {
          id: 1,
          actor_account_id: 7,
          target_account_id: 8,
          action: "account.status_updated",
          target_type: "account",
          outcome: "succeeded",
        },
      ],
    };
  },
};
await loadAdminAuditEvents.call(auditContext);
assert.deepEqual(auditUrls, ["/api/admin/audit/events?limit=30"]);
assert.equal(auditContext.admin.loadingAuditEvents, false);
assert.deepEqual(auditContext.admin.auditEvents, [
  {
    id: 1,
    actor_account_id: 7,
    target_account_id: 8,
    action: "account.status_updated",
    target_type: "account",
    outcome: "succeeded",
  },
]);

const toolUrls = [];
const toolContext = {
  admin: {
    selectedAccountId: 7,
    activeDetailTab: "tokens",
    toolTraces: [],
    selectedToolTraceId: "",
    toolTraceDetail: null,
    loadingToolTraces: false,
    loadingToolTraceDetail: false,
    toolTracesError: "",
    toolTraceDetailError: "",
    toolTraceRequestVersion: 0,
    toolTraceDetailRequestVersion: 0,
    toolTraceTotal: 0,
    toolTracePage: 1,
    usagePage: 1,
    usageTotal: 0,
    ledgerPageSize: ADMIN_LEDGER_PAGE_SIZE,
    ledgerMaxPages: ADMIN_LEDGER_MAX_PAGES,
  },
  requestJson: async (url) => {
    toolUrls.push(url);
    if (url.startsWith("/api/admin/tools/traces?")) {
      return {
        traces: [{ root_request_id: "root-1", title: "导入职位信息", step_count: 1 }],
        total: 1,
      };
    }
    if (url === "/api/admin/tools/traces/root-1") {
      return {
        trace: {
          root_request_id: "root-1",
          title: "导入职位信息",
          trace: { steps: [{ id: "step-1", label: "解析职位信息", status: "completed" }] },
        },
      };
    }
    throw new Error(`unexpected URL ${url}`);
  },
  isAdminAccountSelected,
  adminLedgerPageCount,
  loadAdminActiveDetail,
  loadAdminUsageEvents: async () => {
    throw new Error("token detail should not load while tools tab is active");
  },
  loadAdminToolTraces,
};

await setAdminDetailTab.call(toolContext, "tools");
assert.deepEqual(toolContext.admin.toolTraces, [
  { root_request_id: "root-1", title: "导入职位信息", step_count: 1 },
]);
await selectAdminToolTrace.call(toolContext, "root-1");
assert.equal(toolContext.admin.selectedToolTraceId, "root-1");
assert.deepEqual(toolContext.admin.toolTraceDetail.trace.steps, [
  { id: "step-1", label: "解析职位信息", status: "completed" },
]);
assert.deepEqual(toolUrls, [
  "/api/admin/tools/traces?account_id=7&limit=100&offset=0",
  "/api/admin/tools/traces/root-1",
]);

assert.deepEqual(
  sortedMetricRows.call({}, { c: 0, b: 1, a: 3 }, 2),
  [
    { label: "a", count: 3 },
    { label: "b", count: 1 },
  ],
);

const labelContext = {
  admin: {
    accounts: [{ id: 7, email: "admin@example.com" }],
  },
  adminAccountLabel,
};
assert.equal(adminAccountLabel.call(labelContext, 7), "admin@example.com");
assert.equal(adminAccountLabel.call(labelContext, 9), "账号 #9");
assert.equal(
  adminAuditTargetLabel.call(labelContext, { target_account_id: 7 }),
  "admin@example.com",
);
assert.equal(
  adminAuditTargetLabel.call(labelContext, { target_type: "background_task", target_id: "task-1" }),
  "background_task #task-1",
);
assert.equal(adminAuditActionLabel.call({}, "account.status_updated"), "账号状态变更");
assert.equal(adminAuditActionLabel.call({}, "unknown.action"), "unknown.action");

console.log("frontend admin usage regression: PASS");
