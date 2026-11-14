// Actual Live helpers with deterministic network/body streams and time; no servers/models.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const Clock = require("./helpers/fake_clock.cjs");

function fixture() {
  const clock = new Clock(), elements = new Map(), calls = [], listeners = new Set();
  class Element {
    constructor() {
      this.value = "1"; this.children = []; this.dataset = {}; this.style = {}; this.attributes = {}; this.selectors = {};
      this.events = {}; this.classes = new Set();
      this.classList = { toggle: (name, value) => value ? this.classes.add(name) : this.classes.delete(name),
        add: (...names) => names.forEach((name) => this.classes.add(name)), remove: (...names) => names.forEach((name) => this.classes.delete(name)),
        contains: (name) => this.classes.has(name) };
      this.previousElementSibling = {};
    }
    addEventListener(name, fn) { this.events[name] = fn; }
    setAttribute(name, value) { this.attributes[name] = value; }
    removeAttribute(name) { delete this.attributes[name]; }
    append(...children) { this.children.push(...children); }
    appendChild(child) { this.append(child); return child; }
    insertBefore(child) { this.append(child); }
    replaceChildren(...children) { this.children = children; }
    querySelector(selector) { return selector === ".placeholder" ? null : this.selectors[selector] ??= new Element(); }
    remove() {}
  }
  const element = (id) => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  let state = "up", coordinatorError = null;
  const coordinator = {
    subscribe(fn) { listeners.add(fn); fn({ state }); return () => listeners.delete(fn); },
    set(next, error) { state = next; coordinatorError = error; listeners.forEach((fn) => fn({ state, error })); },
    check() {},
    async wait(signal) {
      signal.throwIfAborted();
      if (state === "blocked") throw coordinatorError;
      if (state === "up") return;
      await new Promise((resolve, reject) => {
        const cleanup = () => { listeners.delete(change); signal.removeEventListener("abort", abort); };
        const change = () => { if (state === "up") { cleanup(); resolve(); } };
        const abort = () => { cleanup(); reject(signal.reason); };
        listeners.add(change); signal.addEventListener("abort", abort, { once: true });
      });
    },
  };
  let network = async () => { throw new Error("Unexpected network call"); };
  class Audio extends Element { play() { return Promise.resolve(); } pause() {} }
  const sandbox = vm.createContext({
    console: { error() {}, warn() {} }, TypeError, SyntaxError, Error, AbortController, AbortSignal, Event, Blob,
    Float32Array, Uint8Array, DataView, TextDecoder, Audio,
    URL: { createObjectURL: () => "blob:test", revokeObjectURL() {} },
    performance: { now: () => clock.now },
    localStorage: { getItem() { return null; }, setItem() {} },
    navigator: {}, window: { addEventListener() {}, dispatchEvent() {} },
    document: { getElementById: element, createElement: () => new Element(), querySelector: element,
      querySelectorAll: () => [], addEventListener() {}, body: new Element(), documentElement: {} },
    requestAnimationFrame() { return 1; }, cancelAnimationFrame() {},
    setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout,
    Coordinator: coordinator,
    fetch(url, options) {
      if (url === "/api/config") return new Promise(() => {});
      calls.push({ url, options }); return network(url, options);
    },
  });
  for (const file of ["i18n-zh.js", "i18n-zh-hant.js", "i18n.js", "translation-actions.js", "app.js"]) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, "../src/static", file), "utf8"), sandbox);
  }
  const run = (code) => vm.runInContext(code, sandbox);
  sandbox.row = new Element(); sandbox.wav = new Blob([new Uint8Array(100)]);
  run("language1='English'; language2='Chinese';");
  return { clock, run, sandbox, row: sandbox.row, calls, coordinator, element, Element,
    network(fn) { network = fn; }, upload: () => run("uploadWav(wav)"),
    translate: () => run("translateRow(row, 'original text', 'english')"), abort: () => run("pageLifetime.abort()") };
}
function json(status = 200, body = {}) { return { ok: status === 200, status, json: async () => body }; }
const done = "data: [DONE]\n\n";
const piece = (text) => `data: ${JSON.stringify({ choices: [{ delta: { content: text } }] })}\n\n`;
function stream(chunks = [], error = null) {
  let index = 0;
  const reader = {
    async read() {
      if (index < chunks.length) return { value: new TextEncoder().encode(chunks[index++]), done: false };
      if (error) throw error;
      return { done: true };
    },
    async cancel() { reader.cancelled = true; }, releaseLock() { reader.released = true; },
  };
  return { ok: true, body: { getReader: () => reader }, reader };
}

(async () => {
  const f = fixture(); let count = 0;
  f.network(async () => { if (++count < 3) throw new TypeError("fetch failed"); return json(200, { text: "hello" }); });
  const result = f.upload(); await f.clock.flush(); assert.equal(f.calls.length, 1);
  await f.clock.advance(999); assert.equal(f.calls.length, 1);
  await f.clock.advance(1); assert.equal(f.calls.length, 2);
  await f.clock.advance(2999); assert.equal(f.calls.length, 2);
  await f.clock.advance(1); assert.equal((await result).text, "hello");
  assert.ok(f.calls.every((call) => call.options.body === f.sandbox.wav));

  for (const status of [502, 503, 504]) {
    const f = fixture(); let count = 0, cancelled = 0;
    f.network(async () => ++count === 1
      ? { ...json(status), body: { async cancel() { cancelled++; } } }
      : json(200, { text: "recovered" }));
    const result = f.upload(); await f.clock.flush(); await f.clock.advance(1000);
    assert.equal((await result).text, "recovered"); assert.equal(count, 2); assert.equal(cancelled, 1);
  }
  for (const status of [400, 401, 403, 404, 408, 429, 500]) {
    const f = fixture(); const response = json(status);
    f.network(async () => response);
    assert.equal(await f.run("pageFetch('/api/translate')"), response);
    assert.equal(f.calls.length, 1);
  }
  for (const url of ["/api/recordings/recheck", "/api/speak", "/api/health"]) {
    const f = fixture(); f.network(async () => json(503));
    assert.equal((await f.run(`pageFetch('${url}')`)).status, 503);
    assert.equal(f.calls.length, 1);
  }
  // Final HTTP response and original caller's error/body contract are unchanged.
  { const f = fixture(); const response = json(503, { error: "last error", file: "recording.wav" });
    f.network(async () => response);
    const failure = assert.rejects(f.upload(), (err) => err.message === "last error" && err.body.file === "recording.wav");
    await f.clock.flush(); await f.clock.advance(1000); await f.clock.advance(3000); await failure;
    assert.equal(f.calls.length, 3);
    f.network(async () => json(200, { text: "next" })); assert.equal((await f.upload()).text, "next"); }
  { const f = fixture(); f.network(async () => { throw new TypeError("offline"); });
    const failure = assert.rejects(f.upload(), TypeError); await f.clock.flush();
    await f.clock.advance(1000); await f.clock.advance(3000); await failure; assert.equal(f.calls.length, 3); }

  // Caller-owned JSON/stream reads occur after pageFetch returns and are never replayed.
  { const f = fixture(); f.network(async () => ({ ok: true, json: async () => { throw new TypeError("body lost"); } }));
    await assert.rejects(f.upload(), TypeError); await f.clock.advance(10000); assert.equal(f.calls.length, 1); }
  { const f = fixture(); f.network(async () => stream([piece("partial")], new TypeError("stream lost")));
    await assert.rejects(f.translate(), TypeError); await f.clock.advance(10000); assert.equal(f.calls.length, 1); }

  { const f = fixture(); f.coordinator.set("down"); f.network(async () => json());
    const result = f.upload(); await f.clock.flush(); await f.clock.advance(10000); assert.equal(f.calls.length, 0);
    f.coordinator.set("up"); await result; assert.equal(f.calls.length, 1); }
  { const f = fixture(); const error = new Error("access denied"); f.coordinator.set("blocked", error);
    await assert.rejects(f.upload(), (err) => err === error); assert.equal(f.calls.length, 0); }
  // Original toWhisper queue still owns ordering through retries.
  { const f = fixture(); let count = 0;
    f.network(async () => ++count === 1 ? json(503) : json());
    const first = f.upload(); f.sandbox.otherWav = new Blob(["other"]);
    const second = f.run("uploadWav(otherWav)"); await f.clock.flush(); assert.equal(f.calls.length, 1);
    await f.clock.advance(1000); await Promise.all([first, second]);
    assert.deepEqual(f.calls.map((call) => call.options.body), [f.sandbox.wav, f.sandbox.wav, f.sandbox.otherWav]); }
  for (const phase of ["delay", "offline", "queue", "inflight", "caller-signal"]) {
    const f = fixture(); let finish;
    if (phase === "offline") f.coordinator.set("down");
    if (phase === "queue") f.run("toWhisper(() => new Promise(resolve => globalThis.releaseQueue = resolve))");
    f.network(async () => {
      if (phase === "inflight") return new Promise((resolve) => { finish = resolve; });
      throw new TypeError("offline");
    });
    const result = phase === "caller-signal"
      ? f.run("globalThis.operation=new AbortController(); pageFetch('/api/translate', {signal: operation.signal})") : f.upload();
    const cancelled = assert.rejects(result, { name: "AbortError" }); await f.clock.flush();
    const count = f.calls.length;
    if (phase === "caller-signal") f.run("operation.abort()"); else f.abort();
    if (finish) finish(json());
    if (phase === "queue") f.run("releaseQueue()");
    f.coordinator.set("up"); await cancelled; await f.clock.advance(10000); assert.equal(f.calls.length, count);
  }
  console.log("pageFetch checks passed: bounded retries/HTTP policy, unchanged upload/queue contract, Coordinator gating, cancellation, and no response-body replay.");
})().catch((err) => { console.error(err); process.exitCode = 1; });

