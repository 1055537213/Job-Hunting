import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const source = readFileSync(
  new URL("../src/job_hunting_agent/web_static/app.js", import.meta.url),
  "utf8",
);
const page = readFileSync(
  new URL("../src/job_hunting_agent/web_static/index.html", import.meta.url),
  "utf8",
);
const styles = readFileSync(
  new URL("../src/job_hunting_agent/web_static/styles.css", import.meta.url),
  "utf8",
);

assert.match(page, /<span class="tool-panel-trigger-title">项目导入<\/span>/);
assert.match(page, /projectImportMode === 'github'/);
assert.match(page, /projectImportMode === 'local'/);
assert.match(page, /webkitdirectory/);
assert.match(page, /选择并分析目录/);
assert.match(page, /取消本次采集/);
assert.doesNotMatch(page, /保留选中文件原件/);
assert.match(page, /projectFileKindSummary\(record\.card\)/);
assert.match(source, /async selectAndAnalyzeLocalProject\(\) \{/);
assert.match(source, /window\.showDirectoryPicker/);
assert.match(source, /crypto\.subtle\.digest\("SHA-256"/);
assert.match(source, /\/api\/projects\/local\/manifest/);
assert.match(source, /pendingLocalProjectCollection/);
assert.match(source, /resumePendingLocalProjectCollection/);
assert.match(source, /cancelLocalProjectCollection/);
assert.match(source, /method: "DELETE"/);
assert.match(source, /form\.append\("files", file, file\.name\)/);
assert.match(source, /batchResult\.visual_task/);
assert.match(source, /task\.result\?\.visual_task_key/);
assert.match(source, /task_type: "visual_index"/);
assert.match(source, /isBlockedLocalProjectPath\(relativePath\)/);
assert.doesNotMatch(source, /preserve_originals|preserveProjectOriginals/);
assert.doesNotMatch(source, /\/api\/projects\/archive/);
assert.match(styles, /\.project-import-mode\s*\{/);
assert.match(styles, /\.project-import-mode-button\.is-active/);
assert.match(styles, /\.project-collection-status\s*\{/);
