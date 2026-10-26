import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const source = readFileSync(new URL("../src/job_hunting_agent/web_static/app.js", import.meta.url), "utf8");
function method(name) {
  const start = source.indexOf(`async ${name}(`);
  const end = source.indexOf("      /**", start + 1);
  return new Function(`return (${source.slice(start, end).trim().replace(`async ${name}(`, `async function ${name}(`).replace(/,\s*$/, "")})`)();
}
const analyze = method("analyzeLocalProjectEntries");
const load = method("loadProjectCards");
const memory = new Map();
globalThis.localStorage = { getItem: key => memory.get(key) || null, setItem: (key, value) => memory.set(key, value), removeItem: key => memory.delete(key) };
const entries = [{ relativePath: "README.md", file: new File(["prototype"], "README.md", { type: "text/plain" }) }];
function context() {
  memory.clear();
  const calls = [], messages = [];
  return {
    calls, messages, currentProfileId: 1, submittingLocalProject: false,
    localProjectCollectionStorageKey(id = this.currentProfileId) { return `account:1:profile:${id}`; },
    async sha256LocalProjectFile() { return "a".repeat(64); },
    rememberLocalProjectCollection(id) { this.localProjectCollectionId = id; memory.set(this.localProjectCollectionStorageKey(), String(id)); },
    forgetLocalProjectCollection() { this.localProjectCollectionId = 0; memory.delete(this.localProjectCollectionStorageKey()); },
    appendAssistant: (...args) => messages.push(args),
    trackProjectIndexTask() {}, syncProjectReviewSelections() {}, reconcileTaskApprovals() {},
    loadProjectCards: load,
    async requestJson(path, options) {
      this.calls.push([path, options]);
      if (path.endsWith("manifest")) return { collection: { id: 12, uploaded_file_count: 1 }, selected_files: [], resumed: true };
      if (path.endsWith("complete")) return { project_card: { id: 7 }, processing_async: false };
      return { project_cards: [{ id: 7 }] };
    },
  };
}
let ctx = context();
await analyze.call(ctx, "Design", entries);
assert.equal(memory.size, 0);
assert.equal(ctx.projectCards[0].id, 7);
assert.equal(ctx.submittingLocalProject, false);
assert.equal(ctx.messages.length, 1);
assert.equal(JSON.parse(ctx.calls[0][1].body).candidate_id, 1);

ctx = context();
const request = ctx.requestJson;
ctx.requestJson = async function(path, options) {
  if (path.endsWith("complete")) throw Object.assign(new Error("unconfirmed"), { status: 503 });
  return request.call(this, path, options);
};
await analyze.call(ctx, "Design", entries);
assert.equal(memory.get("account:1:profile:1"), "12", "ambiguous completion must retain the collection for explicit retry");
assert.deepEqual(ctx.messages, [["unconfirmed", true]]);

ctx = context();
ctx.requestJson = async function(path, options) {
  if (path.endsWith("complete")) {
    this.currentProfileId = 2;
    this.localProjectProgress = "new profile";
    memory.set(this.localProjectCollectionStorageKey(), "99");
    return { project_card: { id: 7 }, processing_async: false };
  }
  return request.call(this, path, options);
};
await analyze.call(ctx, "Design", entries);
assert.equal(memory.get("account:1:profile:2"), "99");
assert.equal(memory.has("account:1:profile:1"), false);
assert.equal(ctx.localProjectProgress, "new profile");
assert.equal(ctx.messages.length, 0);
assert.equal(ctx.calls.length, 1, "late completion must not refresh the new profile's project list");

ctx = context();
ctx.sha256LocalProjectFile = async function() { this.currentProfileId = 2; return "a".repeat(64); };
await analyze.call(ctx, "Design", entries);
assert.equal(ctx.calls.length, 0, "switching profile during pre-scan must not submit a manifest");

ctx = context();
ctx.requestJson = async function(path, options) {
  if (path.endsWith("manifest")) return { collection: { id: 12 }, selected_files: [{ id: 1, relative_path: "README.md" }] };
  return request.call(this, path, options);
};
ctx.requestFormJson = async function() { this.currentProfileId = 2; return { processed_files: [{ id: 1 }] }; };
await analyze.call(ctx, "Design", entries);
assert.equal(memory.get("account:1:profile:1"), "12");
assert.equal(ctx.calls.length, 0, "switching after upload must not finalize or render into the new profile");

for (const fail of [false, true]) {
  ctx = context();
  ctx.projectCards = [{ id: 99 }];
  ctx.requestJson = async function() { this.currentProfileId = 2; if (fail) throw new Error("old request"); return { project_cards: [{ id: 7 }] }; };
  await load.call(ctx);
  assert.deepEqual(ctx.projectCards, [{ id: 99 }]);
  assert.equal(ctx.githubProjectError, undefined);
}
console.log("frontend local completion/retry/profile context regression: PASS");
