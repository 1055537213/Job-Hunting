import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const source = readFileSync(new URL("../src/job_hunting_agent/web_static/app.js", import.meta.url), "utf8");
const start = source.indexOf("async deleteCurrentProfile() {");
const end = source.indexOf("      /**", start + 1);
const remove = new Function(`return (${source.slice(start, end).trim()
  .replace("async deleteCurrentProfile() {", "async function deleteCurrentProfile() {").replace(/,\s*$/, "")})`)();
globalThis.window = { confirm: () => true };
const removed = [];
globalThis.localStorage = { removeItem: key => removed.push(key) };
function context() {
  return { auth: { account: { id: 7 } }, currentProfile: { name: "Candidate" }, currentProfileId: 13,
    deletingProfileId: 0, activeSessionId: "old", messages: ["old"], notices: [],
    appendAssistant(text, error) { this.notices.push({ text, error }); }, async loadProfiles() {},
    async requestJson() { return { deleted: true, candidate_id: 13, cleanup_status: "pending" }; },
  };
}
const pending = context();
await remove.call(pending);
assert.equal(pending.currentProfileId, 0);
assert.equal(pending.deletingProfileId, 0);
assert.match(pending.notices[0].text, /后台清理/);
assert.deepEqual(pending.messages, []);
const failed = context();
failed.requestJson = async () => { throw new Error("busy"); };
await remove.call(failed);
assert.equal(failed.currentProfileId, 13);
assert.deepEqual(failed.messages, ["old"]);
assert.equal(failed.deletingProfileId, 0);
const switched = context();
switched.requestJson = async () => { switched.currentProfileId = 14; return { cleanup_status: "completed" }; };
await remove.call(switched);
assert.equal(switched.currentProfileId, 14);
assert.deepEqual(switched.messages, ["old"], "a different selected profile must not be cleared");
const switchedAccount = context();
let reloaded = false;
switchedAccount.loadProfiles = async () => { reloaded = true; };
switchedAccount.requestJson = async () => { switchedAccount.auth.account.id = 8; return { cleanup_status: "completed" }; };
await remove.call(switchedAccount);
assert.equal(reloaded, false);
assert.equal(switchedAccount.currentProfileId, 13);
assert.ok(removed.includes("activeSessionId:7:13"));
console.log("frontend profile deletion/pending/failure/context isolation regression: PASS");
