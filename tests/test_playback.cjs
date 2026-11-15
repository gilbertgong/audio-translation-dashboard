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
  removeAttribute(name) { delete this.attributes[name]; }
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
  console, AbortController, AbortSignal, Float32Array, Uint8Array, DataView, Event,
  Audio, AudioContext: Context, AudioBufferSourceNode: Source,
  performance: { now: () => 10000 },
  localStorage: { getItem() { return null; }, setItem() {} },
  document: { getElementById: element, querySelector: element, createElement: () => new Element(),
    querySelectorAll() { return []; }, documentElement: {}, addEventListener() {}, body: new Element() },
  window: { addEventListener() {}, dispatchEvent() {} }, navigator: {},
  requestAnimationFrame() { return 1; }, cancelAnimationFrame() {},
  setTimeout() { return 1; }, clearTimeout() {},
  fetch() { return new Promise(() => {}); },
});
vm.runInContext(fs.readFileSync(path.join(__dirname, "../src/static/time-stretch.js"), "utf8"), sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname, "../src/static/i18n-zh.js"), "utf8"), sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname, "../src/static/i18n-zh-hant.js"), "utf8"), sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname, "../src/static/i18n.js"), "utf8"), sandbox);
for (const file of ["audio-utils.js", "segment-splitter.js", "app.js"]) vm.runInContext(fs.readFileSync(path.join(__dirname, "../src/static/" + file), "utf8"), sandbox);
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

run('speedInputs[1].value = "1.5"; const first = fixture(); speakTranslation(first.row, first.btn, first.text, first);');
near(run("speech.ctx.sources[0].buffer.duration"), 2); // 3 source seconds become 2 output seconds
assert.equal(run("speech.ctx.sources[0].playbackRate"), 1); // processed PCM retains its pitch
run("speech.ctx.currentTime = 1.1;");
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
run('speech.ctx.currentTime = 1.1; speedInputs[1].value = "1.25";');
element("speed2").events.input();
near(run("speech.position()"), 1.5); // changing tempo keeps the original source position
assert.equal(run("speech.speed"), 1.25);
run("speech.ctx.currentTime = 0.5; noteSpeech(true);");
assert.equal(run("speech === null"), true);
near(run("speechQueue[0].resumeSeconds"), 1.9);
run("lastSpeechAt = 0; playNextSpeech();");
near(run("speech.position()"), 1.9);
element("playback-restart").click();
run("speech.ctx.currentTime = 0.3; noteSpeech(true);");
near(run("speechQueue[0].resumeSeconds"), 0.15); // restart resets a prior interruption's resume floor
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
// Disallowing interruptions suppresses only microphone samples during received audio playback.
run(`
  I18n.change('en');
  allowInterruptToggle.checked = false;
  speedInputs[1].value = '1';
  const protectedSpeech = fixture('english', false);
  protectedSpeech.audio.pieces = []; protectedSpeech.audio.seconds = 0;
  speakTranslation(protectedSpeech.row, protectedSpeech.btn, protectedSpeech.text, protectedSpeech);
`);
assert.equal(run("micInputSuppressed()"), false); // initial network buffering
run(`
  protectedSpeech.audio.pieces.push(new Float32Array(24000)); protectedSpeech.audio.seconds = 1;
  protectedSpeech.audio.listeners.forEach(fn => fn());
`);
assert.equal(run("micInputSuppressed()"), false); // existing scheduling lead, no mute pre-roll
assert.equal(run("micInputSuppressed(0.1)"), true); // unused optional offset looks ahead, in seconds
run("speech.ctx.currentTime = 0.1");
assert.equal(run("micInputSuppressed()"), true);
assert.equal(run("micInputSuppressed(-0.1)"), false);
run(`
  const manualMic = {chunks: [], splitter: null};
  const micBlock = new Float32Array([0.5, -0.5]);
  microphoneBlock(manualMic, micBlock);
  noteSpeech(true); noteSpeech(true, 'capture');
`);
assert.equal(run("manualMic.chunks[0].every(sample => sample === 0)"), true);
assert.equal(run("manualMic.chunks[0].length"), 2); // preserve time, don't concatenate around the mute
assert.equal(run("speech.item === protectedSpeech"), true);
assert.equal(run("speechQueue.length"), 0); // neither mic nor capture interrupts
const meterClasses = new Map();
element('level-meter').classList.toggle = (name, enabled) => meterClasses.set(name, enabled);
run("drawMeter(0)");
assert.equal(meterClasses.get('muted'), true);
assert.equal(element('level-db').textContent, 'Muted');
run(`
  const continuousMic = {splitter: createSegmentSplitter(1000), chunks: []};
  continuousMic.splitter.calibrating = false;
  microphoneBlock(continuousMic, new Float32Array(100).fill(0.5));
  const video = createSegmentSplitter(1000, 'captured audio', 'capture');
  video.calibrating = false; video.noiseDb = -60;
  segmentSplitterBlock(video, new Float32Array(100).fill(0.1), 0.1);
`);
assert.equal(run("continuousMic.splitter.speaking"), false);
assert.equal(run("video.speaking"), true); // captured input remains live
element('playback-pause').click();
assert.equal(run("micInputSuppressed()"), false);
run("microphoneBlock(manualMic, new Float32Array([0.25]))");
assert.equal(run("manualMic.chunks.at(-1)[0]"), 0.25);
element('playback-pause').click();
assert.equal(run("micInputSuppressed()"), true);
run("speech.ctx.currentTime = 1.1; drawMeter(0)");
assert.equal(run("micInputSuppressed()"), false); // no mute post-roll, even before final stream EOF
assert.equal(run("micInputSuppressed(-0.1)"), true); // can query an earlier playback time
assert.equal(meterClasses.get('muted'), false);
run(`
  speech.ctx.currentTime = 2;
  protectedSpeech.audio.pieces.push(new Float32Array(24000)); protectedSpeech.audio.seconds = 2;
  protectedSpeech.audio.listeners.forEach(fn => fn());
`);
assert.equal(run("micInputSuppressed()"), true); // streaming resumes after the network gap
run("allowInterruptToggle.checked = true");
assert.equal(run("micInputSuppressed()"), false); // option can be changed during playback
run("allowInterruptToggle.checked = false");
run("protectedSpeech.audio.done = true; protectedSpeech.audio.listeners.forEach(fn => fn()); speech.ctx.currentTime = 3; speech.ctx.sources.at(-1).onended();");
assert.equal(run("micInputSuppressed()"), false);
run("speakTranslation(protectedSpeech.row, protectedSpeech.btn, protectedSpeech.text, protectedSpeech); speech.ctx.currentTime = 0.1;");
element('playback-stop').click();
assert.equal(run("micInputSuppressed()"), false);
const originalConsole = sandbox.console;
sandbox.console = { ...console, error() {} }; // expected stream failure below
run(`
  const failedSpeech = fixture('english', false);
  speakTranslation(failedSpeech.row, failedSpeech.btn, failedSpeech.text, failedSpeech);
  speech.ctx.currentTime = 0.1;
  failedSpeech.audio.error = new Error('stream failed');
  failedSpeech.audio.listeners.forEach(fn => fn());
`);
sandbox.console = originalConsole;
assert.equal(run("micInputSuppressed()"), false);
let storedInterrupt;
sandbox.localStorage.setItem = (key, value) => { if (key === 'allowTranslatorInterrupt') storedInterrupt = value; };
element('allow-interrupt').events.change();
assert.equal(storedInterrupt, 'false');
run("allowInterruptToggle.checked = true");

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
// Non-unit tempo keeps one overlap state across arriving PCM pieces and
// holds its short lookahead until EOF, including a network underflow.
run(`
  speedInputs[1].value = "0.75";
  const stretchedStream = fixture("english", false);
  stretchedStream.audio.pieces = []; stretchedStream.audio.seconds = 0;
  speakTranslation(stretchedStream.row, stretchedStream.btn, stretchedStream.text, stretchedStream);
  stretchedStream.audio.pieces.push(new Float32Array(24000)); stretchedStream.audio.seconds = 1;
  stretchedStream.audio.listeners.forEach(fn => fn());
  speech.ctx.currentTime = 10;
`);
const beforeMore = run("speech.position()");
run(`
  stretchedStream.audio.pieces.push(new Float32Array(24000)); stretchedStream.audio.seconds = 2;
  stretchedStream.audio.listeners.forEach(fn => fn());
`);
near(run("speech.position()"), beforeMore); // network waiting is not heard audio
run(`
  stretchedStream.audio.done = true; stretchedStream.audio.listeners.forEach(fn => fn());
`);
near(run("speech.ctx.sources.reduce((sum, source) => sum + source.buffer.duration, 0)"), 64000 / 24000);
assert.equal(run("speech.ctx.sources.every(source => source.playbackRate === 1)"), true);
run("speech.ctx.sources.at(-1).onended();");
assert.equal(run("speech"), null);
// Localized button titles do not control operational playback state.
run(`
  I18n.change("zh-Hans");
  const chineseUI = fixture();
  speakTranslation(chineseUI.row, chineseUI.btn, chineseUI.text, chineseUI);
`);
assert.equal(run("speech.btn.attributes.title"), "停止朗读");
assert.match(element("playback-status").textContent, /^正在朗读/);
element("playback-pause").click();
assert.match(element("playback-status").textContent, /^已暂停/);
run("stopPlayback(); I18n.change('en');");
assert.equal(run(`isRecordingShortcut({ code: "Space", target: { closest: (selector) => selector === "#app-header" } })`), false);
console.log("Playback checks passed: rate scheduling, position, pause/resume, restart, interruptions, next, stop/abort, independent language speeds, meter, recordings.");


// Mobile defaults and mock overrides use the real config application path.
(async () => {
  const classes = new Set();
  sandbox.document.body.classList.toggle = (name, enabled) => {
    if (enabled) classes.add(name); else classes.delete(name);
  };
  sandbox.document.body.classList.contains = name => classes.has(name);
  sandbox.URL = URL;
  sandbox.window.location = { href: 'http://localhost/?mockMobile=1' };
  const controls = element('header-controls');
  const language = element('ui-language');
  controls.children = [language];
  controls.insertBefore = (child, reference) => {
    assert.equal(reference, language);
    controls.children.unshift(child);
  };
  const writes = [];
  sandbox.localStorage.setItem = (...args) => writes.push(args);
  sandbox.console = { ...console, warn(error, detail) { throw detail || Error(error); } };
  run('showMockConversation = () => {};');
  let config = { mock: true, sample_rate: 16000, auto_split: { enabled: true } };
  sandbox.fetch = async () => ({ json: async () => config });
  await run('loadConfig()');
  assert.equal(classes.has('mobile'), true);
  assert.equal(classes.has('activity-hidden'), true);
  assert.equal(element('auto-split').checked, false);
  assert.equal(element('allow-interrupt').checked, false);
  assert.equal(controls.children[0].textContent, 'Mock Desktop');
  assert.deepEqual(writes, []); // preview defaults do not overwrite saved choices
  controls.children[0].click();
  assert.equal(new URL(sandbox.window.location.href).searchParams.get('mockMobile'), '0');
  controls.children = [language];
  await run('loadConfig()');
  assert.equal(classes.has('mobile'), false);
  assert.equal(classes.has('activity-hidden'), false);
  assert.equal(element('auto-split').checked, true);
  assert.equal(element('allow-interrupt').checked, true);
  assert.equal(controls.children[0].textContent, 'Mock Mobile');
  // Preview query parameters have no authority over a real dashboard.
  controls.children = [language];
  sandbox.window.location.href = 'http://localhost/?mockMobile=1';
  config.mock = false;
  await run('loadConfig()');
  assert.equal(element('auto-split').checked, true);
  assert.equal(controls.children.length, 1);
  sandbox.navigator.userAgent = 'iPhone';
  assert.equal(run('isMobileBrowser()'), true);
  run('mobileBrowser = isMobileBrowser();');
  await run('loadConfig()');
  assert.equal(element('auto-split').checked, false);
  console.log('Mobile defaults and mock desktop/mobile config checks passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
