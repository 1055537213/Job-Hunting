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
const page = readFileSync(indexPath, "utf8");
const styles = readFileSync(stylesPath, "utf8");

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

assert.match(page, /class="project-review-choice is-accept"/);
assert.match(page, /@click="setProjectReviewDecision\(record, item, 'accepted'\)"/);
assert.match(page, /@click="setProjectReviewDecision\(record, item, 'rejected'\)"/);
assert.match(page, /按组确认内容/);
assert.match(page, /保存已确认内容/);
assert.match(page, /message\.taskTrace\.approval\.kind !== 'project_card_confirmation'/);
assert.match(source, /captureProjectTasksFromChat\(data\.background_tasks \|\| \[\]\)/);
assert.doesNotMatch(source, /captureProjectTasksFromChat\(data\.tool_outputs \|\| \[\]\)/);
assert.doesNotMatch(
  page,
  /class="ghost-button compact"[\s\S]{0,500}@click="confirmProjectCard\(record\)"/,
);
assert.match(styles, /\.project-source-link\s*\{[^}]*color: var\(--color-accent\);/);
assert.doesNotMatch(
  styles,
  /\.project-source-link\s*\{[^}]*color: var\(--color-accent-ink\);/,
);

const projectReviewItems = extractMethod(
  "projectReviewItems(record) {",
  "function projectReviewItems(record) {",
);
const projectReviewStatus = extractMethod(
  "projectReviewStatus(record, item) {",
  "function projectReviewStatus(record, item) {",
);
const setProjectReviewDecision = extractMethod(
  "setProjectReviewDecision(record, item, status) {",
  "function setProjectReviewDecision(record, item, status) {",
);
const projectReviewAcceptedCount = extractMethod(
  "projectReviewAcceptedCount(record) {",
  "function projectReviewAcceptedCount(record) {",
);
const projectConfirmedSummary = extractMethod(
  "projectConfirmedSummary(record) {",
  "function projectConfirmedSummary(record) {",
);
const confirmProjectCard = extractMethod(
  "async confirmProjectCard(record) {",
  "async function confirmProjectCard(record) {",
);
const normalizeTaskTrace = extractMethod(
  "normalizeTaskTrace(trace, options = {}) {",
  "function normalizeTaskTrace(trace, options = {}) {",
);
const sanitizeUserVisibleChatContent = extractMethod(
  "sanitizeUserVisibleChatContent(value) {",
  "function sanitizeUserVisibleChatContent(value) {",
);

const normalizedTrace = normalizeTaskTrace.call({}, {
  root_request_id: "0123456789abcdef0123456789abcdef",
  status: "waiting_confirmation",
  steps: [],
});
assert.equal(
  normalizedTrace.root_request_id,
  "0123456789abcdef0123456789abcdef",
);
assert.equal(
  sanitizeUserVisibleChatContent.call(
    {},
    "分析如下：\n匹配结果：该岗位与候选人的经历相关。",
  ),
  "分析如下：\n匹配结果：该岗位与候选人的经历相关。",
);
assert.equal(
  sanitizeUserVisibleChatContent.call(
    {},
    "工具：ingest_candidate_message\n保存字段：skills\n长文本 ID：28",
  ),
  "",
);

const record = {
  id: 12,
  status: "待确认",
  card: {
    project_name: "candidate-agent",
    detected_tech_stack: ["Python", "FastAPI", "JavaScript", "Vue", "LangChain", "LangGraph", "RAG"],
    detected_core_features: ["职位匹配"],
    responsibility_draft: ["负责接口设计"],
    highlight_draft: ["完成后端服务拆分"],
    questions_for_candidate: ["项目中你负责的模块是什么？"],
  },
};

const context = {
  projectReviewSelections: {},
  projectReviewItems,
  projectReviewStatus,
};
const items = projectReviewItems.call(context, record);
assert.equal(items.length, 6);
const backendGroup = items.find((item) => item.label === "后端/API 技术栈");
const frontendGroup = items.find((item) => item.label === "前端技术栈");
const aiGroup = items.find((item) => item.label === "AI/Agent 技术栈");
const highlightGroup = items.find((item) => item.label === "项目亮点");
assert.equal(backendGroup?.value, "Python、FastAPI");
assert.equal(frontendGroup?.value, "JavaScript、Vue");
assert.equal(aiGroup?.value, "LangChain、LangGraph、RAG");
assert.equal(highlightGroup?.value, "完成后端服务拆分");
assert.equal(projectReviewStatus.call(context, record, items[0]), "pending");

setProjectReviewDecision.call(context, record, backendGroup, "accepted");
setProjectReviewDecision.call(context, record, frontendGroup, "rejected");
setProjectReviewDecision.call(context, record, highlightGroup, "accepted");
assert.equal(projectReviewStatus.call(context, record, backendGroup), "accepted");
assert.equal(projectReviewStatus.call(context, record, frontendGroup), "rejected");
assert.equal(projectReviewAcceptedCount.call(context, record), 2);

const selectedSummary = projectConfirmedSummary.call(context, record);
assert.match(selectedSummary, /后端\/API 技术栈：Python、FastAPI/);
assert.match(selectedSummary, /项目亮点：完成后端服务拆分/);
assert.doesNotMatch(selectedSummary, /前端技术栈：JavaScript、Vue/);

let requestOptions = null;
const savedMessages = [];
let approvalReconciliations = 0;
const confirmContext = {
  ...context,
  projectConfirmedSummary,
  projectCards: [record],
  confirmingProjectCardId: 0,
  githubProjectError: "",
  currentProfileId: 3,
  backgroundTasks: {},
  messages: [
    {
      taskTrace: {
        root_request_id: "0123456789abcdef0123456789abcdef",
        approval: { kind: "project_card_confirmation", record_id: 12 },
      },
    },
  ],
  appendAssistant(message) {
    savedMessages.push(message);
  },
  requestJson(_url, options) {
    requestOptions = options;
    return Promise.resolve({
      project_card: { ...record, status: "已确认" },
      task: null,
    });
  },
  openWorkspacePanel() {},
  rememberRagTask() {},
  pollBackgroundTask() {},
  reconcileTaskApprovals() {
    approvalReconciliations += 1;
  },
};

await confirmProjectCard.call(confirmContext, record);
const submittedPayload = JSON.parse(requestOptions.body);
const submitted = submittedPayload.confirmed_summary;
assert.match(submitted, /Python/);
assert.match(submitted, /FastAPI/);
assert.doesNotMatch(submitted, /前端技术栈：JavaScript、Vue/);
assert.equal(
  submittedPayload.root_request_id,
  "0123456789abcdef0123456789abcdef",
);
assert.equal(confirmContext.projectReviewSelections["12"], undefined);
assert.equal(approvalReconciliations, 1);
assert.match(savedMessages[0], /只有你确认的内容/);

console.log("frontend project review regression: PASS");
