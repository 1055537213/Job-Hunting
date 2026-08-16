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

// Streaming updates must only update plain text. Re-parsing the growing full
// Markdown document for every token creates increasingly expensive main-thread work.
const start = source.indexOf(
  "updateMessage(message, text, isError = false, isStreaming = message.isStreaming)",
);
const end = source.indexOf("      /** 让聊天窗口自动滚动到最新消息。 */", start + 1);
assert.notEqual(start, -1, "updateMessage must track whether a message is streaming");
assert.notEqual(end, -1, "the next method comment must exist");

const methodSource = source
  .slice(start, end)
  .trim()
  .replace(
    /^updateMessage\(message, text, isError = false, isStreaming = message\.isStreaming\) \{/,
    "function updateMessage(message, text, isError = false, isStreaming = message.isStreaming) {",
  )
  .replace(/,\s*$/, "");
const updateMessage = new Function(`return (${methodSource})`)();

let markdownRenderCount = 0;
let scrollCount = 0;
const context = {
  renderMarkdown(text) {
    markdownRenderCount += 1;
    return `<p>${text}</p>`;
  },
  scrollMessages() {
    scrollCount += 1;
  },
};
const message = {
  content: "",
  isError: false,
  isStreaming: true,
  renderedHtml: "",
};

for (let index = 0; index < 400; index += 1) {
  updateMessage.call(context, message, `token ${index}`, false, true);
}
assert.equal(markdownRenderCount, 0, "streaming tokens must not render Markdown");
assert.equal(message.isStreaming, true, "message remains in streaming mode");

updateMessage.call(context, message, "# Final reply", false, false);
assert.equal(markdownRenderCount, 1, "final reply renders Markdown once");
assert.equal(message.isStreaming, false, "final reply exits streaming mode");
assert.equal(message.renderedHtml, "<p># Final reply</p>");
assert.equal(scrollCount, 401, "every visible update still scrolls to the latest message");

assert.match(template, /v-if="message\.isStreaming"/);
assert.match(template, /v-html="message\.renderedHtml"/);
assert.doesNotMatch(template, /v-html="renderMarkdown\(message\.content\)"/);

console.log("frontend stream render regression: PASS");
