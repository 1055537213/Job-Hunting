import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// 直接从实际前端源文件提取方法，避免测试复制一份容易与页面脱节的实现。
const appPath = new URL(
  "../src/job_hunting_agent/web_static/app.js",
  import.meta.url,
);
const indexPath = new URL(
  "../src/job_hunting_agent/web_static/index.html",
  import.meta.url,
);
const source = readFileSync(appPath, "utf8");
const page = readFileSync(indexPath, "utf8");

function extractMethod(signature, functionSignature) {
  const start = source.indexOf(signature);
  const end = source.indexOf("      /**", start + 1);
  assert.notEqual(start, -1, `${signature} must exist`);
  assert.notEqual(end, -1, `a method comment must follow ${signature}`);

  const methodSource = source
    .slice(start, end)
    .trim()
    .replace(signature, functionSignature)
    .replace(/,\s*$/, "");
  return new Function(`return (${methodSource})`)();
}

assert.match(source, /const AUTH_ERROR_DISMISS_MS = 6000;/);
assert.equal((page.match(/@input="clearAuthFeedback"/g) || []).length, 2);
assert.match(page, /class="inline-error"\s+:class="\{ 'is-success': authSuccess \}"/);

const clearAuthFeedback = extractMethod(
  "clearAuthFeedback() {",
  "function clearAuthFeedback() {",
);
const showAuthError = extractMethod(
  "showAuthError(message) {",
  "function showAuthError(message) {",
);
const showAuthFeedback = extractMethod(
  "showAuthFeedback(message, success = false) {",
  "function showAuthFeedback(message, success = false) {",
);
const showAuthSuccess = extractMethod(
  "showAuthSuccess(message) {",
  "function showAuthSuccess(message) {",
);
const submitAuth = extractMethod(
  "async submitAuth() {",
  "async function submitAuth() {",
);

let scheduledCallback = null;
let scheduledDelay = null;
let cancelledTimer = null;
globalThis.window = {
  clearTimeout(timer) {
    cancelledTimer = timer;
  },
  setTimeout(callback, delay) {
    scheduledCallback = callback;
    scheduledDelay = delay;
    return "auth-error-timer";
  },
};
globalThis.AUTH_ERROR_DISMISS_MS = 6000;

const context = {
  authError: "",
  authErrorTimer: null,
  authForm: {
    email: "candidate@example.com",
    password: "wrong-password",
    displayName: "",
  },
  authLoading: false,
  authMode: "login",
  authSuccess: false,
  // 登录错误回归不覆盖重复内容弹窗；为真实提交方法提供最小 UI 上下文。
  showDuplicateNotice() {
    return false;
  },
  clearAuthFeedback,
  showAuthFeedback,
  showAuthError,
  showAuthSuccess,
  async requestJson() {
    throw new Error("邮箱或密码错误。");
  },
};

// 真实登录提交失败后必须先显示服务端错误。
await submitAuth.call(context);
assert.equal(context.authError, "邮箱或密码错误。");
assert.equal(context.authLoading, false);
assert.equal(context.authErrorTimer, "auth-error-timer");
assert.equal(scheduledDelay, 6000);

// 输入事件调用的清理方法应立即移除旧错误，而不是等下一次提交。
context.clearAuthFeedback();
assert.equal(context.authError, "");
assert.equal(context.authErrorTimer, null);
assert.equal(cancelledTimer, "auth-error-timer");

// 即使用户不继续输入，定时器到期后也必须自动清理错误。
context.showAuthError("邮箱或密码错误。");
scheduledCallback();
assert.equal(context.authError, "");
assert.equal(context.authErrorTimer, null);

// 注册成功必须走成功提示路径，并在同一个自动清理周期后消失。
scheduledCallback = null;
scheduledDelay = null;
const registerContext = {
  ...context,
  authMode: "register",
  authSuccess: false,
  authError: "",
  authErrorTimer: null,
  authPasswordVisible: true,
  authForm: {
    email: "new-candidate@example.com",
    password: "strong-password-123",
    displayName: "新候选人",
  },
  async requestJson() {
    return { account: null };
  },
};
await submitAuth.call(registerContext);
assert.equal(registerContext.authMode, "login");
assert.equal(registerContext.authSuccess, true);
assert.equal(registerContext.authError, "账号已创建，请登录。");
assert.equal(registerContext.authErrorTimer, "auth-error-timer");
assert.equal(scheduledDelay, 6000);
scheduledCallback();
assert.equal(registerContext.authError, "");
assert.equal(registerContext.authSuccess, false);
assert.equal(registerContext.authErrorTimer, null);

console.log("frontend auth error feedback regression: PASS");
