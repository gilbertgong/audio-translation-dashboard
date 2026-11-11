// Offline localization checks: node tests/test_i18n.cjs
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const staticDir = path.join(__dirname, "../src/static");

function fixture(languages = ["en-US"], saved = null, blocked = false) {
  const nodes = [];
  const events = {};
  const windowEvents = {};
  const writes = [];
  function node(attributes = {}) {
    const item = {
      attributes: { ...attributes }, textContent: "", value: "", dataset: {},
      setAttribute(name, value) { this.attributes[name] = value; },
      getAttribute(name) { return this.attributes[name]; },
      removeAttribute(name) { delete this.attributes[name]; },
      addEventListener(name, callback) { this[name] = callback; },
    };
    if (attributes["data-i18n"]) item.dataset.i18n = attributes["data-i18n"];
    nodes.push(item);
    return item;
  }
  const window = { addEventListener(name, fn) { windowEvents[name] = fn; }, dispatchEvent() {} };
  window.parent = window;
  const document = {
    documentElement: {},
    addEventListener(name, fn) { events[name] = fn; },
    getElementById(id) { return nodes.find((node) => node.attributes.id === id); },
    querySelectorAll(selector) {
      if (selector === "iframe.app-view-frame") return nodes.filter((node) => node.contentWindow);
      const name = /^\[([^\]]+)\]$/.exec(selector)[1];
      return nodes.filter((node) => name in node.attributes);
    },
  };
  const context = vm.createContext({ window, document, navigator: { languages, language: languages[0] }, Event,
    localStorage: {
      getItem() { if (blocked) throw new Error("blocked"); return saved; },
      setItem(key, value) { if (blocked) throw new Error("blocked"); writes.push([key, value]); },
    },
  });
  for (const file of ["i18n-zh.js", "i18n.js"]) vm.runInContext(fs.readFileSync(path.join(staticDir, file), "utf8"), context);
  return { ui: window.I18n, node, ready: () => events.DOMContentLoaded(), writes, window, windowEvents, document, context };
}

const f = fixture();
for (const [tag, expected] of Object.entries({
  en: "en", "fr-FR": "en", "zh": "zh-Hans", "zh-CN": "zh-Hans", "zh-SG": "zh-Hans",
  "zh-TW": "zh-Hant", "zh-HK": "zh-Hant", "zh-MO": "zh-Hant", "ZH_hant": "zh-Hant",
  "zh-Hant-CN": "zh-Hant", "zh-Hans-HK": "zh-Hans", "zho": "en",
})) assert.equal(f.ui.detect([tag]), expected, tag);
assert.equal(f.ui.detect(["en-US", "zh-CN"]), "en"); // first browser preference wins
assert.equal(f.ui.detect([]), "en");
assert.equal(fixture(["zh-TW"]).ui.language, "zh-Hans");
assert.equal(fixture(["zh-CN"], "en").ui.language, "en");
assert.equal(fixture(["en"], "zh-Hans").ui.language, "zh-Hans");
assert.equal(fixture(["en"], "invalid").ui.language, "en");
const blocked = fixture(["zh-HK"], null, true);
blocked.ready(); blocked.ui.change("en");
assert.equal(blocked.ui.language, "en");

const button = f.node({ "data-i18n": "Start Recording", "data-i18n-title": "Start/stop recording (Space)" });
const early = f.node({ "data-i18n": "Loading languages…" });
f.ui.text(early, "An early server response");
const toggle = f.node({ id: "ui-language" });
const speedLabel = f.node({ "data-i18n": "Language 1" });
const raw = f.node(); raw.textContent = "Stop"; // a valid interface key used as model output
const input = f.node(); input.value = "Keep this draft 原文";
const dynamic = f.node();
const stale = f.node();
const childChanges = [];
const frame = f.node(); frame.contentWindow = { I18n: { change: (...args) => childChanges.push(args) } };
f.ready();
f.ui.text(speedLabel, "english"); // config replaces the localized placeholder
f.ui.text(dynamic, f.ui.msg("{label} · {language}", { label: f.ui.msg("Speaking"), language: "English" }));
f.ui.text(stale, f.ui.msg("Translating…"));
f.ui.text(stale, "Translate"); // response replaces pending UI copy, even if it matches a key
f.ui.change("zh-Hans");
assert.equal(button.textContent, "开始录音");
assert.equal(early.textContent, "An early server response");
assert.equal(button.getAttribute("title"), "开始/停止录音（空格键）");
assert.equal(dynamic.textContent, "正在朗读 · English");
assert.equal(stale.textContent, "Translate");
assert.equal(raw.textContent, "Stop");
assert.equal(input.value, "Keep this draft 原文");
assert.equal(toggle.textContent, "EN");
assert.equal(speedLabel.textContent, "english");
assert.equal(f.document.documentElement.lang, "zh-Hans");
assert.deepEqual(f.writes.at(-1), ["uiLanguage", "zh-Hans"]);
assert.deepEqual(childChanges.at(-1), ["zh-Hans", false]);
f.ui.text(button, f.ui.msg("Stop Recording"));
f.ui.change("en");
assert.equal(button.textContent, "Stop Recording"); // static marker never restores an old state
assert.equal(f.ui.t("Missing English fallback"), "Missing English fallback");
assert.equal(f.ui.t("Error: {error}", { error: "{count}<b>原样</b>" }), "Error: {count}<b>原样</b>");
f.windowEvents.storage({ key: "uiLanguage", newValue: "zh-Hans" });
assert.equal(button.textContent, "停止录音");
const child = fixture(["en"], null, true);
child.window.parent = { I18n: f.ui };
child.ready();
assert.equal(child.ui.language, "zh-Hans"); // late-mounted views inherit even with storage blocked

const catalog = vm.runInContext("UI_ZH_HANS", f.context);
for (const [key, translated] of Object.entries(catalog)) {
  const placeholders = (value) => [...value.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();
  assert.deepEqual(placeholders(translated), placeholders(key), key);
}
for (const file of ["index.html", "translate.html", "test.html"]) {
  const source = fs.readFileSync(path.join(staticDir, file), "utf8");
  for (const match of source.matchAll(/data-i18n(?:-title|-aria-label|-placeholder)?="([^"]+)"/g)) {
    const key = match[1].replaceAll("&#x27;", "'").replaceAll("&quot;", '"').replaceAll("&amp;", "&");
    assert.ok(Object.hasOwn(catalog, key), `${file}: ${key}`);
  }
}
for (const [file, id] of [["index.html", "translation-window"], ["translate.html", "conversation"]]) {
  assert.match(fs.readFileSync(path.join(staticDir, file), "utf8"), new RegExp(`<section translate="no" class="notranslate" id="${id}"`));
}
console.log("Localization checks passed: locale detection/fallback, persistence, storage denial, live switching, mounted views, raw text/drafts, catalog placeholders and translation protection.");
