# szaL16 reference implementations

Small, independent examples for the [szaL16 stream specification](../szaL16.md).
Both use `codec: "pcm_s16le"` with mono signed 16-bit little-endian audio. They default
to one **16 ms** frame per packet and accept other durations through
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
It converts floats directly into the outgoing little-endian packet, retaining a partial packet
across calls. It clips to the PCM16 range, sends complete packets as samples arrive, and
zero-pads a partial final packet on `stop()`. The float-to-PCM conversion writes little-endian samples directly, without resampling.
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

[szaL16.py](szaL16.py) uses Python 3.11+ standard-library modules only. Its three functions
are blocking and run in ordinary worker threads. Supply already-open connections with
synchronous `recv()` and `send()`, such as those from
[`websockets.sync`](https://websockets.readthedocs.io/en/stable/reference/sync/server.html).
Each helper consumes **one stream**, then returns. Caller code owns connections and threads.

```python
import threading
from szaL16 import relay, write_wav, read_wav

# Server: run in the publisher's connection-handler thread.
# Select listeners and assign the unique uint32 stream ID outside this function.
relay(publisher, [listener], stream_id=42, sender="alice")

# Receiving client: start before the publisher sends its stream.
receiver = threading.Thread(target=write_wav, args=(listener, "received.wav"))
receiver.start()

# Sending client: the server acknowledges start_stream before samples are transmitted.
stop = threading.Event()
sender = threading.Thread(target=read_wav, args=(publisher, "source.wav"),
                          kwargs={"channel": "example", "frame_ms": 16, "stop": stop})
sender.start()
# To end WAV playback early: stop.set()
sender.join()
```

These illustrate separate server and client roles, rather than operations sharing one
socket. A dedicated receive worker owns each connection's `recv()`. Serialize all sends
on a connection if it is shared between workers, including control messages, because
fragmented messages must finish before another send starts. Close the connection to unblock
workers waiting in `recv()`/`send()`, then join them. Exceptions propagate in the calling
thread; the embedding service decides how to log or report worker errors.

- `relay()` forwards controls and audio to the supplied receivers, assigns downstream
  packet IDs, and sends stop events on completion or failure. It does not decode audio.
  Header bytes and a view of the original payload are sent as fragments of **one** binary
  WebSocket message, using `send(iterable_of_buffers)` as supported by `websockets.sync`.
  A transport exposing only `send(bytes)` must join those fragments instead.
- `write_wav()` passes little-endian PCM directly to Python's `wave` module on little-endian
  hosts, writes incrementally, and finalizes the WAV header even if reception raises.
- `read_wav()` reads mono uncompressed PCM16 WAV and forwards the sample bytes directly on
  little-endian hosts. It paces packets against a monotonic clock using `threading.Event.wait()`;
  the optional `stop` event interrupts pacing and ends the stream. It does not resample or mix
  channels. A short final packet is silence-padded. On big-endian hosts, the WAV helpers use
  a native bulk swap to bridge the `wave` module's native-endian buffers and wire LE samples.

The relay sends to receivers sequentially; a slow receiver therefore slows forwarding.
It has no per-listener queues, overflow policy or reconnection machinery. The payload view
avoids an application-level payload copy; transport/TLS processing can still allocate and copy.
For the low-CPU use case, configure the WebSocket transport with compression disabled
(`compression=None` for `websockets.sync`); the helpers do not configure the caller's transport.

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
A separate round trip runs the sender, relay and WAV writer in three threads; cancellation
checks exercise `threading.Event` and the LE-host WAV path preserves buffer identity.
WebSockets and Web Audio are simulated; these checks do not exercise a browser, physical
audio hardware or a real network connection.
