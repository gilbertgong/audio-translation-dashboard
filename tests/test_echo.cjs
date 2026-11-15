// Browser-controller checks without physical audio: node tests/test_echo.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../src/static/echo-test.js'), 'utf8');
const settle = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };

function fixture() {
  class Events {
    events = {};
    addEventListener(name, fn) { (this.events[name] ??= []).push(fn); }
    fire(name, event = {}) { return this.events[name]?.map(fn => fn(event)); }
  }
  const elements = {};
  for (const id of ['echo-connect', 'echo-delay', 'echo-delay-value', 'echo-meter', 'echo-db', 'echo-rtt']) {
    const classes = new Set(), children = {};
    elements[id] = Object.assign(new Events(), { value: '0', style: {},
      querySelector: name => children[name] ??= { style: {} },
      setAttribute(name, value) { this[name] = value; },
      classList: { classes, add(...names) { names.forEach(name => classes.add(name)); },
        remove(...names) { names.forEach(name => classes.delete(name)); },
        toggle(name, active) { if (active) classes.add(name); else classes.delete(name); } } });
  }
  const sockets = [], contexts = [], nodes = [], status = {};
  let permit, deny, worklet;
  let now = 100, frameId = 0;
  const frames = new Map();
  const audio = { tracks: [{ stopped: false, stop() { this.stopped = true; } }], getTracks() { return this.tracks; } };
  class Socket extends Events {
    static OPEN = 1;
    readyState = 0;
    sent = [];
    constructor(url) { super(); this.url = url; sockets.push(this); }
    send(data) { this.sent.push(data); }
    close() { this.readyState = 3; this.fire('close'); }
  }
  class Context {
    sampleRate = 48000;
    state = 'suspended';
    currentTime = 1;
    destination = {};
    sources = [];
    audioWorklet = { addModule: () => worklet ? new Promise(resolve => { worklet = resolve; }) : Promise.resolve() };
    constructor() { contexts.push(this); }
    resume() { this.state = 'running'; return Promise.resolve(); }
    close() { this.state = 'closed'; return Promise.resolve(); }
    createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
    createBuffer(channels, count, rate) {
      const samples = new Float32Array(count);
      return { duration: count / rate, getChannelData: () => samples };
    }
    createBufferSource() {
      const item = { connect() {}, disconnect() {}, start(at) { this.at = at; }, stop() { this.stopped = true; } };
      this.sources.push(item);
      return item;
    }
  }
  class Worklet {
    port = {};
    constructor() { nodes.push(this); }
    connect() {}
    disconnect() {}
  }
  const window = Object.assign(new Events(), { location: { href: 'https://example.test/test.html' } });
  const sandbox = vm.createContext({ console, URL, DataView, ArrayBuffer, Uint8Array, Math, Set,
    WebSocket: Socket, AudioContext: Context, AudioWorkletNode: Worklet, window,
    document: { getElementById: id => elements[id] },
    crypto: { randomUUID: () => 'random-channel' }, btoa: value => Buffer.from(value, 'binary').toString('base64'),
    performance: { now: () => now },
    requestAnimationFrame: fn => { frames.set(++frameId, fn); return frameId; },
    cancelAnimationFrame: id => frames.delete(id),
    audioProcessing: {}, navigator: { mediaDevices: { getUserMedia: () => new Promise((resolve, reject) => { permit = resolve; deny = reject; }) } },
    I18n: { msg: (key, values) => ({ key, values }), text: (element, value) => { element.message = value; },
      attr: (element, name, value) => { element[name] = value; },
      error: message => new Error(message), errorText: error => error.message },
    setStatus: (id, message, error) => { status[id] = { message, error }; },
  });
  vm.runInContext(source, sandbox);
  return { elements, sockets, contexts, nodes, audio, status, window, frames,
    advance: ms => { now += ms; },
    draw() { for (const [id, fn] of [...frames]) { frames.delete(id); fn(now); } },
    click: () => elements['echo-connect'].fire('click'),
    acknowledge() {
      const socket = sockets.at(-1); socket.readyState = 1; socket.fire('open');
      assert.equal(permit, undefined); // the microphone is requested only after stream acknowledgement
      const request = JSON.parse(socket.sent[0]);
      const { seq, ...metadata } = request;
      socket.fire('message', { data: JSON.stringify({ seq: 1, success: true, stream_id: 42 }) });
      socket.fire('message', { data: JSON.stringify({ ...metadata, command: 'on_stream_start', stream_id: 42 }) });
    },
    permit: () => permit(audio),
    deny: () => deny(new Error("permission denied")),
    deferWorklet: () => { worklet = true; },
    finishWorklet: () => worklet(),
  };
}

(async () => {
  const f = fixture(); f.click();
  assert.equal(f.sockets[0].url.protocol, 'wss:');
  assert.equal(f.sockets[0].url.searchParams.get('channel'), 'random-channel');
  assert.equal(f.sockets[0].url.searchParams.get('echo'), '1');
  f.acknowledge(); await settle(); f.permit(); await settle();
  assert.equal(f.status['echo-status'].message.key, 'Connected');
  const node = f.nodes[0], socket = f.sockets[0], context = f.contexts[0];
  const block = new Float32Array(128).fill(0.5);
  for (let i = 0; i < 6; i++) node.port.onmessage({ data: block });
  const packet = socket.sent[1];
  assert.equal(packet.byteLength, 1545);
  assert.equal(new DataView(packet).getUint32(1), 42);
  assert.equal(new DataView(packet).getInt16(9, true), 16384);
  f.draw();
  assert.equal(f.elements['echo-db'].message, '-6 dB');
  assert.equal(f.elements['echo-meter']['aria-valuenow'], '-6');
  assert.ok(f.elements['echo-meter'].classList.classes.has('hot'));
  f.advance(12.5);
  socket.fire('message', { data: packet });
  assert.equal(f.elements['echo-rtt'].message, undefined); // no per-packet repaint
  assert.equal(context.sources[0].at, 1.032);
  assert.equal(context.sources[0].buffer.getChannelData(0)[0], 0.5);
  f.elements['echo-delay'].value = '5'; f.elements['echo-delay'].fire('input');
  assert.ok(context.sources[0].stopped);
  socket.fire('message', { data: packet });
  assert.equal(context.sources[1].at, 6.032);
  f.elements['echo-delay'].value = '5.5'; f.elements['echo-delay'].fire('input');
  assert.ok(context.sources[1].stopped);
  socket.fire('message', { data: packet });
  assert.equal(context.sources.length, 2);
  assert.equal(f.elements['echo-delay-value'].message.key, 'Disabled');
  for (let i = 0; i < 6; i++) node.port.onmessage({ data: block });
  assert.equal(socket.sent.length, 3); // disabled playback still transmits
  const returned = socket.sent[2].slice(0);
  new DataView(returned).setUint32(5, 1); // echoed packet 1 corresponds to the second send
  f.advance(8);
  socket.fire('message', { data: returned });
  assert.equal(f.elements['echo-rtt'].message, undefined);
  f.advance(479.5); f.draw(); // 500 ms interval contains 12.5 ms and 8 ms echoes
  assert.equal(f.elements['echo-rtt'].message.values.milliseconds, '10.3');
  assert.equal(context.sources.length, 2); // RTT still updates while playback is disabled
  node.port.onmessage({ data: new Float32Array(128).fill(1) });
  f.draw();
  assert.equal(f.elements['echo-db'].message.key, 'CLIP');
  for (let i = 0; i < 5; i++) node.port.onmessage({ data: block });
  const next = socket.sent[3].slice(0);
  new DataView(next).setUint32(5, 2);
  f.advance(20); socket.fire('message', { data: next });
  assert.equal(f.elements['echo-rtt'].message.values.milliseconds, '10.3');
  f.advance(480); f.draw();
  assert.equal(f.elements['echo-rtt'].message.values.milliseconds, '20.0'); // interval resets
  f.advance(500); f.draw();
  assert.equal(f.elements['echo-rtt'].message, '—'); // no samples in the next interval
  f.window.fire('pagehide');
  assert.equal(f.frames.size, 0);
  assert.equal(f.elements['echo-db'].message, '-∞ dB');
  assert.equal(f.elements['echo-rtt'].message, '—');
  assert.ok(f.audio.tracks[0].stopped);
  assert.equal(context.state, 'closed');
  assert.equal(socket.readyState, 3);
  assert.equal(JSON.parse(socket.sent.at(-1)).command, 'stop_stream');
  assert.equal(node.port.onmessage, null);

  for (const stage of ['connecting', 'permission', 'worklet']) {
    const late = fixture();
    if (stage === 'worklet') late.deferWorklet();
    late.click();
    if (stage !== 'connecting') { late.acknowledge(); await settle(); }
    if (stage === 'worklet') { late.permit(); await settle(); }
    late.click(); // Disconnect remains available during setup
    if (stage === 'permission') late.permit();
    if (stage === 'worklet') late.finishWorklet();
    await settle();
    assert.equal(late.contexts[0].state, 'closed');
    assert.equal(late.nodes.length, 0);
    if (stage !== 'connecting') assert.ok(late.audio.tracks[0].stopped);
    assert.equal(late.status['echo-status'].message.key, 'Disconnected');
  }
  const denied = fixture(); denied.click(); denied.acknowledge(); await settle();
  denied.deny(); await settle();
  assert.equal(denied.contexts[0].state, 'closed');
  assert.equal(denied.sockets[0].readyState, 3);
  assert.equal(denied.status['echo-status'].error, true);
  const disconnected = fixture(); disconnected.click(); disconnected.acknowledge(); await settle();
  disconnected.permit(); await settle(); disconnected.sockets[0].close(); await settle();
  assert.ok(disconnected.audio.tracks[0].stopped);
  assert.equal(disconnected.contexts[0].state, 'closed');
  console.log('Echo browser checks passed: PCM, local delay/disable, query parameters and setup/disconnect races.');
})().catch(error => { console.error(error); process.exitCode = 1; });
