import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const appSource = fs.readFileSync(
  path.join(repoRoot, "src", "job_hunting_agent", "web_static", "app.js"),
  "utf8",
);
const template = fs.readFileSync(
  path.join(repoRoot, "src", "job_hunting_agent", "web_static", "index.html"),
  "utf8",
);

assert.match(appSource, /profileDisplayNumber\(profileId\)/);
assert.match(appSource, /profile\.id === profileId/);
assert.doesNotMatch(appSource, /Number\(profile\.id\) === Number\(profileId\)/);
assert.match(appSource, /return index >= 0 \? index \+ 1 : "";/);
const computedStart = appSource.indexOf("computed:");
const methodsStart = appSource.indexOf("methods:");
const profileDisplayNumberStart = appSource.indexOf("profileDisplayNumber(profileId)");
assert.ok(computedStart >= 0 && methodsStart > computedStart);
assert.ok(profileDisplayNumberStart > methodsStart);
assert.doesNotMatch(
  appSource.slice(computedStart, methodsStart),
  /profileDisplayNumber\(profileId\)/,
);
assert.match(template, /:value="profile\.id"/);
assert.match(template, /#\{\{ profileDisplayNumber\(profile\.id\) \}\} \{\{ profile\.name \}\}/);
assert.doesNotMatch(template, /#\{\{ profile\.id \}\} \{\{ profile\.name \}\}/);

console.log("frontend profile display regression: PASS");
