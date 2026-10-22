// Offline playback checks: node tests/test_playback.cjs
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

class Element {
  constructor() {
    this.value = "1";
    this.dataset = {};
    this.style = {};
    this.children = [];
    this.attributes = {};
    this.events = {};
    this.selectors = {};
    this.previousElementSibling = {};
    this.classList = { toggle() {}, add() {}, remove() {}, contains() { return false; } };
  }
  addEventListener(event, fn) { this.events[event] = fn; }
  setAttribute(name, value) { this.attributes[name] = value; }
  querySelector(selector) { return this.selectors[selector] ??= new Element(); }
  appendChild(element) { this.children.push(element); return element; }
  append() {}
  replaceChildren(...elements) { this.children = elements; }
  click() { this.events.click?.({}); }
}
const elements = new Map();
const element = (id) => {
  if (!elements.has(id)) elements.set(id, new Element());
  return elements.get(id);
};
class Context {
  constructor() { this.currentTime = 0; this.state = "running"; this.sources = []; this.destination = {}; }
  suspend() { this.state = "suspended"; return Promise.resolve(); }
  resume() { this.state = "running"; return Promise.resolve(); }
  close() { this.state = "closed"; return Promise.resolve(); }
  createAnalyser() { return { connect() {}, getFloatTimeDomainData(samples) { samples.fill(0.1); } }; }
  createBuffer(channels, length, rate) { return { duration: length / rate, copyToChannel() {} }; }
  createMediaElementSource() { return { connect: (node) => node }; }
}
class Source {
  constructor(ctx, options) { Object.assign(this, options); ctx.sources.push(this); }
  connect() {}
  start(at, offset) { this.at = at; this.offset = offset; }
}
class Audio extends Element {
  play() { this.paused = false; return Promise.resolve(); }
  pause() { this.paused = true; }
}
const sandbox = vm.createContext({
  console, AbortController, AbortSignal, Float32Array, Uint8Array, DataView,
  Audio, AudioContext: Context, AudioBufferSourceNode: Source,
  performance: { now: () => 10000 },
  localStorage: { getItem() { return null; }, setItem() {} },
  document: { getElementById: element, querySelector: element, createElement: () => new Element(),
    addEventListener() {}, body: new Element() },
  window: { addEventListener() {} }, navigator: {},
  requestAnimationFrame() { return 1; }, cancelAnimationFrame() {},
  setTimeout() { return 1; }, clearTimeout() {},
  fetch() { return new Promise(() => {}); },
});
vm.runInContext(fs.readFileSync(path.join(__dirname, "../src/static/app.js"), "utf8"), sandbox);
vm.runInContext(`
  language1 = "English"; language2 = "Chinese";
  function fixture(language = "english", done = true) {
    const row = document.createElement("div");
    row.dataset.language = language;
    return { row, btn: document.createElement("button"), text: "test",
      audio: { pieces: [new Float32Array(24000), new Float32Array(48000)],
        rate: 24000, seconds: 3, done, listeners: new Set(), controller: new AbortController() } };
  }
`, sandbox);
const run = (code) => vm.runInContext(code, sandbox);
const near = (value, expected) => assert.ok(Math.abs(value - expected) < 0.00001, `${value} != ${expected}`);

run('speedInputs[1].value = "2"; const first = fixture(); speakTranslation(first.row, first.btn, first.text, first);');
near(run("speech.ctx.sources[1].at"), 0.6); // first 1 s piece occupies 0.5 s
run("speech.ctx.currentTime = 0.85;");
near(run("speech.position()"), 1.5);
element("playback-pause").click();
assert.equal(run("speech.ctx.state"), "suspended");
run("updatePlaybackTimeline();");
assert.equal(element("playback-duration").textContent, "/ 0:03");
element("playback-position").value = "1.2";
element("playback-position").events.input();
run("drawPlaybackMeter();");
assert.equal(element("playback-position").value, "1.2"); // drag preview survives animation frames
element("playback-position").events.change();
assert.equal(run("speech.ctx.state"), "suspended");
near(run("speech.position()"), 1.2);
assert.equal(element("playback-elapsed").textContent, "0:01");
run("noteSpeech(true)");
assert.equal(run("speechQueue.length"), 0); // user pause must not trigger interruption rewind
element("playback-restart").click();
assert.equal(run("speech.ctx.sources[0].offset"), 0);
assert.equal(run("speech.ctx.state"), "suspended");
element("playback-pause").click();
run('speech.ctx.currentTime = 0.85; speedInputs[1].value = "1.5";');
element("speed2").events.input();
near(run("speech.ctx.sources[0].offset"), 0.5); // skips first piece, resumes midway through second
assert.equal(run("speech.speed"), 1.5);
run("speech.ctx.currentTime = 0.5; noteSpeech(true);");
assert.equal(run("speech === null"), true);
near(run("speechQueue[0].resumeSeconds"), 2);
run("lastSpeechAt = 0; playNextSpeech();");
near(run("speech.ctx.sources[0].offset"), 1);
element("playback-restart").click();
run("speech.ctx.currentTime = 0.3; noteSpeech(true);");
near(run("speechQueue[0].resumeSeconds"), 0.2); // restart resets a prior interruption's resume floor
run("lastSpeechAt = 0; playNextSpeech();");

run('const second = fixture("chinese"); speechQueue.push(second); updateStatus();');
assert.equal(element("playback-next").disabled, false);
element("playback-next").click();
assert.equal(run("speech.item === second"), true);
assert.equal(run("speech.speed"), 1); // English speed independent of Chinese
run("const queued = fixture('english', false); speechQueue.push(queued);");
element("playback-pause").click();
element("playback-stop").click();
assert.equal(run("speechQueue.length"), 0);
assert.equal(run("speech"), null);
assert.equal(run("playbackPaused"), false);
assert.equal(run("queued.audio.controller.signal.aborted"), true);
assert.equal(element("playback-stop").disabled, true);
assert.equal(element("playback-position").disabled, true);
assert.equal(element("playback-elapsed").textContent, "0:00");

// Manual playback shares the meter and transport but is not interrupted by speech.
run("const manual = fixture(); speakTranslation(manual.row, manual.btn, manual.text, null, { audio: manual.audio, position: 0 }); drawPlaybackMeter();");
assert.equal(element("playback-meter").attributes["aria-valuenow"], "-20");
run("noteSpeech(true)");
assert.equal(run("speech !== null"), true);
run("stopPlayback(); playingBtn = document.createElement('button'); connectRecordingMeter();");
element("playback-pause").click();
assert.equal(run("player.paused"), true);
element("playback-restart").click();
assert.equal(run("player.currentTime"), 0);
element("playback-pause").click();
assert.equal(run("player.paused"), false);
run("stopPlayback()");

// Pausing during buffering holds newly arriving streamed audio too.
run(`
  const streaming = fixture("english", false);
  streaming.audio.pieces = []; streaming.audio.seconds = 0;
  speakTranslation(streaming.row, streaming.btn, streaming.text, streaming);
`);
element("playback-pause").click();
run(`
  streaming.audio.pieces.push(new Float32Array(24000)); streaming.audio.seconds = 1;
  streaming.audio.listeners.forEach(fn => fn());
`);
assert.equal(run("speech.ctx.state"), "suspended");
assert.equal(run("speech.ctx.sources.length"), 1);
assert.equal(element("playback-duration").textContent, "/ 0:01+");
run(`
  streaming.audio.pieces.push(new Float32Array(24000)); streaming.audio.seconds = 2;
  streaming.audio.done = true; streaming.audio.listeners.forEach(fn => fn());
`);
assert.equal(run("speech.ctx.sources.length"), 2);
assert.equal(element("playback-duration").textContent, "/ 0:02");
element("playback-pause").click();
run("speech.ctx.sources[1].onended();");
assert.equal(run("speech"), null);
console.log("Playback checks passed: rate scheduling, position, pause/resume, restart, interruptions, next, stop/abort, independent language speeds, meter, recordings.");
