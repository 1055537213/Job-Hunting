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

function extractMethod(signature, functionSignature) {
  const start = source.indexOf(signature);
  const end = source.indexOf("      /**", start + 1);
  assert.notEqual(start, -1, `${signature} must exist`);
  assert.notEqual(end, -1, `${signature} must be followed by another method`);
  const methodSource = source
    .slice(start, end)
    .trim()
    .replace(signature, functionSignature)
    .replace(/,\s*$/, "");
  return new Function(`return (${methodSource})`)();
}

assert.match(template, /class="panel-title-row profile-summary-title-row"/);
assert.match(template, /class="ghost-button compact profile-summary-refresh"/);
assert.match(template, /:disabled="!currentProfileId \|\| refreshingProfileSummary"/);
assert.match(template, /@click="refreshProfileSummary"/);
assert.match(template, /v-if="profileSummaryError"/);
assert.match(source, /refreshingProfileSummary: false/);
assert.match(source, /profileSummaryError: ""/);
assert.match(styles, /\.profile-summary-refresh svg\s*\{/);

const refreshProfileSummary = extractMethod(
  "async refreshProfileSummary() {",
  "async function refreshProfileSummary() {",
);

let refreshCalls = 0;
const context = {
  currentProfileId: 13,
  refreshingProfileSummary: false,
  profileSummaryError: "旧错误",
  async refreshCurrentProfile() {
    refreshCalls += 1;
  },
};

await refreshProfileSummary.call(context);
assert.equal(refreshCalls, 1);
assert.equal(context.refreshingProfileSummary, false);
assert.equal(context.profileSummaryError, "");

const errorContext = {
  currentProfileId: 13,
  refreshingProfileSummary: false,
  profileSummaryError: "",
  async refreshCurrentProfile() {
    throw new Error("档案接口不可用");
  },
};

await refreshProfileSummary.call(errorContext);
assert.equal(errorContext.refreshingProfileSummary, false);
assert.equal(errorContext.profileSummaryError, "档案接口不可用");

console.log("frontend profile summary refresh regression: PASS");
