import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// 提取实际 app.js 中的流式方法，测试真实代码而不是复制一份实现。
const appPath = new URL(
  "../src/job_hunting_agent/web_static/app.js",
  import.meta.url,
);
const source = readFileSync(appPath, "utf8");
const start = source.indexOf("async streamChatReply(payload, assistantMessage, options = {})");
const end = source.indexOf("      /** 把较大的网络片段", start + 1);
assert.notEqual(start, -1, "streamChatReply must exist");
assert.notEqual(end, -1, "the next method comment must exist");

const methodSource = source
  .slice(start, end)
  .trim()
  .replace(
    /^async streamChatReply\(payload, assistantMessage, options = \{\}\) \{/,
    "async function streamChatReply(payload, assistantMessage, options = {}) {",
  )
  .replace(/,\s*$/, "");
const streamChatReply = new Function(`return (${methodSource})`)();

globalThis.window = {
  clearTimeout,
  requestAnimationFrame(callback) {
    return setTimeout(callback, 0);
  },
  setTimeout,
};

let readerCancelled = false;
globalThis.fetch = async (_url, options) => ({
  ok: true,
  body: {
    getReader() {
      return {
        read() {
          return new Promise((_resolve, reject) => {
            const rejectAborted = () => {
              const error = new Error("aborted");
              error.name = "AbortError";
              reject(error);
            };
            if (options.signal.aborted) {
              rejectAborted();
            } else {
              options.signal.addEventListener("abort", rejectAborted, { once: true });
            }
          });
        },
        cancel() {
          readerCancelled = true;
          return Promise.resolve();
        },
      };
    },
  },
});

const context = {
  buildChatReply() {
    return "";
  },
  consumeSseBuffer() {
    return { events: [], remaining: "" };
  },
  resolveStreamDrainWaiters() {},
  splitStreamDisplayChunks() {
    return [];
  },
  updateMessage() {},
};

await assert.rejects(
  () => streamChatReply.call(context, {}, {}, { timeoutMs: 10 }),
  (error) => error.name === "ChatStreamTimeoutError",
);
assert.equal(readerCancelled, true);
console.log("frontend stream timeout regression: PASS");
