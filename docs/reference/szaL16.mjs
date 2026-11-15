// Standalone szaL16 reference. See ../szaL16.md and README.md.
// No microphone acquisition, connection setup, authentication or channel routing.

function format(sampleRate, frameMs, framesPerPacket) {
  const duration = frameMs * framesPerPacket;
  const frameSamples = sampleRate * frameMs / 1000;
  if (!Number.isInteger(sampleRate) || sampleRate < 1 || sampleRate > 65535 ||
      !Number.isInteger(frameMs) || frameMs < 1 || frameMs > 255 ||
      ![1, 2].includes(framesPerPacket) || duration < 2.5 || duration > 60 ||
      !Number.isInteger(frameSamples)) {
    throw new RangeError("Format cannot be represented by the szaL16 header and whole samples");
  }
  return { sampleRate, frameMs, framesPerPacket, duration, samples: frameSamples * framesPerPacket };
}

export function codecHeader(sampleRate, frameMs = 16, framesPerPacket = 1) {
  format(sampleRate, frameMs, framesPerPacket);
  const bytes = new Uint8Array(4);
  new DataView(bytes.buffer).setUint16(0, sampleRate, true);
  bytes[2] = framesPerPacket;
  bytes[3] = frameMs;
  return btoa(String.fromCharCode(...bytes));
}

export function streamFormat(event) {
  if (event.type !== "audio" || event.codec !== "audio/L16") throw new Error("Expected audio/L16");
  const bytes = Uint8Array.from(atob(event.codec_header), c => c.charCodeAt(0));
  if (bytes.length !== 4) throw new Error("Expected a four-byte codec_header");
  const result = format(new DataView(bytes.buffer).getUint16(0, true), bytes[3], bytes[2]);
  if (event.packet_duration !== result.duration) throw new Error("Inconsistent packet_duration");
  return result;
}

// The caller supplies an open browser WebSocket and a unique command sequence number.
// Await this before forwarding samples; it rejects on start failure or connection loss.
export async function startStream(socket, {
  channel, sampleRate, frameMs = 16, framesPerPacket = 1, seq = 1, recipient,
}) {
  const request = { command: "start_stream", seq, channel, type: "audio", codec: "audio/L16",
    codec_header: codecHeader(sampleRate, frameMs, framesPerPacket),
    packet_duration: frameMs * framesPerPacket };
  if (recipient !== undefined) request.for = recipient;
  const reply = await new Promise((resolve, reject) => {
    const cleanup = () => {
      socket.removeEventListener("message", message);
      socket.removeEventListener("close", closed);
      socket.removeEventListener("error", closed);
    };
    const closed = () => { cleanup(); reject(new Error("WebSocket closed during start_stream")); };
    const message = event => {
      if (typeof event.data !== "string") return;
      let value;
      try { value = JSON.parse(event.data); } catch { return; }
      if (value.seq !== seq) return;
      cleanup();
      if (value.success && Number.isInteger(value.stream_id)) resolve(value);
      else reject(new Error(value.error || "start_stream failed"));
    };
    socket.addEventListener("message", message);
    socket.addEventListener("close", closed);
    socket.addEventListener("error", closed);
    try { socket.send(JSON.stringify(request)); } catch (error) { cleanup(); reject(error); }
  });
  return new L16Sender(data => socket.send(data), request, reply.stream_id);
}

export class L16Sender {
  constructor(send, request, streamId) {
    this.send = send;
    this.request = request;
    this.streamId = streamId;
    this.format = streamFormat(request);
    this.position = 0;
    this.closed = false;
    this.packet = this.newPacket();
  }

  newPacket() {
    const packet = new DataView(new ArrayBuffer(9 + this.format.samples * 2));
    packet.setUint8(0, 1);
    packet.setUint32(1, this.streamId); // network order; packet_id stays zero upstream
    return packet;
  }

  // samples: one mono Float32Array from Web Audio, of any block length.
  // Convert directly into the outgoing BE packet, preserving a partial packet across calls.
  push(samples) {
    if (this.closed) throw new Error("Stream is stopped");
    for (let i = 0; i < samples.length; i++) {
      const value = Math.max(-1, Math.min(1, samples[i]));
      this.packet.setInt16(9 + this.position * 2, Math.min(32767, Math.round(value * 32768)));
      if (++this.position === this.format.samples) {
        this.send(this.packet.buffer);
        this.packet = this.newPacket();
        this.position = 0;
      }
    }
  }

  stop() {
    if (this.closed) return;
    this.closed = true;
    if (this.position) this.send(this.packet.buffer); // unused tail is zero/silence
    this.send(JSON.stringify({ command: "stop_stream", channel: this.request.channel,
      stream_id: this.streamId }));
  }
}

// Decodes WebSocket message data; onAudio receives Float32 PCM and stream metadata.
// Set socket.binaryType = "arraybuffer" before wiring its message event here.
export class L16Receiver {
  constructor(onAudio) {
    this.onAudio = onAudio;
    this.streams = new Map();
  }

  receive(data) {
    if (typeof data === "string") {
      const event = JSON.parse(data);
      if (event.command === "on_stream_start") {
        this.streams.set(event.stream_id, { ...event, ...streamFormat(event) });
      } else if (event.command === "on_stream_stop") this.streams.delete(event.stream_id);
      return;
    }
    const packet = ArrayBuffer.isView(data)
      ? new DataView(data.buffer, data.byteOffset, data.byteLength) : new DataView(data);
    if (packet.byteLength < 9 || packet.getUint8(0) !== 1) throw new Error("Invalid audio packet");
    const streamId = packet.getUint32(1);
    const stream = this.streams.get(streamId);
    if (!stream || packet.byteLength !== 9 + stream.samples * 2) throw new Error("Unexpected audio packet");
    const samples = new Float32Array(stream.samples);
    for (let i = 0; i < samples.length; i++) samples[i] = packet.getInt16(9 + i * 2) / 32768;
    this.onAudio(samples, { ...stream, packetId: packet.getUint32(5) });
  }
}

// Small playback adapter: decode to AudioBuffers and schedule consecutive packets.
// Resume the caller-owned AudioContext in a user gesture before receiving audio.
export class L16Player {
  constructor(context, { bufferMs = 32, destination = context.destination } = {}) {
    this.context = context;
    this.destination = destination;
    this.bufferSeconds = bufferMs / 1000;
    this.next = new Map();
    this.sources = new Set();
    this.receiver = new L16Receiver((samples, stream) => {
      const buffer = context.createBuffer(1, samples.length, stream.sampleRate);
      buffer.copyToChannel(samples, 0);
      const source = context.createBufferSource();
      source.buffer = buffer;
      source.connect(this.destination);
      const next = this.next.get(stream.stream_id);
      const at = next === undefined || next < context.currentTime ? context.currentTime + this.bufferSeconds : next;
      this.next.set(stream.stream_id, at + buffer.duration);
      this.sources.add(source);
      source.onended = () => { this.sources.delete(source); source.disconnect(); };
      source.start(at);
    });
  }

  receive(data) {
    if (typeof data === "string") {
      const event = JSON.parse(data);
      if (["on_stream_start", "on_stream_stop"].includes(event.command)) this.next.delete(event.stream_id);
    }
    this.receiver.receive(data); // stop drains already-scheduled audio
  }

  close() {
    for (const source of this.sources) { source.onended = null; source.stop(); source.disconnect(); }
    this.sources.clear();
    this.next.clear();
    this.receiver.streams.clear();
  }
}
