import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const source = readFileSync(new URL("../src/job_hunting_agent/web_static/app.js", import.meta.url), "utf8");
const start = source.indexOf("async submitGitHubProject() {");
const end = source.indexOf("      /**", start + 1);
const submit = new Function(`return (${source.slice(start, end).trim()
  .replace("async submitGitHubProject() {", "async function submitGitHubProject() {").replace(/,\s*$/, "")})`)();
let fail = true;
let sequence = 0;
const requests = [], polled = [];
const ctx = {
  currentProfileId: 7, githubProjectUrl: "https://github.com/example/project", githubProjectSubmission: null,
  submittingGitHubProject: false, backgroundTasks: {},
  openWorkspacePanel() {}, appendAssistant() {}, showDuplicateNotice: () => false, rememberProjectTask() {},
  newIdempotencyKey: () => `github:request-${++sequence}`, pollGitHubProjectTask: (...args) => polled.push(args),
  async requestJson(path, options) {
    requests.push(JSON.parse(options.body));
    if (fail) throw Object.assign(new Error("unconfirmed"), { status: 503 });
    ctx.currentProfileId = 9;
    return { task: { task_key: "task-key" } };
  },
};
await submit.call(ctx);
await submit.call(ctx);
assert.equal(requests[0].idempotency_key, requests[1].idempotency_key, "unconfirmed submission retains its key");
ctx.githubProjectUrl = "https://github.com/example/other";
await submit.call(ctx);
assert.notEqual(requests[2].idempotency_key, requests[0].idempotency_key);
ctx.submittingGitHubProject = true;
await submit.call(ctx);
assert.equal(requests.length, 3);
ctx.submittingGitHubProject = false;
fail = false;
await submit.call(ctx);
assert.equal(ctx.githubProjectSubmission, null);
assert.deepEqual(polled, [["task-key", 7]], "profile switches cannot retarget the submitted task");
assert.equal(ctx.submittingGitHubProject, false);
ctx.githubProjectUrl = "https://github.com/example/project";
ctx.requestJson = async () => { throw Object.assign(new Error("cancelled"), { code: "PROJECT_SUBMISSION_CONFLICT" }); };
await submit.call(ctx);
assert.equal(ctx.githubProjectSubmission, null, "cancelled receipt requires a fresh explicit submission");
console.log("frontend GitHub submission idempotency regression: PASS");
