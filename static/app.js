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
// Active recording: { stream, ctx, source, node, chunks, splitter }. splitter is
// set in auto-split mode (and chunks unused); otherwise all audio goes to chunks.
let session = null;
let pending = 0; // recordings still being processed

function setStatus(text) {
  statusEl.textContent = text;
}

// Status line when it isn't simply "Recording...": idle/processing, or the
// auto-split listening state.
function updateStatus() {
  if (session?.splitter) {
    const sp = session.splitter;
    const state = sp.calibrating ? "Listening (measuring background)..."
      : sp.speaking ? "Speaking..." : "Listening...";
    setStatus(pending ? `${state} · processing ${pending}` : state);
  } else if (!session) {
    setStatus(pending ? `Processing (${pending})...` : "Idle");
  }
}

// Microphone level meter, running while recording. Audio blocks from the
// recorder feed meterSample(); drawMeter() shows the loudest level since the
// previous frame (fast rise, slow fall), a marker at the highest peak of the
// whole recording (reset by startMeter), and a clipping warning when samples
// reach full scale.

const meterEl = document.getElementById("level-meter");
const meterFill = meterEl.querySelector(".meter-fill");
const meterPeak = meterEl.querySelector(".meter-peak");
const meterThreshold = meterEl.querySelector(".meter-threshold");
const meterDb = document.getElementById("level-db");
const METER_FLOOR_DB = -60;
const METER_QUIET_DB = -45; // below this: too quiet
const METER_HOT_DB = -10; // above this: very loud
const METER_CLIP_MS = 1500; // how long the clipping warning stays up
const meter = { rms: 0, peak: 0, shownDb: METER_FLOOR_DB, maxDb: METER_FLOOR_DB,
                clipUntil: 0, frame: 0 };

function meterSample(samples) {
  let sum = 0;
  let peak = 0;
  for (let i = 0; i < samples.length; i++) {
    sum += samples[i] * samples[i];
    peak = Math.max(peak, Math.abs(samples[i]));
  }
  const rms = Math.sqrt(sum / samples.length);
  meter.rms = Math.max(meter.rms, rms);
  meter.peak = Math.max(meter.peak, peak);
  return rms;
}

const toDb = (x) => (x > 0 ? Math.max(METER_FLOOR_DB, 20 * Math.log10(x)) : METER_FLOOR_DB);
const dbToPercent = (db) => ((db - METER_FLOOR_DB) / -METER_FLOOR_DB) * 100;

function drawMeter(now) {
  const db = toDb(meter.rms);
  meter.shownDb = db > meter.shownDb ? db : Math.max(db, meter.shownDb - 1); // fall ~60 dB/s
  meter.maxDb = Math.max(meter.maxDb, toDb(meter.peak)); // highest peak this recording
  if (meter.peak >= 0.99) meter.clipUntil = now + METER_CLIP_MS;
  meter.rms = 0;
  meter.peak = 0;

  const clipping = now < meter.clipUntil;
  meterFill.style.width = `${dbToPercent(meter.shownDb)}%`;
  meterPeak.style.left = `calc(${dbToPercent(meter.maxDb)}% - 2px)`;
  meterEl.classList.toggle("clip", clipping);
  meterEl.classList.toggle("hot", !clipping && meter.shownDb > METER_HOT_DB);
  meterEl.classList.toggle("quiet", meter.shownDb < METER_QUIET_DB);
  meterEl.setAttribute("aria-valuenow", String(Math.round(meter.shownDb)));
  meterDb.classList.toggle("clip", clipping);
  meterDb.textContent = clipping ? "CLIP"
    : meter.shownDb <= METER_FLOOR_DB ? "-∞ dB" : `${Math.round(meter.shownDb)} dB`;

  // Auto-split: show the current speech threshold and whether speech is detected.
  const sp = session?.splitter;
  meterEl.classList.toggle("auto", !!sp);
  meterEl.classList.toggle("speaking", !!sp?.speaking);
  if (sp) meterThreshold.style.left = `calc(${dbToPercent(sp.thresholdDb)}% - 1px)`;

  meter.frame = requestAnimationFrame(drawMeter);
}

function startMeter() {
  Object.assign(meter, { rms: 0, peak: 0, shownDb: METER_FLOOR_DB, maxDb: METER_FLOOR_DB,
                         clipUntil: 0 });
  meterEl.classList.add("active");
  meter.frame = requestAnimationFrame(drawMeter);
}

function stopMeter() {
  cancelAnimationFrame(meter.frame);
  meterEl.classList.remove("active", "quiet", "hot", "clip", "auto", "speaking");
  meterEl.setAttribute("aria-valuenow", String(METER_FLOOR_DB));
  meterFill.style.width = "0";
  meterDb.classList.remove("clip");
  meterDb.textContent = "";
}

// Auto-split on pauses. Every audio block updates a simple loudness-based
// voice activity detector: "speech" is anything clearly above the background
// noise level, which is tracked continuously (falling quickly, rising slowly).
// A phrase starts at the first loud block (plus some pre-roll audio), and ends
// after pause_ms of quiet or at max_phrase_s; it is then sent like a manual
// recording. Settings come from [auto_split] in config.toml.

const autoSplitToggle = document.getElementById("auto-split");
const autoSplit = {
  pause_ms: 600, threshold_db: 12, min_threshold_db: -50, pre_roll_ms: 300,
  tail_ms: 200, min_speech_ms: 300, max_phrase_s: 25,
};
const CALIBRATION_MS = 400; // measure background noise only, at the start
const NOISE_FALL_MS = 50; // background estimate follows quieter levels quickly...
const NOISE_RISE_MS = 2000; // ...and louder ones slowly (more slowly mid-phrase)

function createSplitter(sampleRate) {
  return {
    sampleRate,
    elapsedMs: 0,
    calibrating: true,
    noiseDb: null, // background level estimate
    thresholdDb: autoSplit.min_threshold_db,
    speaking: false,
    preRoll: [], // recent blocks kept while waiting for speech
    preRollSamples: 0,
    blocks: [], // the current phrase
    phraseSamples: 0,
    speechMs: 0, // loud time in the current phrase
    silenceMs: 0, // quiet time since the last loud block
  };
}

// Feed one audio block (Float32Array) and its RMS level.
function splitterBlock(sp, block, rms) {
  const ms = (block.length / sp.sampleRate) * 1000;
  const db = toDb(rms);
  sp.elapsedMs += ms;

  // Track the background noise level.
  if (sp.noiseDb === null) sp.noiseDb = db;
  const tau = db < sp.noiseDb ? NOISE_FALL_MS : NOISE_RISE_MS * (sp.speaking ? 4 : 1);
  sp.noiseDb += (db - sp.noiseDb) * (1 - Math.exp(-ms / tau));
  sp.thresholdDb = Math.max(sp.noiseDb + autoSplit.threshold_db, autoSplit.min_threshold_db);

  if (sp.calibrating) {
    if (sp.elapsedMs < CALIBRATION_MS) return;
    sp.calibrating = false;
    updateStatus();
  }
  const loud = db > sp.thresholdDb;

  if (!sp.speaking) {
    sp.preRoll.push(block);
    sp.preRollSamples += block.length;
    const keep = (autoSplit.pre_roll_ms / 1000) * sp.sampleRate;
    while (sp.preRoll.length > 1 && sp.preRollSamples - sp.preRoll[0].length >= keep) {
      sp.preRollSamples -= sp.preRoll.shift().length;
    }
    if (loud) {
      Object.assign(sp, { speaking: true, blocks: sp.preRoll, phraseSamples: sp.preRollSamples,
                          speechMs: ms, silenceMs: 0, preRoll: [], preRollSamples: 0 });
      updateStatus();
    }
    return;
  }

  sp.blocks.push(block);
  sp.phraseSamples += block.length;
  if (loud) {
    sp.speechMs += ms;
    sp.silenceMs = 0;
  } else {
    sp.silenceMs += ms;
  }
  if (sp.silenceMs >= autoSplit.pause_ms) endPhrase(sp, "pause");
  else if (sp.phraseSamples / sp.sampleRate >= autoSplit.max_phrase_s) endPhrase(sp, "max length");
}

// Finish the current phrase and send it (if it had enough speech).
// reason: "pause", "max length" (speech continues into a new phrase) or "stop".
function endPhrase(sp, reason) {
  if (!sp.speaking) return;
  let samples = concat(sp.blocks);
  if (reason === "pause") {
    // Keep only tail_ms of the trailing silence.
    const extraMs = Math.max(0, sp.silenceMs - autoSplit.tail_ms);
    const extra = Math.floor((extraMs / 1000) * sp.sampleRate);
    samples = samples.subarray(0, Math.max(0, samples.length - extra));
  }
  const speechMs = sp.speechMs;
  const continuing = reason === "max length";
  Object.assign(sp, { speaking: continuing, blocks: [], phraseSamples: 0, speechMs: 0, silenceMs: 0 });
  updateStatus();

  if (speechMs < autoSplit.min_speech_ms) return; // too little speech: a click or cough
  sendPhrase(samples, sp.sampleRate, reason);
}

async function sendPhrase(samples, sampleRate, reason) {
  try {
    const resampled = await resample(samples, sampleRate, targetSampleRate);
    processRecording(encodeWav(resampled, targetSampleRate), `continuous mode (${reason})`);
  } catch (err) {
    console.error(err);
    addNote(`Error: ${err.message}`, true);
  }
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

// Playback of a row's recording: the exact audio sent to whisper, kept in the
// browser. One shared player, so starting one recording stops another.
const player = new Audio();
let playingBtn = null;

function stopPlayback() {
  player.pause();
  if (playingBtn) {
    playingBtn.textContent = "▶";
    playingBtn.title = "Play this recording";
    playingBtn = null;
  }
}

player.addEventListener("ended", stopPlayback);

// Add a play/stop button to a row's "audio" timing line.
function addPlayButton(row, wav) {
  const url = URL.createObjectURL(wav);
  const btn = el("button", "row-btn", "▶");
  btn.type = "button";
  btn.title = "Play this recording";
  btn.addEventListener("click", () => {
    const wasPlaying = playingBtn === btn;
    stopPlayback();
    if (wasPlaying) return;
    player.src = url;
    player.play().catch((err) => {
      console.error(err);
      stopPlayback();
    });
    playingBtn = btn;
    btn.textContent = "■";
    btn.title = "Stop playback";
  });
  row.querySelector('.timing[data-kind="audio"]').appendChild(btn);
}

// Raw whisper/llama exchanges: a "{ }" button on a timing line opens a dialog
// with the request and full response as formatted JSON.
const rawDialog = document.getElementById("raw-dialog");
const rawTitle = document.getElementById("raw-title");
const rawBody = document.getElementById("raw-body");
const rawCopy = document.getElementById("raw-copy");

function showRaw(title, raw) {
  rawTitle.textContent = title;
  rawBody.textContent = JSON.stringify(raw, null, 2);
  rawCopy.textContent = "Copy";
  rawDialog.showModal();
  rawBody.scrollTop = 0;
}

// kind: "stt" or "llm" (the timing line to put the button on).
function addRawButton(row, kind, title, raw) {
  const btn = el("button", "row-btn", "{ }");
  btn.type = "button";
  btn.title = `Show ${title}`;
  btn.addEventListener("click", () => showRaw(title, raw));
  row.querySelector(`.timing[data-kind="${kind}"]`).appendChild(btn);
}

document.getElementById("raw-close").addEventListener("click", () => rawDialog.close());
rawDialog.addEventListener("click", (e) => {
  if (e.target === rawDialog) rawDialog.close(); // click on the backdrop
});
rawCopy.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(rawBody.textContent);
    rawCopy.textContent = "Copied";
  } catch (err) {
    console.error(err);
    rawCopy.textContent = "Copy failed";
  }
});

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
    const { enabled, ...settings } = config.auto_split || {};
    Object.assign(autoSplit, settings);
    autoSplitToggle.checked = !!enabled;
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
    // Silence of the listed length, so the play button looks and behaves as usual.
    const silence = new Float32Array(Math.round(item.audio_seconds * targetSampleRate));
    addPlayButton(row, encodeWav(silence, targetSampleRate));
    setTiming(row, "stt", item.stt_seconds);
    setTiming(row, "llm", item.llm_seconds);
    addRawButton(row, "stt", "whisper response (mock)", { mock: true, text: item.text, language: item.language });
    addRawButton(row, "llm", "llama response (mock)", { mock: true, text: item.text, translation: item.translation });
    addDetail(row, "mock sample");
  }
}

// auto: split on pauses (each phrase is sent as it ends) instead of keeping
// all audio for one recording.
async function startRecording(auto) {
  stopPlayback(); // speakers playing into the mic would end up in the recording
  const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1 } });
  const ctx = new AudioContext();
  try {
    await ctx.audioWorklet.addModule("recorder-worklet.js");
    const source = ctx.createMediaStreamSource(stream);
    const node = new AudioWorkletNode(ctx, "recorder");
    const chunks = [];
    const splitter = auto ? createSplitter(ctx.sampleRate) : null;
    node.port.onmessage = (e) => {
      const rms = meterSample(e.data);
      if (splitter) splitterBlock(splitter, e.data, rms);
      else chunks.push(e.data);
    };
    source.connect(node);
    node.connect(ctx.destination); // keeps the node processing; it outputs silence
    session = { stream, ctx, source, node, chunks, splitter };
    startMeter();
  } catch (err) {
    stream.getTracks().forEach((t) => t.stop());
    ctx.close();
    throw err;
  }
}

// Returns the recording as a WAV Blob; in auto-split mode, sends any phrase in
// progress and returns null.
async function stopRecording() {
  const { stream, ctx, source, node, chunks, splitter } = session;
  session = null;
  stopMeter();
  source.disconnect();
  node.disconnect();
  stream.getTracks().forEach((t) => t.stop());
  const inputRate = ctx.sampleRate;
  await ctx.close();

  if (splitter) {
    endPhrase(splitter, "stop");
    return null;
  }
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
// note: optional detail for the activity cell (e.g. how an auto-split phrase ended).
async function processRecording(wav, note = "") {
  const audioSeconds = (wav.size - 44) / 2 / targetSampleRate;
  const row = addRow();
  setMeta(row, new Date().toLocaleTimeString());
  setLine(row, ".original", "Transcribing...", "pending");
  setTiming(row, "audio", audioSeconds);
  addPlayButton(row, wav);
  setTiming(row, "stt", null);
  if (note) addDetail(row, note);
  pending++;
  updateStatus();
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
    if (stt.stt_raw) addRawButton(row, "stt", "whisper request and response", stt.stt_raw);
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
      if (result.llm_raw) addRawButton(row, "llm", "llama request and response", result.llm_raw);
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
    updateStatus();
  }
}

recordBtn.addEventListener("click", async () => {
  recordBtn.disabled = true;
  try {
    if (!session) {
      const auto = autoSplitToggle.checked;
      autoSplitToggle.disabled = true; // no mode change mid-recording
      await startRecording(auto);
      recordBtn.textContent = auto ? "Stop Listening" : "Stop Recording";
      recordBtn.classList.add("recording");
      if (auto) updateStatus();
      else setStatus("Recording...");
    } else {
      recordBtn.textContent = "Start Recording";
      recordBtn.classList.remove("recording");
      const wav = await stopRecording();
      autoSplitToggle.disabled = false;
      updateStatus();
      if (wav) processRecording(wav); // not awaited: runs in the background
    }
  } catch (err) {
    console.error(err);
    addNote(`Error: ${err.message}`, true);
    setStatus("Error");
    stopMeter();
    if (session) {
      session.stream.getTracks().forEach((t) => t.stop());
      session.ctx.close();
      session = null;
    }
    autoSplitToggle.disabled = false;
    recordBtn.textContent = "Start Recording";
    recordBtn.classList.remove("recording");
  } finally {
    recordBtn.disabled = false;
  }
});

// Space starts/stops recording from anywhere on the page. It takes over Space
// from focused buttons and switches (which would otherwise also be toggled);
// Enter still activates buttons. Text fields keep Space for typing.
function isSpaceShortcut(e) {
  if (e.code !== "Space" || e.ctrlKey || e.altKey || e.metaKey) return false;
  if (rawDialog.open) return false; // Space scrolls/acts inside the viewer instead
  const target = e.target;
  const isTextInput = target.tagName === "INPUT" && !["checkbox", "radio", "button"].includes(target.type);
  return !(target.isContentEditable || isTextInput || ["TEXTAREA", "SELECT"].includes(target.tagName));
}

document.addEventListener("keydown", (e) => {
  if (!isSpaceShortcut(e)) return;
  e.preventDefault(); // no page scroll, no click on a focused button
  if (!e.repeat) recordBtn.click(); // holding Space toggles only once
});

// A focused button is "clicked" on Space key-up; block that too.
document.addEventListener("keyup", (e) => {
  if (isSpaceShortcut(e)) e.preventDefault();
});

loadConfig();
