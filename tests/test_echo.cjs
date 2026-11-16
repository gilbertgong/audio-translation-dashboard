// Browser-controller checks without physical audio: node tests/test_echo.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../src/static/component-audio.js'), 'utf8');
const settle = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };

function fixture(splitSettings = { pre_roll_ms: 0 }, codecSupport) {
  class Events {
    events = {};
    addEventListener(name, fn) { (this.events[name] ??= []).push(fn); }
    fire(name, event = {}) { return this.events[name]?.map(fn => fn(event)); }
  }
  const elements = {};
  for (const id of ['echo-connect', 'echo-delay', 'echo-delay-value', 'echo-meter', 'echo-db', 'echo-rtt', 'echo-opus', 'echo-opus-status', 'channel-connect', 'channel-meter', 'channel-db', 'channel-results']) {
    const classes = new Set(), children = {};
    elements[id] = Object.assign(new Events(), { value: '0', style: {},
      querySelector: name => children[name] ??= { style: {} },
      setAttribute(name, value) { this[name] = value; },
      children: [], append(...items) { this.children.push(...items); }, replaceChildren() { this.children = []; },
      classList: { classes, add(...names) { names.forEach(name => classes.add(name)); },
        remove(...names) { names.forEach(name => classes.delete(name)); },
        toggle(name, active) { if (active) classes.add(name); else classes.delete(name); } } });
  }
  const sockets = [], contexts = [], nodes = [], status = {};
  let permit, deny, worklet;
  let now = 100, frameId = 0;
  const frames = new Map();
  let audio = { tracks: [{ stopped: false, stop() { this.stopped = true; } }], getTracks() { return this.tracks; } };
  class Socket extends Events {
    static OPEN = 1;
    readyState = 0;
    sent = [];
    constructor(url) { super(); this.url = url; sockets.push(this); }
    send(data) { this.sent.push(data); }
    close() { this.readyState = 3; this.fire('close'); }
  }
  class Context {
    state = 'suspended';
    currentTime = 1;
    destination = {};
    sources = [];
    audioWorklet = { addModule: () => worklet ? new Promise(resolve => { worklet = resolve; }) : Promise.resolve() };
    constructor(options) { this.sampleRate = options.sampleRate; contexts.push(this); }
    resume() { this.state = 'running'; return Promise.resolve(); }
    close() { this.state = 'closed'; return Promise.resolve(); }
    createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
    createBuffer(channels, count, rate) {
      const samples = new Float32Array(count);
      return { sampleRate: rate, duration: count / rate, getChannelData: () => samples };
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
  const requests = [];
  const encoders = [], decoders = [];
  class AudioData {
    constructor(options) { Object.assign(this, options); this.data = new Float32Array(options.data); }
    close() { this.closed = true; }
    copyTo(target) { target.set(this.data); }
  }
  class AudioEncoder {
    static async isConfigSupported(config) { return { supported: codecSupport?.encode, config }; }
    constructor(callbacks) { Object.assign(this, callbacks); this.frames = []; encoders.push(this); }
    configure(config) { this.config = config; this.state = 'configured'; }
    encode(audio) {
      this.frames.push({ timestamp: audio.timestamp, data: new Float32Array(audio.data) });
      const bytes = new Uint8Array([0xf8, Math.round(audio.data[0] * 100), 0]);
      this.output({ byteLength: bytes.length, copyTo: target => target.set(bytes) });
    }
    flush() { return new Promise((resolve, reject) => { this.finish = resolve; this.reject = reject; }); }
    close() { this.state = 'closed'; }
  }
  class EncodedAudioChunk { constructor(options) { Object.assign(this, options); } }
  class AudioDecoder {
    static async isConfigSupported(config) { return { supported: codecSupport?.decode, config }; }
    constructor(callbacks) { Object.assign(this, callbacks); this.chunks = []; this.pending = []; decoders.push(this); }
    configure(config) { this.config = config; this.state = 'configured'; }
    decode(chunk) {
      this.chunks.push(chunk);
      const output = () => this.output(new AudioData({ timestamp: chunk.timestamp,
        numberOfFrames: 960, sampleRate: 48000, data: new Float32Array(960).fill(chunk.data[1] / 100) }));
      if (this.deferOutput) this.pending.push(output); else output();
    }
    flush() { return new Promise(resolve => { this.finish = () => { this.pending.splice(0).forEach(fn => fn()); resolve(); }; }); }
    close() { this.state = 'closed'; }
  }
  const sandbox = vm.createContext({ console, Blob, URL, DataView, ArrayBuffer, Uint8Array, Math, Set,
    WebSocket: Socket, AudioContext: Context, AudioWorkletNode: Worklet, window,
    document: { getElementById: id => elements[id], createElement: () => ({ children: [],
      append(...items) { this.children.push(...items); } }) },
    AbortController, atob: value => Buffer.from(value, 'base64').toString('binary'),
    crypto: { randomUUID: () => 'random-channel' }, btoa: value => Buffer.from(value, 'binary').toString('base64'),
    performance: { now: () => now },
    requestAnimationFrame: fn => { frames.set(++frameId, fn); return frameId; },
    cancelAnimationFrame: id => frames.delete(id),
    pageFetch: (url, options) => new Promise((resolve, reject) => {
      requests.push({ url, options, resolve });
      options.signal.addEventListener('abort', () => reject(Error('aborted')));
    }), errorMessage: async () => 'request failed',
    sampleRate: 16000, splitSettings, playbackBufferMs: 800,
    audioProcessing: {}, navigator: { mediaDevices: { getUserMedia: () => new Promise((resolve, reject) => { permit = resolve; deny = reject; }) } },
    I18n: { msg: (key, values) => ({ key, values }), text: (element, value) => { element.message = value; },
      attr: (element, name, value) => { element[name] = value; },
      error: message => new Error(message), errorText: error => error.message },
    setStatus: (id, message, error) => { status[id] = { message, error }; },
  });
  if (codecSupport) Object.assign(sandbox, { AudioData, AudioEncoder, AudioDecoder, EncodedAudioChunk });
  for (const file of ['audio-utils.js', 'segment-splitter.js']) vm.runInContext(fs.readFileSync(path.join(__dirname, '../src/static/' + file), 'utf8'), sandbox);
  sandbox.resample = async samples => samples; // conversion is separately exercised; focus here on serialization
  vm.runInContext(source, sandbox);
  return { elements, sockets, contexts, nodes, encoders, decoders, get audio() { return audio; }, status, window, frames, requests,
    advance: ms => { now += ms; },
    draw() { for (const [id, fn] of [...frames]) { frames.delete(id); fn(now); } },
    click: (prefix = 'echo') => elements[prefix + '-connect'].fire('click'),
    acknowledge() {
      const socket = sockets.at(-1); socket.readyState = 1; socket.fire('open');
      assert.equal(socket.sent.length, 0); // opening a socket does not start an audio stream
    },
    ackStream(id = 42) {
      const socket = sockets.at(-1);
      const request = socket.sent.filter(item => typeof item === 'string')
        .map(item => JSON.parse(item)).filter(item => item.command === 'start_stream').at(-1);
      const { seq, ...metadata } = request;
      socket.fire('message', { data: JSON.stringify({ seq, success: true, stream_id: id }) });
      if (socket.url.searchParams.get('echo') === '1') socket.fire('message', {
        data: JSON.stringify({ ...metadata, command: 'on_stream_start', stream_id: id }) });
    },
    permit: () => {
      if (audio.tracks[0].stopped) audio = { tracks: [{ stopped: false, stop() { this.stopped = true; } }], getTracks() { return this.tracks; } };
      permit(audio);
    },
    deny: () => deny(new Error("permission denied")),
    deferWorklet: () => { worklet = true; },
    finishWorklet: () => worklet(),
  };
}

(async () => {
  const f = fixture(); f.click();
  assert.equal(f.elements['echo-opus-status'].message.key, 'Opus unavailable');
  assert.equal(f.elements['echo-opus'].disabled, true);
  assert.equal(f.sockets[0].url.protocol, 'wss:');
  assert.equal(f.sockets[0].url.searchParams.get('channel'), 'random-channel');
  assert.equal(f.sockets[0].url.searchParams.get('echo'), '1');
  f.acknowledge(); await settle(); f.permit(); await settle();
  assert.equal(f.status['echo-status'].message.key, 'Connected: {codec}');
  assert.equal(f.status['echo-status'].message.values.codec, 'PCM');
  const node = f.nodes[0], socket = f.sockets[0], context = f.contexts[0];
  assert.equal(context.sampleRate, 16000);
  for (let i = 0; i < 60; i++) node.port.onmessage({ data: new Float32Array(128) });
  assert.equal(socket.sent.length, 0); // calibration and silence transmit nothing
  const block = new Float32Array(128).fill(0.5);
  for (let i = 0; i < 2; i++) node.port.onmessage({ data: block });
  const start = JSON.parse(socket.sent[0]);
  assert.equal(Buffer.from(start.codec_header, 'base64').readUInt16LE(0), 16000);
  assert.equal(start.packet_duration, 16);
  assert.equal(socket.sent.length, 1); // PCM waits for the start acknowledgement
  f.ackStream();
  const packet = socket.sent[1];
  assert.equal(packet.byteLength, 521); // 9-byte header + 256 mono PCM16 samples
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
  assert.equal(context.sources[0].buffer.duration, 0.016);
  f.elements['echo-delay'].value = '5'; f.elements['echo-delay'].fire('input');
  assert.ok(context.sources[0].stopped);
  socket.fire('message', { data: packet });
  assert.equal(context.sources[1].at, 6.032);
  f.elements['echo-delay'].value = '5.5'; f.elements['echo-delay'].fire('input');
  assert.ok(context.sources[1].stopped);
  socket.fire('message', { data: packet });
  assert.equal(context.sources.length, 2);
  assert.equal(f.elements['echo-delay-value'].message.key, 'Disabled');
  for (let i = 0; i < 2; i++) node.port.onmessage({ data: block });
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
  node.port.onmessage({ data: block });
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
  const channel = fixture();
  channel.click(); channel.acknowledge(); await settle(); channel.permit(); await settle();
  const previous = channel.sockets[0];
  channel.click('channel');
  assert.equal(previous.readyState, 3);
  assert.ok(channel.audio.tracks[0].stopped);
  assert.equal(channel.status['echo-status'].message.key, 'Disconnected');
  channel.acknowledge(); await settle(); channel.permit(); await settle();
  const ws = channel.sockets.at(-1), capture = channel.nodes.at(-1);
  assert.equal(ws.url.searchParams.get('echo'), '0');
  const quiet = new Float32Array(128), loud = new Float32Array(128).fill(0.1);
  const feed = (data, n) => { for (let i = 0; i < n; i++) capture.port.onmessage({ data }); };
  feed(quiet, 160);
  feed(loud, 120); channel.ackStream(); feed(quiet, 120); await settle();
  assert.equal(Buffer.from(JSON.parse(ws.sent[0]).codec_header, 'base64').readUInt16LE(0), 16000);
  feed(loud, 120); channel.ackStream(43); feed(quiet, 120); await settle();
  assert.equal(channel.requests.length, 1); // second WAV is queued, never parallel
  assert.ok(ws.sent.some(item => item instanceof ArrayBuffer)); // raw PCM path also runs
  const rawPost = '{ "message_id": 501,\n "text": "POST text must not be displayed" }';
  channel.requests[0].resolve({ ok: true, text: async () => rawPost });
  await settle(); assert.equal(channel.requests.length, 2);
  const rawTranscription = JSON.stringify({ command: 'on_transcription', stream_id: 77, text: 'hello',
    language: 'en', translations: [{ language: 'zh', message: '你好' }], stt_seconds: 0.1, llm_seconds: 0.2 }, null, 2)
    .replace('你好', '\\u4f60\\u597d');
  ws.fire('message', { data: rawTranscription });
  assert.equal(channel.elements['channel-results'].children.length, 1); // POST diagnostics precede stream/message link
  const postOnly = channel.elements['channel-results'].children[0];
  assert.ok(!postOnly.children[0].textContent); // POST text does not populate the transcription
  assert.ok(!postOnly.children[1].textContent);
  const postSection = postOnly.children[5];
  assert.equal(postSection.hidden, false);
  assert.equal(postSection.children[0].message.key, 'POST response');
  assert.equal(postSection.children[1].textContent, JSON.stringify(JSON.parse(rawPost), null, 2));
  const ttsHeader = Buffer.from([0xc0, 0x5d, 1, 16]).toString('base64');
  const rawStart = JSON.stringify({ command: 'on_stream_start', message_id: 501, stream_id: 77,
    codec: 'pcm_s16le', codec_header: ttsHeader, packet_duration: 16 });
  ws.fire('message', { data: rawStart });
  assert.equal(channel.elements['channel-results'].children.length, 1);
  const result = channel.elements['channel-results'].children[0];
  assert.equal(result.children[0].textContent, 'hello');
  assert.equal(result.children[1].textContent, '你好');
  const rawMessages = result.children[4].children[1].children;
  assert.deepEqual(rawMessages.map(message => message.textContent),
    [rawTranscription, rawStart].map(raw => JSON.stringify(JSON.parse(raw), null, 2)));
  const speech = new ArrayBuffer(777), view = new DataView(speech);
  view.setUint8(0, 1); view.setUint32(1, 77); view.setInt16(9, 16384, true);
  ws.fire('message', { data: speech });
  assert.equal(channel.contexts.at(-1).sources[0].at, 1.8);
  assert.equal(channel.contexts.at(-1).sources[0].buffer.getChannelData(0)[0], 0.5);
  assert.equal(channel.contexts.at(-1).sources[0].buffer.sampleRate, 24000);
  const rawStop = '{ "command": "on_stream_stop", "stream_id": 77 }';
  ws.fire('message', { data: rawStop });
  assert.equal(rawMessages.at(-1).textContent, JSON.stringify(JSON.parse(rawStop), null, 2));
  ws.fire('message', { data: JSON.stringify({ command: 'on_stream_start', message_id: 502, stream_id: 78,
    codec: 'pcm_s16le', codec_header: ttsHeader, packet_duration: 16 }) });
  ws.fire('message', { data: JSON.stringify({ command: 'on_transcription', stream_id: 78, text: 'second',
    language: 'en', translations: [{ language: 'zh', message: '第二' }], stt_seconds: 0.1, llm_seconds: 0.2 }) });
  assert.equal(channel.elements['channel-results'].children.length, 2);
  const secondResult = channel.elements['channel-results'].children[1];
  assert.equal(secondResult.children[0].textContent, 'second');
  assert.equal(secondResult.children[1].textContent, '第二');
  ws.fire('message', { data: JSON.stringify({ command: 'on_error', message_id: 502, stream_id: 78, error: 'TTS interrupted' }) });
  assert.deepEqual(secondResult.children[4].children[1].children.map(message => JSON.parse(message.textContent).command),
    ['on_stream_start', 'on_transcription', 'on_error']); // each received response appears once
  // TTS can fail before announcing its stream; text must still join the error's message ID.
  ws.fire('message', { data: JSON.stringify({ command: 'on_transcription', stream_id: 79, text: 'retained',
    language: 'en', translations: [{ language: 'zh', message: '保留' }], stt_seconds: 0.1, llm_seconds: 0.2 }) });
  ws.fire('message', { data: JSON.stringify({ command: 'on_error', message_id: 503, stream_id: 79, error: 'TTS failed' }) });
  assert.equal(channel.elements['channel-results'].children.length, 3);
  const failedSpeech = channel.elements['channel-results'].children[2];
  assert.equal(failedSpeech.children[0].textContent, 'retained');
  assert.equal(failedSpeech.children[1].textContent, '保留');
  assert.equal(failedSpeech.children[3].message.values.error, 'TTS failed');
  feed(loud, 120); feed(quiet, 120); await settle();
  channel.click('echo'); // drops remaining WAVs, cancels in-flight POST and stops translated speech
  assert.equal(ws.readyState, 3);
  assert.equal(channel.requests[1].options.signal.aborted, true);
  await settle(); assert.equal(channel.requests.length, 2);
  assert.equal(channel.contexts[1].sources[0].stopped, true);
  channel.window.fire('pagehide'); await settle();

  // A whole phrase can end before its start acknowledgement; a later phrase must
  // wait its turn without losing pre-roll, tail, or the final partial packet.
  const delayed = fixture({ pre_roll_ms: 24, max_phrase_s: 0.05, split_pause_ms: 16 });
  delayed.click(); delayed.acknowledge(); await settle(); delayed.permit(); await settle();
  const delayedNode = delayed.nodes[0], delayedWs = delayed.sockets[0];
  const push = (value, n) => {
    for (let i = 0; i < n; i++) delayedNode.port.onmessage({ data: new Float32Array(128).fill(value) });
  };
  const commands = () => delayedWs.sent.filter(item => typeof item === 'string').map(item => JSON.parse(item));
  push(0, 60); push(0.25, 10);
  assert.equal(commands().length, 1); // maximum WAV phrase length does not stop WS speech
  push(0, 32); // configured silence ends the first speech stream, even before its ack
  push(0.5, 3); push(0, 32);
  assert.equal(delayedWs.sent.length, 1); // second start must follow first audio + stop
  delayed.ackStream(101);
  assert.deepEqual(commands().map(item => item.command), ['start_stream', 'stop_stream', 'start_stream']);
  assert.equal(commands()[1].stream_id, 101);
  assert.equal(commands()[2].seq, 2);
  const firstAudio = delayedWs.sent.filter(item => item instanceof ArrayBuffer);
  assert.equal(new DataView(firstAudio[0]).getInt16(9, true), 0); // retained pre-roll
  assert.equal(new DataView(firstAudio[1]).getInt16(9, true), 8192); // speech starts after 16 ms
  delayed.ackStream(102);
  assert.deepEqual(commands().map(item => item.command), ['start_stream', 'stop_stream', 'start_stream', 'stop_stream']);
  const secondAudio = delayedWs.sent.filter(item => item instanceof ArrayBuffer && new DataView(item).getUint32(1) === 102);
  assert.equal(secondAudio.length, 3); // shortened pause threshold: 6 worklet blocks -> 3 full packets
  assert.equal(new DataView(secondAudio[0]).getUint32(5), 0); // packet IDs restart per stream
  assert.equal(new DataView(secondAudio.at(-1)).getUint32(5), 2);
  assert.equal(new DataView(secondAudio.at(-1)).getInt16(519, true), 0);
  // Packet zero from two different streams must retain distinct RTT timestamps.
  delayed.advance(20);
  const receiveAudio = (packet, streamId) => {
    delayedWs.fire('message', { data: JSON.stringify({ ...commands()[0], command: 'on_stream_start', stream_id: streamId }) });
    delayedWs.fire('message', { data: packet });
  };
  receiveAudio(firstAudio[0], 101);
  delayed.advance(10); receiveAudio(secondAudio[0], 102);
  delayed.advance(470); delayed.draw();
  assert.equal(delayed.elements['echo-rtt'].message.values.milliseconds, '25.0');
  const afterPause = delayedWs.sent.length;
  push(0, 100);
  assert.equal(delayedWs.sent.length, afterPause); // stay connected without sending silence
  assert.equal(delayedWs.readyState, 1);
  assert.equal(delayed.audio.tracks[0].stopped, false);
  delayed.window.fire('pagehide');

  for (const support of [{ encode: true, decode: false }, { encode: false, decode: true }]) {
    const unavailable = fixture(undefined, support); await settle();
    assert.equal(unavailable.elements['echo-opus'].disabled, true);
    assert.equal(unavailable.elements['echo-opus-status'].message.key, 'Opus unavailable');
  }
  const opus = fixture({ pre_roll_ms: 0, split_pause_ms: 16 }, { encode: true, decode: true });
  await settle();
  assert.equal(opus.elements['echo-opus-status'].message.key, 'Opus available');
  assert.equal(opus.elements['echo-opus'].disabled, false);
  opus.elements['echo-opus'].fire('click');
  opus.click(); opus.acknowledge(); await settle(); opus.permit(); await settle();
  assert.equal(opus.status['echo-status'].message.values.codec, 'opus');
  assert.equal(opus.elements['echo-opus'].disabled, false);
  const opusNode = opus.nodes[0], opusWs = opus.sockets[0];
  const opusPush = (value, n) => {
    for (let i = 0; i < n; i++) opusNode.port.onmessage({ data: new Float32Array(128).fill(value) });
  };
  opusPush(0, 60); opusPush(0.5, 3);
  assert.equal(opusWs.sent.length, 1); // wait for stream ID before encoding/sending
  const opusStart = JSON.parse(opusWs.sent[0]);
  assert.equal(opusStart.codec, 'opus'); assert.equal(opusStart.packet_duration, 20);
  assert.deepEqual([...Buffer.from(opusStart.codec_header, 'base64')], [0x80, 0x3e, 1, 20]);
  opus.ackStream(201);
  const encoded = opusWs.sent[1];
  assert.equal(encoded.byteLength, 12); // variable-size compressed payload, not PCM
  assert.deepEqual([...new Uint8Array(encoded, 9)], [0xf8, 50, 0]);
  assert.equal(opus.encoders[0].frames[0].data.length, 320);
  assert.equal(opus.encoders[0].frames[0].timestamp, 0);
  opus.advance(12); opusWs.fire('message', { data: encoded }); await settle();
  assert.equal(opus.contexts[0].sources[0].buffer.sampleRate, 48000); // decoder's actual rate
  assert.equal(opus.contexts[0].sources[0].buffer.duration, 0.02);
  assert.equal(opus.contexts[0].sources[0].buffer.getChannelData(0)[0], 0.5);
  opusPush(0, 2); // 640 samples, two 20 ms frames, then asynchronous encoder flush
  assert.equal(opusWs.sent.filter(item => typeof item === 'string').length, 1);
  opusPush(0.25, 3); // new speech queues while previous encoder is flushing
  opus.encoders[0].finish(); await settle();
  assert.equal(opus.encoders[0].state, 'closed');
  const opusCommands = opusWs.sent.filter(item => typeof item === 'string').map(item => JSON.parse(item));
  assert.deepEqual(opusCommands.map(item => item.command), ['start_stream', 'stop_stream', 'start_stream']);
  assert.equal(opus.encoders[0].frames[1].timestamp, 20000);
  opus.decoders[0].deferOutput = true;
  opusWs.fire('message', { data: encoded }); await settle();
  opus.elements['echo-delay'].value = '1'; opus.elements['echo-delay'].fire('input');
  opusWs.fire('message', { data: JSON.stringify({ command: 'on_stream_stop', stream_id: 201 }) });
  await settle(); opus.decoders[0].finish(); await settle();
  assert.equal(opus.contexts[0].sources.length, 1); // old pending decoded audio discarded on delay change
  opus.ackStream(202);
  opusPush(0, 2); // second encoder has a partial final frame to pad
  assert.equal(opus.encoders[1].frames.at(-1).data.at(-1), 0);
  opus.click(); // disconnect during encoder flush
  assert.equal(opus.encoders[1].state, 'closed');
  const beforeLateFlush = opusWs.sent.length;
  opus.encoders[1].finish(); await settle();
  assert.equal(opusWs.sent.length, beforeLateFlush);
  assert.equal(opus.decoders[1].state, 'closed');
  assert.equal(opus.elements['echo-opus'].disabled, false);
  opus.elements['echo-opus'].fire('click');
  opus.click(); opus.acknowledge(); await settle(); opus.permit(); await settle();
  for (let i = 0; i < 60; i++) opus.nodes.at(-1).port.onmessage({ data: new Float32Array(128) });
  opus.nodes.at(-1).port.onmessage({ data: new Float32Array(128).fill(0.5) });
  const pcmAgain = JSON.parse(opus.sockets.at(-1).sent[0]);
  assert.equal(pcmAgain.codec, 'pcm_s16le'); assert.equal(pcmAgain.packet_duration, 16);
  opus.window.fire('pagehide');

  // Codec changes during speech apply after the natural pause, on the same socket. A pending
  // acknowledgement or native encoder/decoder flush must preserve their ordering.
  const hot = fixture({ pre_roll_ms: 0, split_pause_ms: 250 }, { encode: true, decode: true });
  await settle(); hot.click(); hot.acknowledge(); await settle(); hot.permit(); await settle();
  const hotWs = hot.sockets[0], hotNode = hot.nodes[0];
  const hotPush = (value, n) => {
    for (let i = 0; i < n; i++) hotNode.port.onmessage({ data: new Float32Array(128).fill(value) });
  };
  const hotCommands = () => hotWs.sent.filter(item => typeof item === 'string').map(item => JSON.parse(item));
  hotPush(0, 60); hotPush(0.5, 3);
  hot.elements['echo-opus'].fire('click'); // PCM stream's acknowledgement is still pending
  assert.equal(hot.elements['echo-opus']['aria-pressed'], 'true');
  assert.equal(hot.status['echo-status'].message.values.codec, 'PCM');
  hotPush(0.5, 3);
  assert.equal(hotCommands().length, 1); // toggling does not cut speech into a new stream
  hotPush(0, 32);
  hotPush(0.25, 3);
  assert.equal(hotCommands().length, 1);
  hot.ackStream(301);
  assert.deepEqual(hotCommands().map(item => item.command), ['start_stream', 'stop_stream', 'start_stream']);
  assert.equal(hotCommands()[0].codec, 'pcm_s16le');
  assert.equal(hotCommands()[2].codec, 'opus');
  assert.equal(hotCommands()[2].packet_duration, 20);
  assert.equal(hot.status['echo-status'].message.values.codec, 'opus');
  hotWs.fire('message', { data: JSON.stringify({ command: 'on_stream_stop', stream_id: 301 }) });
  hot.ackStream(302);
  const hotOpus = hotWs.sent.find(item => item instanceof ArrayBuffer && new DataView(item).getUint32(1) === 302);
  hot.decoders[0].deferOutput = true;
  hotWs.fire('message', { data: hotOpus }); await settle();
  hot.elements['echo-opus'].fire('click'); // selection changes; current speech remains Opus
  assert.equal(hot.status['echo-status'].message.values.codec, 'opus');
  assert.equal(hot.elements['echo-opus']['aria-pressed'], 'false');
  hotPush(0, 32);
  hotPush(0.75, 2);
  assert.equal(hotCommands().length, 3);
  hot.encoders[0].finish(); await settle();
  assert.equal(hot.status['echo-status'].message.values.codec, 'PCM');
  assert.deepEqual(hotCommands().map(item => item.command), ['start_stream', 'stop_stream', 'start_stream', 'stop_stream', 'start_stream']);
  assert.equal(hotCommands()[4].codec, 'pcm_s16le');
  assert.equal(hotCommands()[4].packet_duration, 16);
  hotWs.fire('message', { data: JSON.stringify({ command: 'on_stream_stop', stream_id: 302 }) });
  await settle(); hot.ackStream(303);
  const hotPcm = hotWs.sent.find(item => item instanceof ArrayBuffer && new DataView(item).getUint32(1) === 303);
  hotWs.fire('message', { data: hotPcm }); await settle();
  assert.equal(hot.contexts[0].sources.length, 0); // PCM waits for earlier decoded Opus
  hot.decoders[0].finish(); await settle();
  assert.equal(hot.contexts[0].sources[0].buffer.getChannelData(0)[0], 0.25);
  assert.equal(hot.contexts[0].sources[1].buffer.getChannelData(0)[0], 0.75);
  assert.equal(hot.sockets.length, 1); assert.equal(hot.contexts.length, 1);
  assert.equal(hotWs.readyState, 1); assert.equal(hot.audio.tracks[0].stopped, false);
  hot.window.fire('pagehide');
  console.log('Echo browser checks passed: PCM, local delay/disable, query parameters and setup/disconnect races.');
})().catch(error => { console.error(error); process.exitCode = 1; });
