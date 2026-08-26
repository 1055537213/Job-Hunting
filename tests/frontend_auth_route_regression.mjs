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
const source = readFileSync(appPath, "utf8");
const template = readFileSync(indexPath, "utf8");

const FRONTEND_ROUTES = {
  auth: "/login",
  workspace: "/workspace",
  profile: "/profile",
  admin: "/admin",
};

function extractMethod(signature, functionSignature) {
  const start = source.indexOf(signature);
  const end = source.indexOf("      /**", start + 1);
  assert.notEqual(start, -1, `${signature} must exist`);
  assert.notEqual(end, -1, `${signature} must be followed by another method`);
  return new Function(
    "safeFrontendNextRoute",
    "FRONTEND_ROUTES",
    `return (${source
      .slice(start, end)
      .trim()
      .replace(signature, functionSignature)
      .replace(/,\s*$/, "")})`,
  )(safeFrontendNextRoute, FRONTEND_ROUTES);
}

function safeFrontendNextRoute(value) {
  const next = String(value || "").trim();
  if (!next || !next.startsWith("/") || next.startsWith("//")) return "";
  const pathname = next.split(/[?#]/, 1)[0].replace(/\/+$/, "") || "/";
  if (
    [
      FRONTEND_ROUTES.workspace,
      FRONTEND_ROUTES.profile,
      FRONTEND_ROUTES.admin,
      "/",
    ].includes(pathname)
  ) {
    return pathname === "/" ? FRONTEND_ROUTES.workspace : next;
  }
  return "";
}

assert.match(
  source,
  /navigateToAuth\(replace = false, preserveCurrentRoute = true\)/,
);
// 退出、退出全部设备、改密和注销都会失去当前 Session，必须回登录页且不能带 next。
assert.equal((source.match(/this\.navigateToAuth\(true, false\);/g) || []).length, 4);
assert.match(template, /app\.js\?v=20260825-project-collection-v3/);

const navigateToAuth = extractMethod(
  "navigateToAuth(replace = false, preserveCurrentRoute = true) {",
  "function navigateToAuth(replace = false, preserveCurrentRoute = true) {",
);
const navigateAfterAuth = extractMethod(
  "navigateAfterAuth(replace = false) {",
  "function navigateAfterAuth(replace = false) {",
);

const navigations = [];
globalThis.window = {
  location: {
    pathname: "/profile",
    search: "",
    replace(target) {
      navigations.push({ method: "replace", target });
    },
    assign(target) {
      navigations.push({ method: "assign", target });
    },
  },
};

navigateToAuth.call({}, true);
assert.deepEqual(navigations.pop(), {
  method: "replace",
  target: "/login?next=%2Fprofile",
});

navigateToAuth.call({}, true, false);
assert.deepEqual(navigations.pop(), {
  method: "replace",
  target: "/login",
});

let afterAuthTarget = null;
navigateAfterAuth.call({
  navigateTo(target, replace) {
    afterAuthTarget = { target, replace };
  },
}, true);
assert.deepEqual(afterAuthTarget, { target: "/workspace", replace: true });

window.location.search = "?next=%2Fprofile";
navigateAfterAuth.call({
  navigateTo(target, replace) {
    afterAuthTarget = { target, replace };
  },
}, true);
assert.deepEqual(afterAuthTarget, { target: "/profile", replace: true });

console.log("frontend auth route regression: PASS");
