import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const source = readFileSync(new URL("../src/job_hunting_agent/web_static/app.js", import.meta.url), "utf8");
function method(name) {
  const start = source.indexOf(`async ${name}() {`);
  const end = source.indexOf("      /**", start + 1);
  return new Function(`return (${source.slice(start, end).trim().replace(`async ${name}() {`, `async function ${name}() {`).replace(/,\s*$/, "")})`)();
}
const resume = method("resumePendingLocalProjectCollection");
const cancel = method("cancelLocalProjectCollection");
const memory = new Map([["collection:1", "12"]]);
globalThis.localStorage = { getItem: key => memory.get(key) || null, removeItem: key => memory.delete(key) };
globalThis.window = { confirm: () => true };
const messages = [];
const ctx = {
  currentProfileId: 1, localProjectCollectionId: 12, deletingLocalProjectCollection: false,
  localProjectCollectionStorageKey(id = this.currentProfileId) { return `collection:${id}`; },
  forgetLocalProjectCollection(id = this.currentProfileId) { memory.delete(`collection:${id}`); this.localProjectCollectionId = 0; },
  appendAssistant: text => messages.push(text),
  async requestJson() { throw Object.assign(new Error("offline"), { status: 503 }); },
};
await resume.call(ctx);
assert.equal(memory.get("collection:1"), "12", "transient outage must retain resumable collection");
assert.equal(ctx.localProjectCollectionId, 12);
ctx.requestJson = async () => { throw Object.assign(new Error("gone"), { status: 404 }); };
await resume.call(ctx);
assert.equal(memory.has("collection:1"), false);
memory.set("collection:1", "12");
ctx.requestJson = async () => { ctx.currentProfileId = 2; return { collection: { project_name: "Old profile", uploaded_file_count: 2 } }; };
ctx.localProjectDirectoryName = "New profile";
await resume.call(ctx);
assert.equal(ctx.localProjectDirectoryName, "New profile", "late response must not overwrite a new profile");
ctx.currentProfileId = 1;
ctx.localProjectCollectionId = 12;
ctx.requestJson = async () => ({ deleted: true, cleanup_status: "pending" });
await cancel.call(ctx);
assert.equal(memory.has("collection:1"), false);
assert.equal(ctx.localProjectCollectionId, 0);
assert.match(messages.at(-1), /附件正在后台清理/);
assert.equal(ctx.deletingLocalProjectCollection, false);
memory.set("collection:1", "12");
memory.set("collection:2", "99");
ctx.localProjectCollectionId = 12;
ctx.requestJson = async () => { ctx.currentProfileId = 2; ctx.localProjectCollectionId = 99; return { deleted: true, cleanup_status: "completed" }; };
await cancel.call(ctx);
assert.equal(ctx.localProjectCollectionId, 99);
assert.equal(memory.get("collection:2"), "99", "cancelling old profile must not clear another collection");
console.log("frontend local collection recovery/cancellation/context regression: PASS");
