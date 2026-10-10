// Dashboard front end: microphone capture -> 16-bit mono WAV -> POST to backend,
// which transcribes it; the text is then sent back for translation. Each
// recording becomes a row in the translation window: the conversation text on
// the left, its activity (timings first, then details) on the right.

const recordBtn = document.getElementById("record-btn");
const statusEl = document.getElementById("status");
const windowEl = document.getElementById("translation-window");
const activityToggle = document.getElementById("activity-toggle");

let targetSampleRate = 16000;
let language2 = ""; // entries detected in this language are right-aligned
let session = null; // active recording: { stream, ctx, source, node, chunks }
let pending = 0; // recordings still being processed

function setStatus(text) {
  statusEl.textContent = text;
}

function updateIdleStatus() {
  if (!session) setStatus(pending ? `Processing (${pending})...` : "Idle");
}

function el(tag, className, text = "") {
  const node = document.createElement(tag);
  node.className = className;
  node.textContent = text;
  return node;
}

// Rows: one per recording (or note). Returns the row element; its cells are
// .utterance (left: time/language, text) and .activity (right: timings, details).

function addRow() {
  const placeholder = windowEl.querySelector(".placeholder");
  if (placeholder) placeholder.remove();
  const row = el("div", "row");

  const utterance = el("div", "utterance");
  utterance.append(el("div", "meta"), el("div", "original"), el("div", "translation"));

  const activity = el("div", "activity");
  activity.append(el("div", "timings"), el("div", "details"));

  row.append(utterance, activity);
  windowEl.appendChild(row);
  windowEl.scrollTop = windowEl.scrollHeight;
  return row;
}

// Set one line (".original" or ".translation") of a row's text.
// state: "pending", "error", or "" for normal text.
function setLine(row, selector, text, state = "") {
  const line = row.querySelector(selector);
  line.textContent = text;
  line.classList.toggle("pending", state === "pending");
  line.classList.toggle("error", state === "error");
  windowEl.scrollTop = windowEl.scrollHeight;
}

// The small line above a row's text: the time; setLanguage adds the language.
function setMeta(row, time) {
  row.querySelector(".meta").textContent = time;
}

// Record the detected language: shown in the meta line, and rows in
// language2 are right-aligned.
function setLanguage(row, language) {
  const meta = row.querySelector(".meta");
  if (language) meta.textContent += ` · ${language}`;
  const isLang2 = !!language && language.toLowerCase() === language2.toLowerCase();
  row.querySelector(".utterance").classList.toggle("lang2", isLang2);
}

// Set a timing in a row's activity cell, listed in the order first set:
// "audio" (recording length), "stt" (transcription), "llm" (translation).
// seconds: a number, null while in progress, or "failed".
const TIMING_LABELS = { audio: "audio", stt: "transcribe", llm: "translate" };
const TIMING_DECIMALS = { audio: 1, stt: 2, llm: 2 };

function setTiming(row, kind, seconds) {
  const timings = row.querySelector(".timings");
  let timing = timings.querySelector(`[data-kind="${kind}"]`);
  if (!timing) {
    timing = el("div", "timing");
    timing.dataset.kind = kind;
    timing.append(el("span", "secs"), el("span", "label", TIMING_LABELS[kind]));
    timings.appendChild(timing);
  }
  const secs = timing.querySelector(".secs");
  timing.classList.toggle("pending", seconds === null);
  timing.classList.toggle("error", seconds === "failed");
  if (seconds === null) secs.textContent = "…";
  else if (seconds === "failed") secs.textContent = "failed";
  else secs.textContent = `${Number(seconds).toFixed(TIMING_DECIMALS[kind])}s`;
}

// Add a line of supporting detail to a row's activity cell.
function addDetail(row, text, isError = false) {
  const detail = el("div", isError ? "error" : "", text);
  row.querySelector(".details").appendChild(detail);
  return detail;
}

// A row with only an activity note (e.g. a microphone error), no conversation text.
function addNote(text, isError = false) {
  const row = addRow();
  addDetail(row, new Date().toLocaleTimeString());
  addDetail(row, text, isError);
}

// Activity column visibility, remembered per browser.

function setActivityHidden(hidden) {
  document.body.classList.toggle("activity-hidden", hidden);
  activityToggle.setAttribute("aria-expanded", String(!hidden));
  const label = hidden ? "Show activity" : "Hide activity";
  activityToggle.setAttribute("aria-label", label);
  activityToggle.title = label;
  try {
    localStorage.setItem("activityHidden", hidden ? "1" : "0");
  } catch {}
}

activityToggle.addEventListener("click", () => {
  setActivityHidden(!document.body.classList.contains("activity-hidden"));
});

try {
  setActivityHidden(localStorage.getItem("activityHidden") === "1");
} catch {}

async function loadConfig() {
  try {
    const res = await fetch("/api/config");
    const config = await res.json();
    targetSampleRate = config.sample_rate;
    language2 = config.language2 || "";
    if (config.mock) showMockConversation(config.mock_conversation);
  } catch (err) {
    console.warn("Could not load config, using defaults", err);
  }
}

// Mock mode (app.py --mock): show the start of the sample conversation, built
// the same way as real entries, so the page can be styled without whisper or llama.
function showMockConversation(items) {
  const badge = el("span", "mock-badge", "MOCK");
  document.querySelector("header h1").append(" ", badge);
  for (const item of items) {
    const row = addRow();
    setMeta(row, new Date().toLocaleTimeString());
    setLanguage(row, item.language);
    setLine(row, ".original", item.text);
    setLine(row, ".translation", item.translation);
    setTiming(row, "audio", item.audio_seconds);
    setTiming(row, "stt", item.stt_seconds);
    setTiming(row, "llm", item.llm_seconds);
    addDetail(row, "mock sample");
  }
}

async function startRecording() {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1 } });
  const ctx = new AudioContext();
  try {
    await ctx.audioWorklet.addModule("recorder-worklet.js");
    const source = ctx.createMediaStreamSource(stream);
    const node = new AudioWorkletNode(ctx, "recorder");
    const chunks = [];
    node.port.onmessage = (e) => chunks.push(e.data);
    source.connect(node);
    node.connect(ctx.destination); // keeps the node processing; it outputs silence
    session = { stream, ctx, source, node, chunks };
  } catch (err) {
    stream.getTracks().forEach((t) => t.stop());
    ctx.close();
    throw err;
  }
}

async function stopRecording() {
  const { stream, ctx, source, node, chunks } = session;
  session = null;
  source.disconnect();
  node.disconnect();
  stream.getTracks().forEach((t) => t.stop());
  const inputRate = ctx.sampleRate;
  await ctx.close();

  const samples = concat(chunks);
  if (samples.length === 0) throw new Error("No audio captured");
  const resampled = await resample(samples, inputRate, targetSampleRate);
  return encodeWav(resampled, targetSampleRate);
}

function concat(chunks) {
  const total = chunks.reduce((n, c) => n + c.length, 0);
  const out = new Float32Array(total);
  let offset = 0;
  for (const c of chunks) {
    out.set(c, offset);
    offset += c.length;
  }
  return out;
}

// Let the browser's own resampler convert to the target rate.
async function resample(samples, fromRate, toRate) {
  if (fromRate === toRate) return samples;
  const length = Math.ceil((samples.length * toRate) / fromRate);
  const offline = new OfflineAudioContext(1, length, toRate);
  const buffer = offline.createBuffer(1, samples.length, fromRate);
  buffer.copyToChannel(samples, 0);
  const src = offline.createBufferSource();
  src.buffer = buffer;
  src.connect(offline.destination);
  src.start();
  return (await offline.startRendering()).getChannelData(0);
}

// Mono 16-bit PCM WAV.
function encodeWav(samples, sampleRate) {
  const dataSize = samples.length * 2;
  const view = new DataView(new ArrayBuffer(44 + dataSize));
  const writeStr = (offset, s) => {
    for (let i = 0; i < s.length; i++) view.setUint8(offset + i, s.charCodeAt(i));
  };

  writeStr(0, "RIFF");
  view.setUint32(4, 36 + dataSize, true);
  writeStr(8, "WAVE");
  writeStr(12, "fmt ");
  view.setUint32(16, 16, true);             // fmt chunk size
  view.setUint16(20, 1, true);              // PCM
  view.setUint16(22, 1, true);              // mono
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true); // byte rate
  view.setUint16(32, 2, true);              // block align
  view.setUint16(34, 16, true);             // bits per sample
  writeStr(36, "data");
  view.setUint32(40, dataSize, true);

  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return new Blob([view], { type: "audio/wav" });
}

// Returns the response body; on failure, throws an Error that also carries
// the body (which may include the saved file's path).
async function uploadWav(wav) {
  const res = await fetch("/api/recordings", {
    method: "POST",
    headers: { "Content-Type": "audio/wav" },
    body: wav,
  });
  const body = await res.json();
  if (!res.ok) throw Object.assign(new Error(body.error || `HTTP ${res.status}`), { body });
  return body;
}

async function requestTranslation(text) {
  const res = await fetch("/api/translate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  const body = await res.json();
  if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
  return body;
}

// Shows a saved file by name; the full path is in the tooltip.
function addFileDetail(row, path) {
  const detail = addDetail(row, path.split(/[\\/]/).pop());
  detail.title = path;
}

// Transcribe, then translate, in the background so the next recording can start.
async function processRecording(wav) {
  const audioSeconds = (wav.size - 44) / 2 / targetSampleRate;
  const row = addRow();
  setMeta(row, new Date().toLocaleTimeString());
  setLine(row, ".original", "Transcribing...", "pending");
  setTiming(row, "audio", audioSeconds);
  setTiming(row, "stt", null);
  pending++;
  updateIdleStatus();
  try {
    let stt;
    try {
      stt = await uploadWav(wav);
    } catch (err) {
      setTiming(row, "stt", "failed");
      setLine(row, ".original", `Error: ${err.message}`, "error");
      if (err.body?.file) addFileDetail(row, err.body.file);
      throw err;
    }
    setTiming(row, "stt", stt.stt_seconds);
    addFileDetail(row, stt.file);
    setLanguage(row, stt.language);
    if (!stt.text) {
      setLine(row, ".original", "(no speech detected)", "pending");
      return;
    }
    setLine(row, ".original", stt.text);

    setLine(row, ".translation", "Translating...", "pending");
    setTiming(row, "llm", null);
    try {
      const result = await requestTranslation(stt.text);
      setTiming(row, "llm", result.llm_seconds);
      setLine(row, ".translation", result.translation);
    } catch (err) {
      setTiming(row, "llm", "failed");
      setLine(row, ".translation", `Error: ${err.message}`, "error");
      throw err;
    }
  } catch (err) {
    console.error(err);
  } finally {
    pending--;
    updateIdleStatus();
  }
}

recordBtn.addEventListener("click", async () => {
  recordBtn.disabled = true;
  try {
    if (!session) {
      await startRecording();
      recordBtn.textContent = "Stop Recording";
      recordBtn.classList.add("recording");
      setStatus("Recording...");
    } else {
      recordBtn.textContent = "Start Recording";
      recordBtn.classList.remove("recording");
      const wav = await stopRecording();
      processRecording(wav); // not awaited: runs in the background
    }
  } catch (err) {
    console.error(err);
    addNote(`Error: ${err.message}`, true);
    setStatus("Error");
    if (session) {
      session.stream.getTracks().forEach((t) => t.stop());
      session.ctx.close();
      session = null;
    }
    recordBtn.textContent = "Start Recording";
    recordBtn.classList.remove("recording");
  } finally {
    recordBtn.disabled = false;
  }
});

loadConfig();
