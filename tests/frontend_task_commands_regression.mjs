import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const source = readFileSync(new URL("../src/job_hunting_agent/web_static/app.js", import.meta.url), "utf8");
const signature = "async retryAdminBackgroundTask(taskKey)";
const start = source.indexOf(signature);
const end = source.indexOf("      /**", start + 1);
const retry = new Function(`return (${source.slice(start, end).trim().replace(signature, "async function(taskKey)").replace(/,\s*$/, "")})`)();
const requests = [];
const notices = [];
let offline = true;
let release;
const ctx = {
  admin: { backgroundTaskBusy: {}, backgroundTaskCommandKeys: {}, backgroundTaskPage: 1 },
  newIdempotencyKey: () => "retry-stable-key",
  requestJson: async (url, options) => {
    requests.push({ url, options });
    if (offline) {
      await new Promise(resolve => { release = resolve; });
      const error = new Error("queue unavailable");
      error.status = 503;
      throw error;
    }
    return { task: { task_key: "task-1", status: "queued" } };
  },
  showAccountAction: (message, success) => notices.push({ message, success }),
  loadAdminBackgroundTasks: async () => {},
  loadAdminAuditEvents: async () => {},
};
const pending = retry.call(ctx, "task-1");
await retry.call(ctx, "task-1");
assert.equal(requests.length, 1);
release();
await pending;
assert.equal(ctx.admin.backgroundTaskBusy["task-1"], false);
assert.equal(ctx.admin.backgroundTaskCommandKeys["task-1"], "retry-stable-key");
offline = false;
await retry.call(ctx, "task-1");
assert.equal(requests[0].options.headers["Idempotency-Key"], requests[1].options.headers["Idempotency-Key"]);
assert.equal(ctx.admin.backgroundTaskCommandKeys["task-1"], undefined);
ctx.loadAdminBackgroundTasks = async () => { throw new Error("refresh failed"); };
await retry.call(ctx, "task-1");
assert.equal(notices.at(-1).success, true);
assert.match(notices.at(-1).message, /已重新排队.*刷新失败/);
console.log("frontend task command idempotency regression: PASS");
