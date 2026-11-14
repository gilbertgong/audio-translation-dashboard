// Offline shared-header behavior: node tests/test_header.cjs
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const staticDir = path.join(__dirname, "../src/static");
const flush = () => new Promise(setImmediate);

function fixture(page, custom, hash = "", engineState = "ok") {
  class Element {
    constructor(tag) { this.tagName = tag; this.children = []; this.attributes = {}; this.dataset = {}; this.events = {}; this._text = ""; }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; this._text = ""; }
    set textContent(text) { this._text = text; this.children = []; }
    get textContent() { return this._text + this.children.map((child) => child.textContent ?? child).join(""); }
    setAttribute(name, value) { this.attributes[name] = value; }
    getAttribute(name) { return this.attributes[name]; }
    removeAttribute(name) { delete this.attributes[name]; }
    addEventListener(name, fn) { this.events[name] = fn; }
    contains(target) { return this === target || this.children.some((child) => child.contains?.(target)); }
    focus() { this.focused = true; }
    descendants() { return this.children.flatMap((child) => child instanceof Element ? [child, ...child.descendants()] : []); }
    querySelector(selector) { return this.descendants().find((node) => selector[0] === "#" ? node.id === selector.slice(1) : node.className?.split(" ").includes(selector.slice(1))); }
  }
  const body = new Element("body"); body.dataset.page = page;
  const host = new Element("header"); host.id = "app-header"; body.append(host);
  const events = {}, documentEvents = {}, timers = [], requests = [];
  let reloads = 0, redirect;
  const document = {
    body, documentElement: {}, createElement: (tag) => new Element(tag),
    getElementById: (id) => body.querySelector("#" + id),
    addEventListener(name, fn) { (documentEvents[name] ??= []).push(fn); },
    querySelectorAll(selector) {
      const attribute = /^\[([^\]]+)\]$/.exec(selector)?.[1];
      return attribute ? body.descendants().filter((node) => attribute in node.attributes) : [];
    },
  };
  const window = {
    HeaderCustom: custom,
    location: { hash, reload() { reloads++; }, replace(url) { redirect = url; } },
    addEventListener(name, fn) { (events[name] ??= []).push(fn); }, dispatchEvent() {},
  };
  const sandbox = vm.createContext({ document, window, navigator: { languages: ["en"] }, Event, AbortController, AbortSignal,
    localStorage: { getItem() { return null; }, setItem() {} },
    setTimeout(fn, delay) { timers.push({ fn, delay }); return timers.length; }, clearTimeout() {},
    async fetch(url, options) {
      requests.push({ url, options });
      return { ok: true, json: async () => ({ services: ["stt", "llm", "tts"].map((role) =>
        ({ role, name: role, state: engineState, detail: "test", url: "http://test" })) }) };
    },
  });
  for (const file of ["i18n-zh.js", "i18n-zh-hant.js", "i18n.js", "coordinator.js", "header-custom.js", "header.js"]) {
    vm.runInContext(fs.readFileSync(path.join(staticDir, file), "utf8"), sandbox);
  }
  for (const fn of documentEvents.DOMContentLoaded || []) fn();
  return { host, window, document, events, documentEvents, timers, requests, sandbox,
    get reloads() { return reloads; }, get redirect() { return redirect; } };
}

(async () => {
  for (const [page, title] of [["live", "Live translation"], ["text", "Text translation"], ["tests", "Component tests"]]) {
    const f = fixture(page);
    await flush();
    const brand = f.document.getElementById("header-branding");
    assert.equal(brand.textContent, "Audio Translation Dashboard");
    const controls = f.document.getElementById("header-controls");
    assert.equal(controls.textContent, "中文");
    const menu = f.host.querySelector(".page-menu");
    assert.equal(menu.children[0].textContent, title);
    const links = menu.children[1].children;
    assert.deepEqual(links.map((link) => link.href), ["/", "translate.html", "test.html"]);
    assert.equal(links.filter((link) => link.getAttribute("aria-current") === "page")[0].textContent, title);
    assert.equal(f.requests.length, 1); // one header poller, no live-page dependency
    assert.equal(f.requests[0].url, "/api/health");
    assert.equal(f.timers[0].delay, 10000);
    assert.equal(f.document.getElementById("services").children.length, 4);
    menu.open = true;
    menu.events.keydown({ key: "Escape", stopPropagation() {} });
    assert.equal(menu.open, false);
    assert.equal(menu.children[0].focused, true);
    menu.open = true;
    f.documentEvents.click[0]({ target: brand });
    assert.equal(menu.open, false);
    vm.runInContext("I18n.change('zh-Hant')", f.sandbox);
    assert.equal(brand.textContent, "音訊翻譯面板");
    assert.equal(controls.textContent, "EN");
    assert.equal(f.document.getElementById("services").children[0].textContent, "協調服務");
    assert.equal(f.document.getElementById("services").children[1].textContent, "轉寫");
    f.events.pagehide[0]();
    assert.equal(f.requests[0].options.signal.aborted, true);
    f.events.pageshow[0]({ persisted: true });
    assert.equal(f.reloads, 1);
  }
  const custom = fixture("text", {
    branding(container, { page }) { container.textContent = "Custom " + page; },
    controls(container, { defaults }) { defaults.controls(container); container.append(" account"); },
    statusBar(container) { container.textContent = "Custom status"; },
  });
  assert.equal(custom.document.getElementById("header-branding").textContent, "Custom text");
  assert.equal(custom.document.getElementById("header-controls").textContent, "中文 account");
  assert.equal(custom.document.getElementById("header-status-bar").textContent, "Custom status");
  assert.equal(custom.requests.length, 1); // shared health still runs with a custom display
  const unhealthy = fixture("live", undefined, "", "down");
  await flush();
  const lights = unhealthy.document.getElementById("services").children;
  assert.equal(lights[0].className, "service ok");
  assert.ok(lights.slice(1).every((light) => light.className === "service down"));
  assert.equal(unhealthy.requests.length, 1);
  assert.equal(unhealthy.timers[0].delay, 2000);
  assert.equal(fixture("live", undefined, "#text").redirect, "translate.html");
  assert.equal(fixture("live", undefined, "#tests").redirect, "test.html");
  console.log("Header checks passed: independent pages, default sections, navigation, localization, customization, polling ownership and keyboard dismissal.");
})().catch((err) => { console.error(err); process.exitCode = 1; });
