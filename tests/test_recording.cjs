// Offline push-to-talk interaction checks: node tests/test_recording.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../src/static/app.js'), 'utf8');
function fixture() {
  const events = {};
  const button = { disabled: false, classList: { add() {}, remove() {}, toggle() {} },
    addEventListener(name, fn) { events['button:' + name] = fn; },
    click() { events['button:click'](); } };
  const mode = { checked: false, addEventListener() {} };
  const hint = {};
  const toggleHint = {};
  let resolveStart;
  const state = { starts: 0, stops: 0, sends: 0, defer: false, fail: false };
  const context = vm.createContext({ console: { error() {} }, recordBtn: button,
    autoSplitToggle: mode, micRecording: null, rawDialog: { open: false },
    I18n: { msg: x => x, text: (el, text) => { el.text = text; }, attr() {}, errorText: x => x },
    document: { getElementById: id => id === "talk-shortcut" ? hint : toggleHint, addEventListener: (name, fn) => { events[name] = fn; } },
    window: { addEventListener: (name, fn) => { events[name] = fn; } },
    async startRecording(auto) {
      state.starts++;
      if (state.defer) await new Promise(resolve => { resolveStart = resolve; });
      if (state.fail) throw Error('permission denied');
      context.micRecording = { splitter: auto ? {} : null };
    },
    async stopRecording() { state.stops++; context.micRecording = null; return {}; },
    processAudioSegment() { state.sends++; }, transcriptContext() {}, updateStatus() {},
    addNote() {}, setIndicator() {}, stopMeter() {},
  });
  vm.runInContext(source.slice(source.indexOf('// A held input owns'), source.indexOf('// --- Capture Audio')), context);
  vm.runInContext(source.slice(source.indexOf('function isRecordingShortcut'), source.indexOf('// Discard unfinished audio')), context);
  const fire = (name, overrides = {}) => {
    const event = { code: 'Space', button: 0, pointerId: 0, repeat: false,
      target: { tagName: 'BODY', closest() { return false; } },
      prevented: false, preventDefault() { this.prevented = true; }, ...overrides };
    events[name](event); return event;
  };
  return { state, button, mode, hint, fire, ready: () => resolveStart() };
}
const settle = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };
(async () => {
  for (const input of ['pointer', 'Space', 'Backquote']) {
    const f = fixture();
    assert.equal(f.button.text, 'Hold to Talk');
    assert.equal(f.hint.hidden, false);
    f.fire(input === 'pointer' ? 'button:pointerdown' : 'keydown', { code: input, key: input === 'Backquote' ? '`' : ' ' });
    await settle();
    assert.equal(f.button.text, 'Release to send');
    f.fire('keydown', { repeat: true });
    assert.equal(f.state.starts, 1);
    f.fire(input === 'pointer' ? 'pointerup' : 'keyup', { code: input });
    await settle();
    assert.equal(f.state.stops, 1); assert.equal(f.state.sends, 1);
    assert.equal(f.button.text, 'Hold to Talk');
    f.fire('button:click'); // synthesized click must not start another recording
    assert.equal(f.state.starts, 1);
  }
  for (const release of ['pointerup', 'pointercancel', 'blur']) {
    const f = fixture(); f.state.defer = true;
    f.fire('button:pointerdown'); f.fire(release); f.ready(); await settle();
    assert.equal(f.state.stops, 1); assert.equal(f.button.text, 'Hold to Talk');
  }
  const denied = fixture(); denied.state.fail = true;
  denied.fire('keydown'); await settle(); denied.fire('keyup');
  denied.state.fail = false; denied.fire('keydown'); await settle(); denied.fire('keyup'); await settle();
  assert.equal(denied.state.sends, 1);
  const continuous = fixture(); continuous.mode.checked = true;
  continuous.fire('keydown'); await settle(); continuous.fire('keyup');
  assert.equal(continuous.state.stops, 0);
  continuous.fire('keydown'); await settle(); assert.equal(continuous.state.stops, 1);
  const excluded = fixture();
  for (const target of [
    { tagName: 'INPUT', type: 'text', closest() {} },
    { tagName: 'SELECT', closest() {} },
    { tagName: 'BUTTON', closest: selector => selector === '#playback' },
    { tagName: 'BUTTON', closest: selector => selector === '#app-header' },
  ]) assert.equal(excluded.fire('keydown', { target }).prevented, false);
  assert.equal(excluded.state.starts, 0);
  for (const overrides of [
    { target: { tagName: 'TEXTAREA', closest() {} } },
    { target: { isContentEditable: true, closest() {} } },
    { ctrlKey: true }, { altKey: true }, { metaKey: true },
    { key: '~', shiftKey: true }, { isComposing: true },
  ]) assert.equal(excluded.fire('keydown', { code: 'Backquote', key: '`', ...overrides }).prevented, false);
  assert.equal(excluded.state.starts, 0);
  const backtick = fixture(); backtick.mode.checked = true;
  assert.equal(backtick.fire('keydown', { code: 'Backquote', key: '`' }).prevented, true);
  await settle();
  assert.equal(backtick.state.starts, 1);
  backtick.fire('keydown', { code: 'Backquote', key: '`', repeat: true });
  backtick.fire('keyup', { code: 'Backquote', key: '`' });
  assert.equal(backtick.state.stops, 0);
  backtick.fire('keydown', { code: 'Backquote', key: '`' });
  await settle(); assert.equal(backtick.state.stops, 1);
  const pending = fixture(); pending.state.defer = true;
  pending.fire('keydown', { code: 'Backquote', key: '`' });
  pending.fire('keyup', { code: 'Backquote', target: { tagName: 'TEXTAREA', closest() {} } });
  pending.ready(); await settle(); assert.equal(pending.state.stops, 1);
  console.log('Recording checks passed: pointer/Space/backtick holds, release, startup races, cancellation/blur, permission failure, continuous toggling and native Space exclusions.');
})().catch(err => { console.error(err); process.exitCode = 1; });
