// Component test page: transcribe, translate and speak one at a time, through
// the dashboard's own endpoints. Results can be passed down the chain
// ("Translate this", "Speak this"), and sample texts help test a language you
// don't speak.

const $ = (id) => document.getElementById(id);
let sampleRate = 16000; // what the dashboard sends whisper; from /api/config
let audioProcessing = {};

const SAMPLES = [
  ["English", "Hello, this is a test. The meeting has moved to three o'clock this afternoon, so please don't be late."],
  ["Chinese", "你好，这是一个测试。今天下午的会议改到三点了，请不要迟到。"],
  ["Mixed", "我们明天用Zoom开会。Please send the slides before 10 a.m."],
];

for (const span of document.querySelectorAll(".samples")) {
  for (const [label, text] of SAMPLES) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = label;
    btn.title = text;
    btn.addEventListener("click", () => { $(span.dataset.target).value = text; });
    span.append(btn);
  }
}

function setStatus(id, text, isError = false) {
  $(id).textContent = text;
  $(id).classList.toggle("error", isError);
}

async function errorMessage(res) {
  try {
    return (await res.json()).error || `HTTP ${res.status}`;
  } catch {
    return `HTTP ${res.status}`;
  }
}

const secs = (ms) => `${(ms / 1000).toFixed(2)}s`;

// --- Transcribe: record (or pick a file), convert to the dashboard's WAV
// format in the browser, and POST it as the dashboard does.

let recorder = null;
let sentWav = null;

$("stt-record").addEventListener("click", async () => {
  if (recorder) {
    recorder.stop();
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        autoGainControl: audioProcessing.auto_gain_control ?? false,
        noiseSuppression: audioProcessing.noise_suppression ?? true,
        echoCancellation: audioProcessing.echo_cancellation ?? true,
      },
    });
    const chunks = [];
    recorder = new MediaRecorder(stream);
    recorder.ondataavailable = (e) => chunks.push(e.data);
    recorder.onstop = () => {
      stream.getTracks().forEach((t) => t.stop());
      recorder = null;
      $("stt-record").textContent = "Record";
      $("stt-record").classList.remove("active");
      transcribe(new Blob(chunks));
    };
    recorder.start();
    $("stt-record").textContent = "Stop";
    $("stt-record").classList.add("active");
    setStatus("stt-status", "Recording...");
  } catch (err) {
    setStatus("stt-status", `Microphone: ${err.message}`, true);
  }
});

$("stt-file").addEventListener("change", (e) => {
  const file = e.target.files[0];
  e.target.value = ""; // so picking the same file again still triggers
  if (file) transcribe(file);
});

// Any audio the browser can decode -> mono, resampled -> 16-bit PCM WAV.
async function toWav(blob) {
  const ctx = new AudioContext();
  let decoded;
  try {
    decoded = await ctx.decodeAudioData(await blob.arrayBuffer());
  } finally {
    ctx.close();
  }
  const length = Math.ceil(decoded.duration * sampleRate);
  const offline = new OfflineAudioContext(1, length, sampleRate); // mixes channels down to mono
  const src = offline.createBufferSource();
  src.buffer = decoded;
  src.connect(offline.destination);
  src.start();
  const samples = (await offline.startRendering()).getChannelData(0);
  return encodeWav(samples, sampleRate);
}

function encodeWav(samples, rate) {
  const view = new DataView(new ArrayBuffer(44 + samples.length * 2));
  const text = (offset, s) => [...s].forEach((c, i) => view.setUint8(offset + i, c.charCodeAt(0)));
  text(0, "RIFF");
  view.setUint32(4, 36 + samples.length * 2, true);
  text(8, "WAVEfmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, rate, true);
  view.setUint32(28, rate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  text(36, "data");
  view.setUint32(40, samples.length * 2, true);
  samples.forEach((s, i) => view.setInt16(44 + i * 2, Math.max(-1, Math.min(1, s)) * 0x7fff, true));
  return new Blob([view], { type: "audio/wav" });
}

async function transcribe(blob) {
  setStatus("stt-status", "Converting...");
  try {
    sentWav = await toWav(blob);
  } catch (err) {
    setStatus("stt-status", `Could not decode the audio: ${err.message}`, true);
    return;
  }
  const audioSeconds = (sentWav.size - 44) / 2 / sampleRate;
  setStatus("stt-status", "Transcribing...");
  const start = performance.now();
  try {
    const res = await fetch("/api/recordings", { method: "POST", headers: { "Content-Type": "audio/wav" }, body: sentWav });
    if (!res.ok) throw new Error(await errorMessage(res));
    const result = await res.json();
    $("stt-text").textContent = result.text || "(no speech detected)";
    $("stt-meta").textContent = `${result.language || "?"} · ${audioSeconds.toFixed(1)}s audio · ` +
      `whisper ${result.stt_seconds}s · round trip ${secs(performance.now() - start)} · saved as ${result.file}`;
    $("stt-raw").textContent = JSON.stringify(result.stt_raw ?? result, null, 2);
    $("stt-result").hidden = false;
    setStatus("stt-status", "");
  } catch (err) {
    setStatus("stt-status", `Transcription failed: ${err.message}`, true);
  }
}

$("stt-play").addEventListener("click", () => {
  if (sentWav) new Audio(URL.createObjectURL(sentWav)).play();
});

$("stt-to-llm").addEventListener("click", () => {
  $("llm-input").value = $("stt-text").textContent;
  $("llm").scrollIntoView({ behavior: "smooth" });
});

// --- Translate: the dashboard's streamed endpoint (our "request" event, then
// llama's server-sent events unchanged).

$("llm-go").addEventListener("click", async () => {
  const text = $("llm-input").value.trim();
  if (!text) return;
  $("llm-go").disabled = true;
  setStatus("llm-status", "Translating...");
  $("llm-result").hidden = false;
  $("llm-text").textContent = "";
  $("llm-meta").textContent = "";
  const raw = [];
  const start = performance.now();
  let first = null;
  try {
    const res = await fetch("/api/translate", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }),
    });
    if (!res.ok) throw new Error(await errorMessage(res));
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let translation = "";
    for (;;) {
      const { value, done } = await reader.read();
      buffer = (buffer + decoder.decode(value, { stream: !done })).replace(/\r\n/g, "\n");
      let end;
      while ((end = buffer.indexOf("\n\n")) >= 0) {
        const event = buffer.slice(0, end);
        buffer = buffer.slice(end + 2);
        raw.push(event);
        const data = event.split("\n").filter((l) => l.startsWith("data:")).map((l) => l.slice(5).trim()).join("\n");
        if (event.startsWith("event: error")) throw new Error(JSON.parse(data).error);
        if (!data.startsWith("{") || event.startsWith("event:")) continue;
        const piece = JSON.parse(data).choices?.[0]?.delta?.content;
        if (piece) {
          first ??= performance.now() - start;
          translation += piece;
          $("llm-text").textContent = withoutTag(translation).text;
        }
      }
      if (done) break;
    }
    const { text: translated, tag } = withoutTag(translation);
    $("llm-text").textContent = translated || "(empty translation)";
    $("llm-meta").textContent = `language tag ${tag ? `[${tag}]` : "(none)"} · ` +
      `first token ${first === null ? "-" : secs(first)} · total ${secs(performance.now() - start)}`;
    setStatus("llm-status", "");
  } catch (err) {
    setStatus("llm-status", `Translation failed: ${err.message}`, true);
  } finally {
    $("llm-raw").textContent = raw.join("\n\n");
    $("llm-go").disabled = false;
  }
});

// llama ends each translation with a language tag, e.g. "Okay. [chinese]"
// (see [llama_prompt]); split it off, as the dashboard does. A tag that's only
// partly streamed in is hidden too.
function withoutTag(translation) {
  const match = /\s*\[([^\]\n]*)\]?\s*$/.exec(translation);
  return match ? { text: translation.slice(0, match.index).trim(), tag: match[1].trim() }
    : { text: translation.trim(), tag: "" };
}

$("llm-to-tts").addEventListener("click", () => {
  $("tts-input").value = $("llm-text").textContent;
  $("tts").scrollIntoView({ behavior: "smooth" });
});

// --- Speak: stream raw PCM from /api/speak and play each piece as it arrives
// (as the dashboard does); keep it all for "Play again" and the WAV download.

let speech = null; // { ctx, controller }
let lastWav = null;

function stopSpeech() {
  if (!speech) return;
  speech.controller.abort();
  speech.ctx.close();
  speech = null;
  $("tts-go").textContent = "Speak";
  $("tts-go").classList.remove("active");
}

$("tts-go").addEventListener("click", async () => {
  if (speech) {
    stopSpeech();
    setStatus("tts-status", "Stopped");
    return;
  }
  const text = $("tts-input").value.trim();
  if (!text) return;
  const current = { ctx: new AudioContext(), controller: new AbortController() };
  speech = current;
  $("tts-go").textContent = "Stop";
  $("tts-go").classList.add("active");
  setStatus("tts-status", "Generating...");
  const start = performance.now();
  let first = null;
  const pieces = [];
  try {
    const res = await fetch("/api/speak", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, voice: $("tts-voice").value }), signal: current.controller.signal,
    });
    if (!res.ok) throw new Error(await errorMessage(res));
    const rate = Number(/rate=(\d+)/.exec(res.headers.get("Content-Type") || "")?.[1]) || 24000;
    const reader = res.body.getReader();
    let leftover = new Uint8Array(0);
    let playAt = 0;
    let last = null;
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      const bytes = new Uint8Array(leftover.length + value.length);
      bytes.set(leftover);
      bytes.set(value, leftover.length);
      const even = bytes.length & ~1;
      leftover = bytes.slice(even);
      if (!even) continue;
      const view = new DataView(bytes.buffer, 0, even);
      const samples = new Float32Array(even / 2);
      for (let i = 0; i < samples.length; i++) samples[i] = view.getInt16(i * 2, true) / 32768;
      pieces.push(samples);
      const buffer = current.ctx.createBuffer(1, samples.length, rate);
      buffer.copyToChannel(samples, 0);
      const source = new AudioBufferSourceNode(current.ctx, { buffer });
      source.connect(current.ctx.destination);
      if (!last) {
        first = performance.now() - start;
        setStatus("tts-status", "Playing...");
      }
      playAt = Math.max(playAt, current.ctx.currentTime + (last ? 0 : 0.1));
      source.start(playAt);
      playAt += buffer.duration;
      last = source;
    }
    if (!last) throw new Error("no audio received");
    const total = pieces.reduce((n, p) => n + p.length, 0);
    const all = new Float32Array(total);
    pieces.reduce((offset, p) => (all.set(p, offset), offset + p.length), 0);
    lastWav = encodeWav(all, rate);
    $("tts-download").href = URL.createObjectURL(lastWav);
    $("tts-meta").textContent = `voice ${$("tts-voice").value} · first audio ${secs(first)} · ` +
      `all received ${secs(performance.now() - start)} · ${(total / rate).toFixed(1)}s audio`;
    $("tts-result").hidden = false;
    last.onended = () => {
      if (speech === current) {
        stopSpeech();
        setStatus("tts-status", "");
      }
    };
  } catch (err) {
    if (speech !== current) return; // stopped
    stopSpeech();
    setStatus("tts-status", `Speech failed: ${err.message}`, true);
  }
});

$("tts-replay").addEventListener("click", () => {
  if (lastWav) new Audio(URL.createObjectURL(lastWav)).play();
});

// --- Setup: the dashboard's settings and the TTS server's voices.

async function init() {
  try {
    const config = await (await fetch("/api/config")).json();
    sampleRate = config.sample_rate;
    audioProcessing = config.audio_processing || {};
    if (config.mock) document.querySelector("header h1").insertAdjacentHTML("beforeend", ' <span class="mock-badge">MOCK</span>');
  } catch (err) {
    console.warn("Could not load config, using defaults", err);
  }
  const select = $("tts-voice");
  try {
    const res = await fetch("/api/voices");
    if (!res.ok) throw new Error(await errorMessage(res));
    const { voices, default: def } = await res.json();
    for (const name of voices.includes(def) ? voices : [def, ...voices]) {
      select.add(new Option(name === def ? `${name} (default)` : name, name, false, name === def));
    }
  } catch (err) {
    select.add(new Option("(default)", ""));
    setStatus("tts-status", `Voices: ${err.message}`, true);
  }
}

init();
