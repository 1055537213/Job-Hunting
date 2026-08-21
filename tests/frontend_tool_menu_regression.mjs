import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const indexPath = new URL(
  "../src/job_hunting_agent/web_static/index.html",
  import.meta.url,
);
const stylesPath = new URL(
  "../src/job_hunting_agent/web_static/styles.css",
  import.meta.url,
);
const template = readFileSync(indexPath, "utf8");
const styles = readFileSync(stylesPath, "utf8");

const triggerRule = styles.match(/\.tool-panel-trigger\s*\{([\s\S]*?)\n\}/);
assert.ok(triggerRule, "tool menu trigger rule must exist");
assert.match(
  triggerRule[1],
  /min-height:\s*calc\(var\(--size-hit\) \+ var\(--space-md\)\);/,
  "tool menu triggers must keep a comfortable, consistent hit area",
);
assert.match(
  triggerRule[1],
  /grid-template-columns:\s*minmax\(0, 1fr\) max-content var\(--space-lg\);/,
  "tool menu triggers must reserve a right-aligned arrow column",
);

const chevronRule = styles.match(/\.tool-panel-chevron\s*\{([\s\S]*?)\n\}/);
assert.ok(chevronRule, "tool menu chevron rule must exist");
assert.match(chevronRule[1], /grid-column:\s*3;/);
assert.match(chevronRule[1], /width:\s*var\(--space-lg\);/);
assert.match(chevronRule[1], /height:\s*var\(--space-lg\);/);
assert.match(chevronRule[1], /place-items:\s*center;/);
assert.match(chevronRule[1], /font-size:\s*0;/);
assert.match(chevronRule[1], /transform-origin:\s*center;/);
assert.doesNotMatch(chevronRule[1], /inset-block-start/);
assert.match(
  styles,
  /\.tool-panel-chevron svg\s*\{[\s\S]*?width:\s*var\(--space-sm\);[\s\S]*?height:\s*var\(--space-sm\);/,
  "the arrow graphic must have a stable centered box",
);
assert.match(
  styles,
  /\.tool-panel-chevron path\s*\{[\s\S]*?stroke:\s*currentColor;[\s\S]*?stroke-linecap:\s*round;/,
  "the arrow graphic must use a consistent stroked shape",
);

assert.match(
  styles,
  /\.tool-panel-trigger:hover\s*\{[\s\S]*?background:\s*var\(--color-paper-2\);/,
  "hover feedback must be color based",
);
const hoverRules = [...styles.matchAll(/\.tool-panel-trigger:hover\s*\{([^}]*)\}/g)].map(
  (match) => match[1],
);
assert.ok(hoverRules.length > 0, "tool menu hover rule must exist");
assert.doesNotMatch(
  hoverRules.join("\n"),
  /transform:\s*translateY/,
  "hovering a tool menu trigger must not cover its divider",
);
const activeRules = [...styles.matchAll(/\.tool-panel-trigger:active\s*\{([^}]*)\}/g)].map(
  (match) => match[1],
);
assert.ok(activeRules.length > 0, "tool menu active rule must exist");
assert.doesNotMatch(
  activeRules.join("\n"),
  /transform:\s*translateY/,
  "pressing a tool menu trigger must not cover its divider",
);
const bodyRule = styles.match(/\.tool-panel-body\s*\{([\s\S]*?)\n\}/);
assert.ok(bodyRule, "tool panel body rule must exist");
assert.match(
  bodyRule[1],
  /padding:\s*var\(--space-sm\)\s+var\(--space-md\)\s+var\(--space-md\);/,
  "expanded tool content must keep space below the section divider",
);
assert.equal((template.match(/class="tool-panel-chevron"/g) || []).length, 6);
assert.equal(
  (template.match(/class="tool-panel-chevron"[^>]*>\s*<svg viewBox="0 0 24 24" focusable="false"><path d="m6 9 6 6 6-6"><\/path><\/svg>/g) || []).length,
  6,
);
assert.doesNotMatch(template, /class="tool-panel-chevron"[^>]*>⌄/);
assert.match(template, /styles\.css\?v=20260821-project-delete-v1/);
assert.match(template, /app\.js\?v=20260821-project-delete-v1/);

console.log("frontend tool menu regression: PASS");
