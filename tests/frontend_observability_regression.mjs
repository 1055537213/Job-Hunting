import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../src/job_hunting_agent/web_static/app.js", import.meta.url), "utf8");
const template = readFileSync(new URL("../src/job_hunting_agent/web_static/index.html", import.meta.url), "utf8");
let options;
vm.runInNewContext(source, { window: { Vue: {
  createApp(value) { options = value; return { mount() {} }; }, nextTick() {},
} } });
const methods = options.methods;
function context() {
  return { ...methods, admin: {
    observabilityScope: "all", requestMetrics: null, observabilityServices: null,
    observabilityRequestVersion: 0, observabilityStale: false,
    emailOutbox: { status: "unavailable", summary: null, records: [] },
  } };
}
const ctx = context();
assert.equal(ctx.adminMetricValue(null, "total_requests"), "--");
assert.equal(ctx.adminMetricValue({ total_requests: 0 }, "total_requests"), "0");
assert.equal(ctx.adminMetricValue({ average_duration_ms: 0 }, "average_duration_ms", "ms"), "0ms");
ctx.applyAdminObservability({ status: "partial", requests: { total_requests: 7 }, services: {
  java: { status: "available", api: { total_requests: 4 }, internal: { total_requests: 9 } },
  python: { status: "unavailable", api: null, internal: null },
}, account_email: { status: "partial", summary: null, records: [{ owner: "java", id: 1 }] } });
// Mail-only partial status must not hide valid API metrics.
assert.equal(options.computed.adminSelectedRequestMetrics.call(ctx).total_requests, 7);
ctx.admin.observabilityScope = "java_internal";
assert.equal(options.computed.adminSelectedRequestMetrics.call(ctx).total_requests, 9);
ctx.admin.observabilityScope = "python";
assert.equal(options.computed.adminSelectedRequestMetrics.call(ctx), null);
assert.equal(ctx.adminEmailMetricValue("sent"), "--");
assert.notEqual(ctx.adminEmailRecordKey({ owner: "java", id: 1 }), ctx.adminEmailRecordKey({ owner: "python", id: 1 }));
assert.notEqual(ctx.adminRequestErrorKey({ service: "java", request_id: "same", created_at: "same" }),
  ctx.adminRequestErrorKey({ service: "python", request_id: "same", created_at: "same" }));

const pending = [];
ctx.requestJson = () => new Promise((resolve, reject) => pending.push({ resolve, reject }));
const first = ctx.loadAdminObservability();
const second = ctx.loadAdminObservability();
pending[1].resolve({ status: "available", requests: { total_requests: 20 } });
await second;
pending[0].resolve({ status: "available", requests: { total_requests: 1 } });
await first;
assert.equal(ctx.admin.requestMetrics.total_requests, 20);
const failed = ctx.loadAdminObservability();
pending[2].reject(new Error("暂时不可用"));
await failed;
assert.equal(ctx.admin.requestMetrics.total_requests, 20);
assert.equal(ctx.admin.observabilityStale, true);
assert.equal(ctx.admin.observabilityLoading, false);
assert.equal(ctx.admin.observabilityError, "暂时不可用");

const scoped = context();
scoped.admin.activeSection = "observability";
let refreshed = false;
scoped.loadAdminObservability = async () => { refreshed = true; };
scoped.loadAdminData = () => { throw new Error("Account reload is not an observability refresh"); };
await scoped.refreshAdminSection();
assert.equal(refreshed, true);
assert.match(template, /observabilityStatus === 'partial' && !admin.requestMetrics/);
assert.match(template, /v-model="admin.observabilityScope"/);
assert.match(template, /数据已过期/);
console.log("frontend observability regression: PASS");
