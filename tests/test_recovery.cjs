// Actual Live helpers with deterministic network/body streams and time; no servers/models.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const Clock = require("./helpers/fake_clock.cjs");

function fixture({ savedInterrupt = null, storageBlocked = false, browser = {} } = {}) {
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
    Float32Array, Uint8Array, DataView, TextDecoder, TextEncoder, Audio,
    URL: { createObjectURL: () => "blob:test", revokeObjectURL() {} },
    performance: { now: () => clock.now },
    localStorage: {
      getItem(key) { if (storageBlocked) throw new Error('storage blocked'); return key === 'allowTranslatorInterrupt' ? savedInterrupt : null; },
      setItem() {},
    },
    navigator: browser, window: { addEventListener() {}, dispatchEvent() {} },
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
  for (const file of ["i18n-zh.js", "i18n-zh-hant.js", "i18n.js", "translation-actions.js", "audio-utils.js", "segment-splitter.js", "app.js"]) {
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
  assert.equal(fixture().run("allowInterruptToggle.checked"), true);
  assert.equal(fixture({savedInterrupt:'false'}).run("allowInterruptToggle.checked"), false);
  assert.equal(fixture({storageBlocked:true}).run("allowInterruptToggle.checked"), true);
  for (const browser of [
    {userAgentData:{mobile:true}}, {userAgent:'iPhone'}, {userAgent:'iPad'},
    {userAgent:'Android', userAgentData:{mobile:false}}, {platform:'MacIntel', maxTouchPoints:5},
  ]) {
    assert.equal(fixture({browser}).run("allowInterruptToggle.checked"), false);
    assert.equal(fixture({browser, storageBlocked:true}).run("allowInterruptToggle.checked"), false);
    assert.equal(fixture({browser, savedInterrupt:'true'}).run("allowInterruptToggle.checked"), true);
  }
  assert.equal(fixture({browser:{userAgentData:{mobile:false}, platform:'MacIntel', maxTouchPoints:0}})
    .run("allowInterruptToggle.checked"), true);
  { const f = fixture();
    assert.equal(f.run("transcriptTail('prefix '.repeat(40) + 'I think…')"), 'prefix '.repeat(13) + 'I think…');
    assert.equal(f.run("transcriptTail('a'.repeat(150))"), ''); // no partial oversized word
    assert.equal(f.run("transcriptTail('x'.repeat(110) + ' Hello world')"), 'Hello world');
    assert.equal(f.run("transcriptTail('中'.repeat(100))"), '');
    assert.equal(f.run("transcriptTail('中'.repeat(100) + '， 继续说明。')"), '继续说明。');
    assert.equal(f.run("transcriptTail('😀'.repeat(100) + ' next phrase')"), 'next phrase');
    assert.equal(f.run("transcriptTail('这是一个短句子。')"), '这是一个短句子。');
    const chineseTail = f.run("transcriptTail('中， '.repeat(100))");
    assert.equal(chineseTail, '中， '.repeat(28)); // 196 bytes; retain whole space-delimited spans
    assert.equal(f.run("transcriptTail('I think…')"), 'I think…');
    assert.equal(f.run("transcriptTail('I think…\\n What next?')"), 'I think…\n What next?');
  }
  // Context follows the same speech-clock pause rule as playback, separately per source.
  { const f = fixture();
    f.run("noteSpeech(false, 'capture'); globalThis.initial = transcriptContext('capture'); initial.text='previous';");
    await f.clock.advance(1999);
    f.run("noteSpeech(false, 'capture'); globalThis.continuation=transcriptContext('capture');");
    assert.equal(f.run("continuation.previous === initial"), true);
    assert.equal(f.run("transcriptContext('mic').previous"), null);
    await f.clock.advance(2000);
    assert.equal(f.run("turnWait(transcriptSources.capture.lastSpeechAt)"), 0);
    f.run("noteSpeech(false, 'capture')");
    assert.equal(f.run("transcriptContext('capture').previous"), null);
  }
  { const f = fixture(); let finish;
    const text = "earlier sentence " + "中😀".repeat(120) + "， 后续内容。";
    f.network(async () => {
      if (f.calls.length === 1) await new Promise((resolve) => { finish = resolve; });
      return json(200, { text });
    });
    f.run("noteSpeech(false, 'capture'); globalThis.first=transcriptContext('capture')");
    const first = f.run("uploadWav(wav, first)"); await f.clock.flush();
    await f.clock.advance(100);
    f.run("noteSpeech(false, 'capture'); globalThis.second=transcriptContext('capture')");
    const second = f.run("uploadWav(wav, second)"); await f.clock.flush();
    // A slow transcription cannot expire context for a continuation already recorded.
    await f.clock.advance(10000); finish(); await Promise.all([first, second]);
    assert.equal(f.calls[0].options.headers['X-Whisper-Prompt'], undefined);
    assert.equal(decodeURIComponent(f.calls[1].options.headers['X-Whisper-Prompt']), '后续内容。');
    assert.equal(f.calls[1].options.body, f.sandbox.wav);
    assert.equal(f.run("second.previous"), null);
    // New speech after the recording pause starts a new turn, regardless of the old result.
    f.run("noteSpeech(false, 'capture')");
    await f.run("uploadWav(wav, transcriptContext('capture'))");
    assert.equal(f.calls[2].options.headers['X-Whisper-Prompt'], undefined);
  }
  { const f = fixture();
    f.run("capture={muted:true}; noteSpeech(false, 'capture'); globalThis.initial=transcriptContext('capture'); initial.text='YouTube';");
    await f.clock.advance(1000);
    f.run("noteSpeech(false, 'capture')");
    assert.equal(f.run("transcriptContext('capture').previous.text"), "YouTube");
    assert.equal(f.run("lastSpeechAt"), 0); // muted video doesn't delay automatic playback
  }
  { const f = fixture();
    f.network(async () => json(200, { text: "wrong language", recheck: true }));
    f.run("noteSpeech(false); globalThis.first=transcriptContext('mic')");
    await f.run("uploadWav(wav, first)");
    assert.equal(f.run("first.text"), "");
    f.network(async () => json(200, { text: "corrected", language: "english", stt_seconds: 1 }));
    await f.run("recheck(row, {file:'recording.wav', text:'wrong language'}, 'reason', first)");
    assert.equal(f.run("first.text"), "corrected");
    assert.equal(f.calls[1].url, "/api/recordings/recheck");
  }
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
  for (const url of ["/api/recordings/recheck", "/api/health"]) {
    const f = fixture(); f.network(async () => json(503));
    assert.equal((await f.run(`pageFetch('${url}')`)).status, 503);
    assert.equal(f.calls.length, 1);
  }
  { const f = fixture(); let count = 0; const audio = stream([], new TypeError("audio body lost"));
    f.network(async () => { count++; if (count === 1) throw new TypeError("network"); return count === 2 ? json(503) : audio; });
    const result = f.run("pageFetch('/api/speak', {method:'POST', body:'speech input'})");
    await f.clock.flush(); await f.clock.advance(1000); await f.clock.advance(3000);
    assert.equal(await result, audio); assert.equal(f.calls.length, 3);
    assert.ok(f.calls.every((call) => call.options.body === "speech input"));
    await assert.rejects(audio.body.getReader().read(), TypeError);
    await f.clock.advance(10000); assert.equal(f.calls.length, 3); // no replay after audio response returned
  }
  { const f = fixture(); f.network(async () => json(503));
    const cancelled = assert.rejects(f.run("globalThis.speechController = new AbortController(); pageFetch('/api/speak', {signal:speechController.signal})"), { name: "AbortError" });
    await f.clock.flush(); f.run("speechController.abort()"); await cancelled;
    await f.clock.advance(10000); assert.equal(f.calls.length, 1);
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

