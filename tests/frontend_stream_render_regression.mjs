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
assert.match(source, /我是求职助手 Agent/);
assert.match(source, /建立属于你的专属档案/);
assert.doesNotMatch(source, /默认通过标准 LangChain Agent 来处理你的聊天请求/);
assert.match(template, /class="task-trace"/);
assert.match(template, /@click="toggleTaskTrace\(message\)"/);
assert.match(source, /event\.event === "task_started"/);
assert.match(source, /event\.event === "step_completed"/);
assert.match(source, /setMessageTaskTrace\(assistantMessage, data\.task_trace/);

const extractObjectMethod = (signature, nextComment, replacement) => {
  const methodStart = source.indexOf(signature);
  const methodEnd = source.indexOf(nextComment, methodStart + 1);
  assert.notEqual(methodStart, -1, `${signature} must exist`);
  assert.notEqual(methodEnd, -1, `${signature} must have a following method comment`);
  const extracted = source
    .slice(methodStart, methodEnd)
    .trim()
    .replace(signature, replacement)
    .replace(/,\s*$/, "");
  return new Function(`return (${extracted})`)();
};

const renderInlineMarkdown = extractObjectMethod(
  "renderInlineMarkdown(value) {",
  "      /** 保存需要暂时跳过 Markdown 二次处理的 HTML 片段。 */",
  "function renderInlineMarkdown(value) {",
);
const stashRenderedToken = extractObjectMethod(
  "stashRenderedToken(tokens, html) {",
  "      /** 判断一行是否开启新的 Markdown 块。 */",
  "function stashRenderedToken(tokens, html) {",
);
const escapeHtml = extractObjectMethod(
  "escapeHtml(value) {",
  "      /** 读取 CSRF cookie，用于浏览器同源状态变更请求。 */",
  "function escapeHtml(value) {",
);
const markdownContext = { escapeHtml, stashRenderedToken };
const inlineCode = renderInlineMarkdown.call(
  markdownContext,
  "请调用 `ingest_candidate_message` 保存。",
);
assert.match(inlineCode, /<code>ingest_candidate_message<\/code>/);
assert.doesNotMatch(inlineCode, /MD_?TOKEN/);

console.log("frontend stream render regression: PASS");
