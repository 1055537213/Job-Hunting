import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// 直接提取实际页面方法，确保截图审核失败时的弹窗行为不会被后续改动悄悄移除。
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

assert.match(page, /v-if="jobImportNotice\.open"/);
assert.match(page, /role="alertdialog"/);
assert.match(page, /ref="jobImportNoticeClose"/);
assert.match(source, /job-import-dialog-lock/);

const showJobImportNotice = extractMethod(
  "showJobImportNotice(message, title = \"无法导入职位截图\") {",
  "function showJobImportNotice(message, title = \"无法导入职位截图\") {",
);
const closeJobImportNotice = extractMethod(
  "closeJobImportNotice() {",
  "function closeJobImportNotice() {",
);
const importJobScreenshots = extractMethod(
  "async importJobScreenshots() {",
  "async function importJobScreenshots() {",
);

class FakeElement {
  focus() {}
}

const bodyClasses = new Set();
globalThis.HTMLElement = FakeElement;
globalThis.document = {
  activeElement: new FakeElement(),
  body: {
    classList: {
      add(name) {
        bodyClasses.add(name);
      },
      remove(name) {
        bodyClasses.delete(name);
      },
    },
  },
};
globalThis.nextTick = (callback) => callback();
globalThis.FormData = class {
  append() {}
};

const rejection = new Error(
  "职位截图信息不完整，未保存任何职位信息。请上传更完整的职位截图。",
);
rejection.status = 400;

const context = {
  jobForm: {
    screenshots: [{ name: "partial-job.png" }],
    sourceUrl: "",
  },
  jobImportError: "",
  jobImportNotice: {
    open: false,
    title: "",
    message: "",
  },
  jobImportNoticeReturnTarget: null,
  importingJob: false,
  $refs: {
    jobImportNoticeClose: new FakeElement(),
  },
  openWorkspacePanel() {},
  showDuplicateNotice() {
    return false;
  },
  showJobImportNotice,
  closeJobImportNotice,
  appendAssistant() {
    throw new Error("400 截图审核错误应优先显示弹窗，而不是写入聊天记录。");
  },
  async requestFormJson() {
    throw rejection;
  },
  clearJobScreenshotSelection() {},
  async loadJobs() {},
  async matchJobs() {},
};

await importJobScreenshots.call(context);
assert.equal(context.importingJob, false);
assert.equal(context.jobImportError, rejection.message);
assert.equal(context.jobImportNotice.open, true);
assert.equal(context.jobImportNotice.title, "无法导入职位截图");
assert.equal(context.jobImportNotice.message, rejection.message);
assert.equal(bodyClasses.has("job-import-dialog-lock"), true);

closeJobImportNotice.call(context);
assert.equal(context.jobImportNotice.open, false);
assert.equal(bodyClasses.has("job-import-dialog-lock"), false);

console.log("frontend job screenshot notice regression: PASS");
