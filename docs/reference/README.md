# szaL16 reference implementations

Small, independent examples for the [szaL16 stream specification](../szaL16.md).
Both default to one **16 ms** frame per packet and accept other durations through
the existing header fields. They do not implement connection setup, authentication,
channel membership or receiver selection.

The code is original to this reference; the adapted protocol documentation attributes
Zello and includes its [upstream license](../zello_LICENSE).

## JavaScript: Web Audio samples and playback

[szaL16.mjs](szaL16.mjs) is a browser ES module with no dependencies.

```js
import { startStream, L16Player } from "./szaL16.mjs";

// socket is already open; context is the AudioContext supplying the captured samples.
// Choose seq uniquely among pending commands on this connection.
const sender = await startStream(socket, {
  channel: "example", sampleRate: context.sampleRate, seq: 1,
  frameMs: 16, framesPerPacket: 1,
});

// recorder is an existing AudioWorkletNode forwarding one mono Float32Array per message.
// Install after the start reply: earlier capture blocks are not queued by this helper.
recorder.port.onmessage = ({ data }) => sender.push(data);

// When recording ends:
recorder.port.onmessage = null;
sender.stop();
```

`L16Sender.push()` accepts any block length, including 128-sample AudioWorklet blocks.
It converts floats directly into the outgoing big-endian packet, retaining a partial packet
across calls. It clips to the PCM16 range, sends complete packets as samples arrive, and
zero-pads a partial final packet on `stop()`. No resampling or intermediate LE audio is used.
Feed samples at their actual capture rate; this sender does not pace a preloaded recording.
It uses the browser's normal WebSocket send queue; the caller monitors `bufferedAmount`
and decides how to handle a slow connection.

For receiving and playback on a listener connection:

```js
socket.binaryType = "arraybuffer";
// Resume context from a user gesture before receiving audio.
await context.resume();
const player = new L16Player(context); // 32 ms initial/rebuffering delay
const receive = ({ data }) => player.receive(data);
socket.addEventListener("message", receive);

// When disposing the listener (not at every on_stream_stop):
socket.removeEventListener("message", receive);
player.close(); // stops scheduled sources; leaves the caller's AudioContext open
```

Install the receive handler before the server sends `on_stream_start`. `L16Receiver`
can also be used directly: its callback receives a mono `Float32Array` and stream metadata
including the sample rate and packet ID. `L16Player` feeds those samples into AudioBuffers
at the declared rate, schedules consecutive buffers, and re-buffers after an underflow.
Normal stream stop drains already-scheduled audio. Concurrent streams are mixed by Web Audio.
This is basic playback scheduling, without adaptive jitter buffering or clock-drift correction.

## Python: relay and WAV conversion

[szaL16.py](szaL16.py) uses Python 3.11+ standard-library modules only. Supply already-open
async WebSocket connections with `recv()` and `send()`, such as those provided by
the [`websockets` library](https://websockets.readthedocs.io/en/stable/reference/asyncio/server.html).
Each helper consumes **one stream** on a dedicated connection, then returns. Caller code
manages connections, cancellation, routing and any other control messages.

```python
from szaL16 import relay, write_wav, read_wav

# Server: publisher sends start_stream/data/stop_stream; listeners receive events/data.
# Select listeners and assign the unique uint32 stream ID outside this function.
await relay(publisher, [listener], stream_id=42, sender="alice")

# Receiving client: run on its listener connection as the stream arrives.
samples_written = await write_wav(listener, "received.wav")

# Sending client: the peer acknowledges start_stream before samples are transmitted.
await read_wav(publisher, "source.wav", channel="example", frame_ms=16)
```

These illustrate the separate server and client roles, not sequential operations on the
same socket. A receiving client must be listening before the publisher starts.

- `relay()` forwards controls and audio to the supplied receivers, assigns downstream
  packet IDs, and sends stop events on completion or failure. It does not decode audio.
  Header bytes and a view of the original payload are sent as fragments of **one** binary
  WebSocket message, using `send(iterable_of_buffers)` as supported by `websockets`.
  A transport exposing only `send(bytes)` must join those fragments instead.
- `write_wav()` converts received big-endian samples to the format expected by Python's
  `wave` module, writes incrementally, and finalizes the WAV header even if reception raises.
- `read_wav()` reads mono uncompressed PCM16 WAV, converts samples to big-endian in bulk,
  and sends packets paced against a monotonic clock. It does not convert other encodings,
  mix channels or resample. A short final packet is silence-padded.

The relay awaits receivers' sends; a slow receiver therefore slows forwarding. It has no
per-listener queues, overflow policy or reconnection machinery. The payload view avoids an
application-level payload copy; transport/TLS processing can still allocate and copy.
For the low-CPU use case, configure the WebSocket transport with compression disabled
(`compression=None` for `websockets`); the helpers do not configure the caller's transport.

## Duration and final-packet details

The inherited `frame_size_ms` is an unsigned byte, so these references encode whole
milliseconds. Although the upstream duration table mentions 2.5 ms, its four-byte header
does not describe a fractional-millisecond representation; these helpers do not invent one.
Sample rate and frame duration must also produce an integral sample count: 44.1 kHz works
with 20 ms, but not exactly with 16 ms. Such combinations raise rather than silently resample
or round the rate/duration. The packet-duration range and one/two frames per packet follow
the documented fields.

Silence-padding maintains the advertised packet duration; it can add less than one packet
of trailing silence. There is no original-sample-count extension, so WAV output includes it.
Files, permission errors, malformed messages and transport failures propagate to the caller.

## Verification

From the repository root, with Node.js and Python available:

```text
node tests/test_szaL16.mjs
python tests/test_szaL16.py
```

Checks cover known sample values and clipping, byte order, 128-sample and irregular input
blocks, alternate durations, two frames per packet, partial tails, relay payload reuse and
packet numbering, start/stop handling, WAV header finalization and basic playback scheduling.
Python invokes Node for JS-to-WAV and WAV-to-JS interoperability using temporary files.
WebSockets and Web Audio are simulated; these checks do not exercise a browser, physical
audio hardware or a real network connection.
