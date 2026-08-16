import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// 从实际前端源码中提取全局快捷键处理器，避免测试复制一份实现。
const appPath = new URL(
  "../src/job_hunting_agent/web_static/app.js",
  import.meta.url,
);
const source = readFileSync(appPath, "utf8");
const start = source.indexOf("handleGlobalShortcut(event) {");
const end = source.indexOf("      /**", start + 1);

assert.notEqual(start, -1, "handleGlobalShortcut must exist");
assert.notEqual(end, -1, "the next method comment must exist");

const methodSource = source
  .slice(start, end)
  .trim()
  .replace(/^handleGlobalShortcut\(event\) \{/, "function handleGlobalShortcut(event) {")
  .replace(/,\s*$/, "");
const handleGlobalShortcut = new Function(`return (${methodSource})`)();

// 扩展或自动化脚本可能派发没有 key 字段的事件；页面不应因此抛异常。
assert.doesNotThrow(() => {
  handleGlobalShortcut.call({ commandPaletteOpen: false }, { ctrlKey: false, metaKey: false });
});

// 标准 Ctrl/Cmd+K 行为仍需保留。
let opened = false;
let prevented = false;
handleGlobalShortcut.call(
  {
    commandPaletteOpen: false,
    duplicateNotice: { open: false },
    jobImportNotice: { open: false },
    openCommandPalette() {
      opened = true;
    },
  },
  {
    key: "k",
    ctrlKey: true,
    metaKey: false,
    preventDefault() {
      prevented = true;
    },
  },
);
assert.equal(opened, true);
assert.equal(prevented, true);

// 截图审核弹窗打开时，Esc 应优先关闭弹窗，不应继续触发工作台快捷键。
let screenshotNoticeClosed = false;
handleGlobalShortcut.call(
  {
    duplicateNotice: { open: false },
    jobImportNotice: { open: true },
    closeJobImportNotice() {
      screenshotNoticeClosed = true;
    },
  },
  {
    key: "Escape",
    preventDefault() {},
  },
);
assert.equal(screenshotNoticeClosed, true);

console.log("frontend shortcut regression: PASS");
