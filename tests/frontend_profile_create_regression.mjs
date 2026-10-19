import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const source = readFileSync(new URL("../src/job_hunting_agent/web_static/app.js", import.meta.url), "utf8");
const start = source.indexOf("async createProfile() {");
const end = source.indexOf("      /**", start + 1);
const create = new Function(`return (${source.slice(start, end).trim()
  .replace("async createProfile() {", "async function createProfile() {").replace(/,\s*$/, "")})`)();
globalThis.localStorage = { setItem() {} };
const form = () => ({ name: "Candidate", education: "", experienceYears: 0, skills: "", preferredCities: [], directions: "" });
const requests = [];
let reject = true;
const ctx = {
  auth: { account: { id: 7 } }, profileForm: form(), creatingProfile: false, pendingProfileCreate: null,
  parseSkills: () => ({}), splitItems: () => [], appendAssistant() {}, showDuplicateNotice: () => false,
  closeCityPicker() {}, async loadProfiles() {},
  async requestJson(path, options) {
    requests.push({ path, ...options });
    if (reject) throw Object.assign(new Error("unavailable"), { status: 503 });
    return { candidate_id: 13, profile: { name: "Candidate" } };
  },
};
await create.call(ctx);
assert.equal(ctx.creatingProfile, false);
assert.ok(ctx.pendingProfileCreate);
const first = requests[0].headers["Idempotency-Key"];
await create.call(ctx);
assert.equal(requests[1].headers["Idempotency-Key"], first, "same failed intent keeps its key");
ctx.profileForm.name = "Different";
await create.call(ctx);
assert.notEqual(requests[2].headers["Idempotency-Key"], first, "edited form is a new intent");
ctx.creatingProfile = true;
await create.call(ctx);
assert.equal(requests.length, 3, "double click does not start concurrent submissions");
ctx.creatingProfile = false;
reject = false;
await create.call(ctx);
assert.equal(ctx.currentProfileId, 13);
assert.equal(ctx.pendingProfileCreate, null);
assert.equal(ctx.profileForm.name, "");
ctx.profileForm = form();
ctx.requestJson = async () => { throw Object.assign(new Error("deleted"), { code: "PROFILE_CREATE_CONFLICT" }); };
await create.call(ctx);
assert.equal(ctx.pendingProfileCreate, null, "a deleted receipt can only be replaced by a fresh explicit submit");
console.log("frontend profile creation idempotency regression: PASS");
