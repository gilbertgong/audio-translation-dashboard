// Offline wire-format and Web Audio adapter checks: node tests/test_szaL16.mjs
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { codecHeader, streamFormat, startStream, L16Sender, L16Receiver, L16Player } from "../docs/reference/szaL16.mjs";

function fixture(frameMs = 16, framesPerPacket = 1) {
  const request = { command: "start_stream", seq: 7, channel: "example", type: "audio", codec: "pcm_s16le",
    codec_header: codecHeader(16000, frameMs, framesPerPacket), packet_duration: frameMs * framesPerPacket };
  const output = [];
  const sender = new L16Sender(data => output.push(data), request, 42);
  return { request, output, sender };
}

function wireFixture() {
  const f = fixture();
  // The ninth sample exercises rounding just below +1; the final partial packet is padded.
  const samples = Float32Array.from([-1, -0.5, 0, 0.5, 1, -2, 2, 1 / 32768, 0.9999999,
    ...new Array(250).fill(0.25)]);
  for (let i = 0; i < samples.length; i += 128) f.sender.push(samples.subarray(i, i + 128));
  f.sender.stop();
  return { request: f.request, start: { ...f.request, command: "on_stream_start", stream_id: 42, from: "example" },
    packets: f.output.filter(data => typeof data !== "string").map(data => Buffer.from(data).toString("base64")),
    stop: { command: "on_stream_stop", stream_id: 42 } };
}

if (process.argv.includes("--emit")) {
  console.log(JSON.stringify(wireFixture()));
} else if (process.argv.includes("--decode")) {
  const wire = JSON.parse(readFileSync(0, "utf8"));
  const output = [];
  const receiver = new L16Receiver(samples => output.push(...samples.map(value => value * 32768)));
  receiver.receive(JSON.stringify(wire.start));
  for (const packet of wire.packets) receiver.receive(Buffer.from(packet, "base64"));
  receiver.receive(JSON.stringify(wire.stop));
  console.log(JSON.stringify(output));
} else {
  assert.equal(codecHeader(16000), "gD4BEA==");
  const wire = wireFixture();
  assert.equal(wire.start.codec, "pcm_s16le");
  const firstPacket = Buffer.from(wire.packets[0], "base64");
  assert.deepEqual([...firstPacket.subarray(0, 9)], [1, 0, 0, 0, 42, 0, 0, 0, 0]);
  assert.deepEqual([...firstPacket.subarray(9, 19)], [0, 128, 0, 192, 0, 0, 0, 64, 255, 127]);
  const values = [];
  const receiver = new L16Receiver((samples, stream) => {
    assert.equal(stream.sampleRate, 16000);
    assert.equal(stream.stream_id, 42);
    values.push(...samples.map(value => value * 32768));
  });
  receiver.receive(JSON.stringify(wire.start));
  for (const packet of wire.packets) receiver.receive(Buffer.from(packet, "base64"));
  assert.deepEqual(values.slice(0, 9), [-32768, -16384, 0, 16384, 32767, -32768, 32767, 1, 32767]);
  assert.equal(values.length, 512);
  assert.ok(values.slice(9, 259).every(value => value === 8192));
  assert.ok(values.slice(259).every(value => value === 0));
  receiver.receive(JSON.stringify(wire.stop));
  assert.equal(receiver.streams.size, 0);
  assert.throws(() => receiver.receive(new ArrayBuffer(8)), /Invalid audio packet/);
  assert.throws(() => receiver.receive(Buffer.from(wire.packets[0], "base64")), /Unexpected/);
  assert.throws(() => streamFormat({ ...wire.start, codec: "opus" }), /Expected pcm_s16le/);
  assert.throws(() => streamFormat({ ...wire.start, codec: "audio/L16" }), /Expected pcm_s16le/);
  assert.throws(() => streamFormat({ ...wire.start, packet_duration: 20 }), /Inconsistent/);
  assert.throws(() => codecHeader(44100, 16), /whole samples/);
  assert.throws(() => codecHeader(16000, 2.5), /whole samples/);
  assert.equal(streamFormat({ ...wire.start, codec_header: codecHeader(44100, 20), packet_duration: 20 }).samples, 882);

  // Arbitrary input block lengths, alternate durations and two frames per packet.
  for (const [ms, frames] of [[16, 1], [20, 1], [10, 2], [32, 1]]) {
    const f = fixture(ms, frames);
    const count = streamFormat(f.request).samples;
    const input = Float32Array.from({ length: count * 2 }, (_, i) => ((i % 101) - 50) / 128);
    for (let i = 0; i < input.length; i += 127) f.sender.push(input.subarray(i, i + 127));
    assert.equal(f.output.length, 2);
    const decoded = [];
    const r = new L16Receiver(samples => decoded.push(...samples));
    r.receive(JSON.stringify({ ...f.request, command: "on_stream_start", stream_id: 42 }));
    f.output.forEach(packet => r.receive(packet));
    assert.deepEqual(decoded, [...input]);
    assert.equal(new DataView(f.output[0]).getUint32(5), 0);
    f.sender.stop();
    assert.equal(f.output.length, 3); // no extra silence packet on an exact boundary
    f.sender.stop();
    assert.equal(f.output.length, 3);
    assert.throws(() => f.sender.push(input), /stopped/);
  }

  class Socket extends EventTarget {
    sent = [];
    send(data) {
      this.sent.push(data);
      if (typeof data === "string" && JSON.parse(data).command === "start_stream") {
        queueMicrotask(() => {
          const event = new Event("message");
          event.data = JSON.stringify({ seq: 9, success: true, stream_id: 42 });
          this.dispatchEvent(event);
        });
      }
    }
  }
  const socket = new Socket();
  const sender = await startStream(socket, { channel: "example", sampleRate: 16000, seq: 9 });
  sender.push(new Float32Array(256));
  sender.stop();
  assert.equal(socket.sent.length, 3);
  const closing = new EventTarget();
  closing.send = () => queueMicrotask(() => closing.dispatchEvent(new Event("close")));
  await assert.rejects(startStream(closing, { channel: "example", sampleRate: 16000 }), /closed/);

  const sources = [];
  const context = { currentTime: 0, destination: {},
    createBuffer: (channels, count, rate) => ({ duration: count / rate, copyToChannel() {} }),
    createBufferSource: () => {
      const source = { connect() {}, disconnect() {}, start(at) { this.at = at; }, stop() { this.stopped = true; } };
      sources.push(source);
      return source;
    } };
  const player = new L16Player(context);
  player.receive(JSON.stringify(wire.start));
  player.receive(Buffer.from(wire.packets[0], "base64"));
  player.receive(Buffer.from(wire.packets[1], "base64"));
  assert.equal(sources[0].at, 0.032);
  assert.equal(sources[1].at, 0.048);
  context.currentTime = 1; // restart buffering after underflow
  player.receive(Buffer.from(wire.packets[0], "base64"));
  assert.equal(sources[2].at, 1.032);
  player.receive(JSON.stringify(wire.stop));
  assert.equal(player.sources.size, 3); // normal stream stop drains scheduled audio
  player.close();
  assert.ok(sources.every(source => source.stopped));
  assert.equal(player.sources.size, 0);
  console.log("szaL16 JS checks passed: samples, framing, durations, partial blocks, startup and playback scheduling.");
}
