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
const template = readFileSync(indexPath, "utf8");
const styles = readFileSync(stylesPath, "utf8");

function extractMethod(signature, functionSignature) {
  const start = source.indexOf(signature);
  const end = source.indexOf("      /**", start + 1);
  assert.notEqual(start, -1, `${signature} must exist`);
  assert.notEqual(end, -1, `${signature} must be followed by another method`);

  const methodSource = source
    .slice(start, end)
    .trim()
    .replace(signature, functionSignature)
    .replace(/,\s*$/, "");
  return new Function(`return (${methodSource})`)();
}

globalThis.normalizeCityName = (value) => {
  const suffixes = ["特别行政区", "自治州", "地区", "盟", "市", "县"];
  let city = String(value || "").trim();
  const suffix = suffixes.find((item) => city.endsWith(item) && city.length > item.length);
  if (suffix) city = city.slice(0, -suffix.length);
  return city;
};

assert.match(template, /class="city-picker"/);
assert.match(template, /v-if="cityPickerOpen"/);
assert.match(template, /热门城市/);
assert.match(template, /省份及直辖市/);
assert.match(template, /v-for="city in activeCityOptions"/);
assert.doesNotMatch(template, /<optgroup/);
assert.match(template, /class="city-picker-trigger-chevron"[\s\S]*?<svg viewBox="0 0 24 24"/);
assert.doesNotMatch(template, /class="city-picker-trigger-chevron"[^>]*>⌄/);
assert.match(template, /class="city-picker-menu"[\s\S]*?@wheel="handleCityPickerWheel"/);
assert.match(template, /styles\.css\?v=20260823-cleanup-v1/);
assert.match(template, /app\.js\?v=20260823-project-collapse-v1/);
assert.match(source, /HOT_CITY_NAMES/);
assert.match(source, /handleCityPickerWheel\(event\)/);
assert.match(source, /event\.preventDefault\(\);\s*event\.stopPropagation\(\);/);
assert.match(styles, /\.city-picker-menu/);
assert.match(styles, /\.city-picker-menu\s*\{[\s\S]*?overscroll-behavior:\s*contain;/);
assert.match(styles, /\.city-picker-primary\s*\{[\s\S]*?overscroll-behavior:\s*contain;/);
assert.match(styles, /\.city-picker-city-list\s*\{[\s\S]*?overscroll-behavior:\s*contain;/);
assert.match(styles, /\.city-picker-trigger-chevron\s*\{[\s\S]*?place-items:\s*center;/);
assert.match(styles, /\.city-picker-trigger-chevron path\s*\{[\s\S]*?stroke:\s*currentColor;/);
assert.match(styles, /grid-template-columns: minmax\(0, 1fr\);/);

const addPreferredCity = extractMethod(
  'addPreferredCity(cityValue = "") {',
  'function addPreferredCity(cityValue = "") {',
);
const isPreferredCity = extractMethod(
  "isPreferredCity(cityValue) {",
  "function isPreferredCity(cityValue) {",
);
const togglePreferredCity = extractMethod(
  "togglePreferredCity(cityValue) {",
  "function togglePreferredCity(cityValue) {",
);
const removePreferredCity = extractMethod(
  "removePreferredCity(city) {",
  "function removePreferredCity(city) {",
);
const selectCityProvince = extractMethod(
  "selectCityProvince(province) {",
  "function selectCityProvince(province) {",
);
const handleCityPickerWheel = extractMethod(
  "handleCityPickerWheel(event) {",
  "function handleCityPickerWheel(event) {",
);

const context = {
  profileForm: { preferredCities: [] },
  addPreferredCity,
  isPreferredCity,
  removePreferredCity,
};

togglePreferredCity.call(context, "杭州市");
assert.deepEqual(context.profileForm.preferredCities, ["杭州"]);
assert.equal(isPreferredCity.call(context, "杭州市"), true);

togglePreferredCity.call(context, "杭州");
assert.deepEqual(context.profileForm.preferredCities, []);

selectCityProvince.call(context, "广东省");
assert.equal(context.activeCityProvince, "广东省");

const primaryScroller = { clientHeight: 180, scrollTop: 12 };
const cityScroller = { clientHeight: 180, scrollTop: 24 };
const cityMenu = {
  querySelector(selector) {
    if (selector === ".city-picker-primary") return primaryScroller;
    if (selector === ".city-picker-city-list") return cityScroller;
    return null;
  },
};
const cityWheelEvent = {
  currentTarget: cityMenu,
  target: {
    closest(selector) {
      return selector === ".city-picker-city-list" ? {} : null;
    },
  },
  deltaY: 36,
  deltaMode: 0,
  prevented: false,
  stopped: false,
  preventDefault() {
    this.prevented = true;
  },
  stopPropagation() {
    this.stopped = true;
  },
};

handleCityPickerWheel.call({}, cityWheelEvent);
assert.equal(cityWheelEvent.prevented, true);
assert.equal(cityWheelEvent.stopped, true);
assert.equal(cityScroller.scrollTop, 60);
assert.equal(primaryScroller.scrollTop, 12);

console.log("frontend city picker regression: PASS");
