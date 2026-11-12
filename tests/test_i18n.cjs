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
      children: [], classList: { toggle() {}, add() {} },
      append(...children) { this.children.push(...children); },
      replaceChildren(...children) { this.children = children; },
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
    createElement() { return node(); },
    querySelectorAll(selector) {
      if (selector === "iframe.app-view-frame") return nodes.filter((node) => node.contentWindow);
      const match = /^\[([^\]]+)\]$/.exec(selector);
      if (!match) return [];
      const name = match[1];
      return nodes.filter((node) => name in node.attributes);
    },
  };
  const context = vm.createContext({ window, document, navigator: { languages, language: languages[0] }, Event,
    AbortController, TextDecoder, fetch() { return new Promise(() => {}); },
    localStorage: {
      getItem() { if (blocked) throw new Error("blocked"); return saved; },
      setItem(key, value) { if (blocked) throw new Error("blocked"); writes.push([key, value]); },
    },
  });
  for (const file of ["i18n-zh.js", "i18n-zh-hant.js", "i18n.js"]) vm.runInContext(fs.readFileSync(path.join(staticDir, file), "utf8"), context);
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
assert.equal(fixture(["zh-TW"]).ui.language, "zh-Hant");
assert.equal(fixture(["zh-CN"], "en").ui.language, "en");
assert.equal(fixture(["en"], "zh-Hans").ui.language, "zh-Hans");
assert.equal(fixture(["en"], "invalid").ui.language, "en");
const blocked = fixture(["zh-HK"], null, true);
blocked.ready(); blocked.ui.change("en");
assert.equal(blocked.ui.language, "en");

// Still only EN/中文: Chinese script is automatic, including after a saved English override.
for (const [locale, chinese] of [["zh-TW", "zh-Hant"], ["zh-HK", "zh-Hant"], ["zh-MO", "zh-Hant"],
  ["zh-Hant-CN", "zh-Hant"], ["zh-Hans-HK", "zh-Hans"], ["zh-CN", "zh-Hans"], ["en-US", "zh-Hans"], ["fr", "zh-Hans"]]) {
  const view = fixture([locale], "en");
  const toggle = view.node({ id: "ui-language" });
  const label = view.node({ "data-i18n": "Capture Audio" });
  const literal = view.node(); literal.textContent = "Capture Audio";
  view.ready();
  assert.equal(view.ui.language, "en");
  assert.equal(toggle.textContent, "中文");
  assert.equal(toggle.lang, chinese);
  for (let i = 0; i < 2; i++) {
    toggle.click();
    assert.equal(view.ui.language, chinese, locale);
    assert.equal(toggle.textContent, "EN");
    assert.equal(label.textContent, chinese === "zh-Hant" ? "擷取音訊" : "捕获音频");
    assert.equal(literal.textContent, "Capture Audio");
    assert.equal(view.document.documentElement.lang, chinese);
    assert.equal(fixture([locale], view.writes.at(-1)[1]).ui.language, chinese);
    toggle.click();
    assert.equal(view.ui.language, "en");
  }
}
assert.equal(fixture(["zh-TW"], "zh-Hans").ui.language, "zh-Hant"); // existing Chinese preference
assert.equal(fixture(["en"], "zh-Hant").ui.language, "zh-Hans"); // new browser chooses its variant
const traditional = fixture(["zh-TW"], null, true);
const traditionalToggle = traditional.node({ id: "ui-language" });
traditional.ready(); traditionalToggle.click(); traditionalToggle.click();
assert.equal(traditional.ui.language, "zh-Hant");
traditional.windowEvents.storage({ key: "uiLanguage", newValue: "en" });
assert.equal(traditional.ui.language, "en");
traditional.windowEvents.storage({ key: "uiLanguage", newValue: "zh-Hans" });
assert.equal(traditional.ui.language, "zh-Hant");
traditional.windowEvents.storage({ key: "uiLanguage", newValue: null });
assert.equal(traditional.ui.language, "zh-Hant");
const traditionalChild = fixture(["zh-TW"], "en", true);
traditionalChild.window.parent = { I18n: traditional.ui };
traditionalChild.ready();
assert.equal(traditionalChild.ui.language, "zh-Hant");

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
const traditionalCatalog = vm.runInContext("UI_ZH_HANT", f.context);
assert.deepEqual(Object.keys(traditionalCatalog).sort(), Object.keys(catalog).sort());
for (const [key, translated] of [...Object.entries(catalog), ...Object.entries(traditionalCatalog)]) {
  const placeholders = (value) => [...value.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();
  assert.deepEqual(placeholders(translated), placeholders(key), key);
}
for (const file of ["index.html", "translate.html", "test.html"]) {
  const source = fs.readFileSync(path.join(staticDir, file), "utf8");
  for (const match of source.matchAll(/data-i18n(?:-title|-aria-label|-placeholder|-rich)?="([^"]+)"/g)) {
    const key = match[1].replaceAll("&#x27;", "'").replaceAll("&quot;", '"').replaceAll("&amp;", "&");
    assert.ok(Object.hasOwn(catalog, key), `${file}: ${key}`);
    assert.ok(Object.hasOwn(traditionalCatalog, key), `${file}: Traditional ${key}`);
  }
  assert.ok(source.indexOf('src="i18n-zh-hant.js"') < source.indexOf('src="i18n.js"'));
}
for (const [file, id] of [["index.html", "translation-window"], ["translate.html", "conversation"]]) {
  assert.match(fs.readFileSync(path.join(staticDir, file), "utf8"), new RegExp(`<section translate="no" class="notranslate" id="${id}"`));
}
// Rich messages reuse authored emphasis nodes while the catalog controls word order.
const rich = fixture();
const windowSlot = rich.node({ "data-i18n": "window" });
windowSlot.dataset.i18nSlot = "window";
const help = rich.node({ "data-i18n-rich": "A {window} shares no sound in most browsers." });
help.dataset.i18nRich = help.getAttribute("data-i18n-rich");
help.querySelectorAll = () => [windowSlot];
rich.document.createTextNode = (textContent) => ({ textContent });
rich.ready();
assert.equal(help.children.map((n) => n.textContent).join(""), "A window shares no sound in most browsers.");
rich.ui.change("zh-Hans");
assert.equal(help.children.map((n) => n.textContent).join(""), "在大多数浏览器中，窗口不共享声音。");
assert.ok(help.children.includes(windowSlot));
rich.ui.change("en");
assert.equal(help.children.map((n) => n.textContent).join(""), "A window shares no sound in most browsers.");
console.log("Localization checks passed: locale detection/fallback, persistence, storage denial, live switching, mounted views, raw text/drafts, catalog placeholders and translation protection.");

// Exercise the actual text-view failure paths, not just message construction.
async function errorChecks() {
  const f = fixture();
  for (const id of ["source-text", "swap", "clear", "messages", "translate", "conversation", "translation-status"]) f.node({ id });
  f.ready();
  vm.runInContext(fs.readFileSync(path.join(staticDir, "translate.js"), "utf8"), f.context);
  function response(stream) {
    let done = false;
    return { ok: true, body: { getReader: () => ({
      async read() { if (done) return { done: true }; done = true; return { value: new TextEncoder().encode(stream), done: false }; },
      async cancel() {}, releaseLock() {},
    }) } };
  }
  for (const [stream, key] of [
    ["data: [DONE]\n\n", "No translation returned. Please try again."],
    ["", "Translation was interrupted. Please try again."],
  ]) {
    f.ui.change("en");
    const message = { row: f.node(), text: f.node(), actions: f.node(), original: "original", source: "English" };
    f.context.testMessage = message;
    f.context.fetch = async () => response(stream);
    await vm.runInContext("runTranslation(testMessage)", f.context);
    const status = f.document.getElementById("translation-status");
    assert.equal(status.textContent, key);
    f.ui.change("zh-Hans");
    assert.equal(status.textContent, f.ui.t(key));
    assert.equal(message.actions.children[0].textContent, f.ui.t(key));
    f.ui.change("en");
    assert.equal(status.textContent, key);
  }
  const nested = f.node();
  const speechError = f.ui.error("no audio received");
  f.ui.text(nested, f.ui.msg("Speech error: {error}", { error: f.ui.errorText(speechError) }));
  const external = f.node();
  f.ui.text(external, f.ui.errorText(new Error("No translation returned. Please try again.")));
  f.ui.change("zh-Hans");
  assert.equal(nested.textContent, "语音错误：未收到音频");
  assert.equal(external.textContent, "No translation returned. Please try again.");
  const fallback = await vm.runInContext("responseError({status: 503, json: async () => ({})})", f.context);
  f.ui.text(nested, f.ui.errorText(fallback));
  assert.equal(nested.textContent, "请求失败（503）");
  f.ui.change("en");
  assert.equal(nested.textContent, "Request failed (503)");
  console.log("Error localization checks passed: actual empty/interrupted translation paths, nested speech errors, HTTP fallback and literal external errors.");
}
errorChecks().catch((err) => { console.error(err); process.exitCode = 1; });
