// Dashboard front end: microphone capture -> 16-bit mono WAV -> POST to backend,
// which transcribes it; the text is then sent back for translation. Each
// recording becomes a row in the translation window: the conversation text on
// the left, its activity (timings first, then details) on the right.

const recordBtn = document.getElementById("record-btn");
const windowEl = document.getElementById("translation-window");
const activityToggle = document.getElementById("activity-toggle");
const pageLifetime = new AbortController();

// Navigation cancels requests, including speech with its own cancellation signal.
function pageFetch(url, options = {}) {
  return fetch(url, { ...options, signal: options.signal
    ? AbortSignal.any([pageLifetime.signal, options.signal]) : pageLifetime.signal });
}

let targetSampleRate = 16000;
let language1 = ""; // the two languages to translate between ([languages])
let language2 = ""; // entries detected in this language are right-aligned
let languageChecks = true; // whether Whisper can recheck the configured pair (also used by smoke tests)
// Active recording: { stream, ctx, source, node, chunks, splitter }. splitter is
// set in auto-split mode (and chunks unused); otherwise all audio goes to chunks.
let session = null;
let capture = null; // a running audio capture (see Capture Audio, below)
const pending = { mic: 0, capture: 0 }; // each source's phrases still being processed

// The status row: three independent, unlabeled indicators (kept short; the
// tooltip names each and spells it out). Capture and Mic: each source's own
// session (off, calibrating = measuring the background, listening, speech; or
// for a manual recording, recording) and how many of its phrases are still
// being transcribed and translated. Playback: automatic playback's queue, which
// waits for both to be quiet (see playNextSpeech), or what's playing.
function setIndicator(key, text, state = "", tip = text) {
  const item = document.getElementById(`status-${key}`);
  item.textContent = text;
  item.title = `${item.dataset.name}: ${tip}`;
  item.dataset.state = state;
}

function sourceStatus(recording, key) {
  let text = "off";
  let state = "";
  let tip;
  const sp = recording?.splitter;
  if (recording && !sp) [text, state] = ["recording", "active"];
  else if (key === "capture" && capture?.selected === null) [text, state, tip] = ["no audio", "waiting", "waiting for sound to choose a channel"];
  else if (sp?.calibrating) [text, state, tip] = ["calibrating", "waiting", "measuring the background noise"];
  else if (sp) [text, state] = sp.speaking ? ["speech", "active"] : ["listening", ""];
  tip ??= text;
  if (pending[key]) {
    text += `/${pending[key]}`;
    tip += ` · ${pending[key]} being translated`;
  }
  setIndicator(key, text, state, tip);
}

function updateStatus() {
  sourceStatus(capture?.session, "capture");
  sourceStatus(session, "mic");
  const waiting = speechQueue.length;
  if (playbackPaused) setIndicator("playback", "paused", "waiting", "playback paused");
  else if (speech) setIndicator("playback", ...(speech.item
    ? ["speaking", "active"] : ["speaking ▶", "active", "speaking (played with ▶)"]));
  else if (playingBtn) setIndicator("playback", "playing", "active", "playing a recording");
  else if (waiting) setIndicator("playback", `${waiting} queued`, "waiting", `${waiting} waiting for a pause in the speech`);
  else setIndicator("playback", ...(autoPlayToggle.checked
    ? ["auto", "", "automatic playback, idle"] : ["▶ only", "", "plays only with ▶ (automatic playback is off)"]));
  updatePlaybackPanel();
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

// A block's RMS level (0..1) and peak.
function blockLevel(samples) {
  let sum = 0;
  let peak = 0;
  for (let i = 0; i < samples.length; i++) {
    sum += samples[i] * samples[i];
    peak = Math.max(peak, Math.abs(samples[i]));
  }
  return { rms: Math.sqrt(sum / samples.length), peak };
}

// A microphone block: onto the meter; returns its RMS level.
function meterSample(samples) {
  const { rms, peak } = blockLevel(samples);
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
  meterDb.textContent = "-∞ dB"; // keep the readout (and the layout) in place
}

// Auto-split on pauses. Every audio block updates a simple loudness-based
// voice activity detector: "speech" is anything clearly above the background
// noise level, which is tracked continuously (falling quickly, rising slowly).
// A phrase starts at the first loud block (plus some pre-roll audio), and ends
// after split_pause_ms of quiet or at max_phrase_s; it is then sent like a
// manual recording. Settings come from [auto_split] in the config.

const autoSplitToggle = document.getElementById("auto-split");
const autoSplit = {
  split_pause_ms: 250, threshold_db: 12, min_threshold_db: -50, pre_roll_ms: 300,
  tail_ms: 200, min_speech_ms: 300, max_phrase_s: 25,
};
const CALIBRATION_MS = 400; // measure background noise only, at the start
const NOISE_FALL_MS = 50; // background estimate follows quieter levels quickly...
const NOISE_RISE_MS = 2000; // ...and louder ones slowly (more slowly mid-phrase)

// label: where the audio comes from, noted with each phrase ("continuous
// mode" for the microphone, "captured audio").
// source: "mic" or "capture", for the status row.
function createSplitter(sampleRate, label = "continuous mode", source = "mic") {
  return {
    sampleRate,
    label,
    source,
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
  // For automatic playback: speech now; sustained (past a cough) mid-phrase.
  // A muted captured tab isn't heard here, so the translations talk over it.
  if (loud && !(sp.source === "capture" && capture?.muted)) noteSpeech(sp.speaking && sp.speechMs + ms >= autoSplit.min_speech_ms);

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
  if (sp.silenceMs >= autoSplit.split_pause_ms) endPhrase(sp, "pause");
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
  sendPhrase(samples, sp.sampleRate, `${sp.label} (${reason})`, sp.source);
}

async function sendPhrase(samples, sampleRate, note, source) {
  try {
    const resampled = await resample(samples, sampleRate, targetSampleRate);
    processRecording(encodeWav(resampled, targetSampleRate), note, source);
  } catch (err) {
    console.error(err);
    addNote(`Error: ${err.message}`, true);
  }
}

// Microphone input: the browser's processing options (from [audio] in
// config_local.toml) and a software gain stage between the microphone and the
// recorder, so the meter, speech detection and recordings all see the
// adjusted level. The slider value is remembered per browser.

const audioProcessing = { auto_gain_control: false, noise_suppression: true, echo_cancellation: true };
const gainSlider = document.getElementById("gain");
const gainValue = document.getElementById("gain-value");

function gainDb() {
  return Number(gainSlider.value);
}

function showGain() {
  const db = gainDb();
  gainValue.textContent = `${db > 0 ? "+" : ""}${db} dB`;
  if (session) session.gain.gain.setTargetAtTime(10 ** (db / 20), session.ctx.currentTime, 0.02);
  try {
    localStorage.setItem("gainDb", String(db));
  } catch {}
}

gainSlider.addEventListener("input", showGain);
gainSlider.addEventListener("dblclick", () => {
  gainSlider.value = "0";
  showGain();
});

try {
  const saved = localStorage.getItem("gainDb");
  if (saved !== null) gainSlider.value = saved;
} catch {}
showGain();

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
  row.dataset.time = time;
  row.querySelector(".meta").textContent = time;
}

// Record the detected language (again, if a recheck corrects it): shown in
// the meta line, and rows in language2 are right-aligned.
function setLanguage(row, language) {
  row.dataset.language = language || "";
  const meta = row.querySelector(".meta");
  meta.textContent = language ? `${row.dataset.time} · ${language}` : row.dataset.time;
  const isLang2 = !!language && language.toLowerCase() === language2.toLowerCase();
  row.querySelector(".utterance").classList.toggle("lang2", isLang2);
}

// Set a timing in a row's activity cell, listed in the order first set:
// "audio" (recording length), "stt" (transcription), "llm" (translation),
// "recheck" (re-transcribing when whisper heard another language),
// "tts" (time until the spoken translation starts playing).
// seconds: a number, null while in progress, or "failed".
const TIMING_LABELS = { audio: "audio", stt: "transcribe", recheck: "recheck", llm: "translate", tts: "speak" };
const TIMING_DECIMALS = { audio: 1, stt: 2, recheck: 2, llm: 2, tts: 2 };

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

// A small second line under a timing's label, e.g. time to first token.
function setTimingSub(row, kind, text, title = "") {
  const label = row.querySelector(`.timing[data-kind="${kind}"] .label`);
  let sub = label.querySelector(".sub");
  if (!sub) sub = label.appendChild(el("span", "sub"));
  sub.textContent = text;
  sub.title = title;
}

// Playback of a row's recording: the exact audio sent to whisper, kept in the
// browser. One shared player, so starting one recording stops another. Only
// one thing plays at a time: stopPlayback() stops recordings and speech (see
// speakTranslation) and drops any queued automatic speech.
const player = new Audio();
let playingBtn = null;

function stopRecordingPlayback() {
  player.pause();
  if (playingBtn) {
    playingBtn.textContent = "▶";
    playingBtn.title = "Play this recording";
    playingBtn = null;
    updateStatus();
  }
}

function stopPlayback() {
  playbackSeeking = false;
  playbackPaused = false;
  clearTimeout(turnTimer);
  speechQueue.splice(0).forEach((item) => cancelSpeech(item.audio));
  cancelSpeech(speech?.audio);
  stopSpeech();
  stopRecordingPlayback();
  updateStatus(); // e.g. "2 queued" -> idle, when nothing was playing
}

player.addEventListener("ended", () => {
  stopRecordingPlayback();
  playNextSpeech();
});

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
    connectRecordingMeter();
    player.play().catch((err) => {
      console.error(err);
      stopPlayback();
    });
    playingBtn = btn;
    updateStatus();
    btn.textContent = "■";
    btn.title = "Stop playback";
  });
  row.querySelector('.timing[data-kind="audio"]').appendChild(btn);
}

// Speaking a translation: the text is sent to /api/speak, which streams
// tts-server's audio back as raw PCM (16-bit little-endian mono; the rate is
// in the Content-Type). Fetching and playing are separate: loadSpeech() fetches
// a translation's audio into memory, one request at a time from this page
// (toTts), so queued translations are fetched ahead while another plays or the
// speaker is still talking; speakTranslation() plays it, starting once
// playback_buffer_ms ([tts]) of audio is in, or all of it if less (at once if
// it was fetched ahead), so a slow start doesn't make it stutter; then each
// piece is scheduled right after the previous one, so long speech starts
// before it is fully generated. Only one thing plays at a time: starting
// speech stops other playback, and vice versa. Active speech:
// { ctx, btn, item, audio, update }.
let speech = null;
let playbackPaused = false;
let recordingOutput = null;
const SPEECH_LEAD_S = 0.1; // head start before the first piece plays, to absorb network jitter
const SPEECH_RESUME_REWIND_S = 0.1; // replay a little context after an interruption
let playbackBufferS = 0.8;
const toTts = oneAtATime();

function setSpeakButton(btn, state) {
  const looks = {
    idle: ["▶", "Speak this translation"],
    waiting: ["…", "Generating speech (click to cancel)"],
    playing: ["■", "Stop speaking"],
  };
  [btn.textContent, btn.title] = looks[state];
  btn.setAttribute("aria-label", btn.title);
}

// Stop the speech playing (its audio is kept: see cancelSpeech).
function stopSpeech() {
  if (!speech) return;
  const { ctx, btn, audio, update } = speech;
  speech = null;
  audio.listeners.delete(update);
  ctx.close();
  setSpeakButton(btn, "idle");
  updateStatus();
}

// Convert 16-bit little-endian PCM bytes to samples (-1..1).
function pcmToFloat(bytes) {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.length);
  const samples = new Float32Array(bytes.length >> 1);
  for (let i = 0; i < samples.length; i++) samples[i] = view.getInt16(i * 2, true) / 32768;
  return samples;
}

// Which of the two languages a row's translation is in: the other one than
// the row's (whisper's, or a recheck's); "" if the row's is neither. The
// server picks [tts] voice_language1/voice_language2 by it.
function speechTarget(row) {
  const heard = (row.dataset.language || "").toLowerCase();
  if (heard === language1.toLowerCase()) return "language2";
  if (heard === language2.toLowerCase()) return "language1";
  return "";
}

// Fetch a row's speech in the background, once the requests before it are
// done. Returns { pieces (sample arrays), seconds, rate, done, error,
// controller, listeners }: listeners are called as pieces arrive and when it's
// done (or failed, or cancelled).
function loadSpeech(row, text) {
  const audio = { pieces: [], seconds: 0, rate: 24000, done: false, error: null,
    controller: new AbortController(), listeners: new Set() };
  const notify = () => audio.listeners.forEach((listener) => listener());
  setTiming(row, "tts", null);
  toTts(async () => {
    const { signal } = audio.controller;
    if (signal.aborted) return; // cancelled while waiting its turn
    const start = performance.now();
    const res = await pageFetch("/api/speak", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, target: speechTarget(row) || undefined }),
      signal,
    });
    if (!res.ok) {
      let message = `HTTP ${res.status}`;
      try {
        message = (await res.json()).error || message;
      } catch {}
      throw new Error(message);
    }
    audio.rate = Number(/rate=(\d+)/.exec(res.headers.get("Content-Type") || "")?.[1]) || 24000;
    const reader = res.body.getReader();
    let leftover = new Uint8Array(0); // an odd byte, completed by the next piece
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      let bytes = value;
      if (leftover.length) {
        bytes = new Uint8Array(leftover.length + value.length);
        bytes.set(leftover);
        bytes.set(value, leftover.length);
      }
      const even = bytes.length & ~1;
      leftover = bytes.slice(even);
      if (!even) continue;
      if (!audio.pieces.length) setTiming(row, "tts", (performance.now() - start) / 1000); // first audio
      const samples = pcmToFloat(bytes.subarray(0, even));
      audio.pieces.push(samples);
      audio.seconds += samples.length / audio.rate;
      notify();
    }
  }).catch((err) => {
    if (!audio.controller.signal.aborted) audio.error = err;
  }).finally(() => {
    audio.done = true;
    notify();
  });
  return audio;
}

// Cancel a speech's fetch (waiting or running) when it won't be played.
function cancelSpeech(audio) {
  if (audio && !audio.done) audio.controller.abort();
}

// Play a row's speech: item's audio (an automatic-playback queue entry, put
// back in the queue if the speaker starts again), else fetched now (a ▶ click).
function speakTranslation(row, btn, text, item = null, resume = null) {
  stopSpeech();
  stopRecordingPlayback();

  // Created right away (during the click, for the speak button) so the browser
  // allows it to play; automatic playback relies on the page having been
  // clicked or typed in before, so resume in case it starts suspended.
  const ctx = new AudioContext();
  if (playbackPaused) ctx.suspend().catch(() => {});
  else ctx.resume().catch(() => {});
  const audio = resume?.audio ?? item?.audio ?? loadSpeech(row, text);
  const resumeSeconds = resume?.position ?? item?.resumeSeconds ?? 0;
  const speed = playbackSpeed(row);
  const stretch = speed === 1 ? null : new SpeechTimeStretch(audio.rate, speed);
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 2048;
  analyser.connect(ctx.destination);
  const scheduled = []; // context times mapped to positions in the original audio
  const current = { ctx, btn, row, text, item, audio, analyser, speed, update: null, position: () => {
    const now = ctx.currentTime;
    for (let i = scheduled.length - 1; i >= 0; i--) {
      const segment = scheduled[i];
      if (now >= segment.start) return Math.min(audio.seconds,
        segment.offset + Math.min((now - segment.start) * speed, segment.duration));
    }
    return resumeSeconds; // buffering, or the first piece has not started yet
  } };
  speech = current;
  updateStatus();
  setSpeakButton(btn, "waiting");

  let played = 0; // pieces scheduled so far
  let pieceOffset = 0;
  let playAt = 0;
  let last = null;
  let stretchedSeconds = 0;
  const schedule = (samples, skip, offset, duration) => {
    if (!samples.length) return;
    const buffer = ctx.createBuffer(1, samples.length, audio.rate);
    buffer.copyToChannel(samples, 0);
    const source = new AudioBufferSourceNode(ctx, { buffer, playbackRate: 1 });
    source.connect(analyser);
    if (!last) setSpeakButton(btn, "playing");
    playAt = Math.max(playAt, ctx.currentTime + (last ? 0 : SPEECH_LEAD_S));
    source.start(playAt, skip);
    scheduled.push({ start: playAt, offset, duration });
    playAt += buffer.duration - skip;
    last = source;
  };
  const finished = () => {
    if (speech !== current) return;
    stopSpeech();
    playNextSpeech();
  };
  const fail = (err) => {
    console.error(err);
    stopSpeech();
    setTiming(row, "tts", "failed");
    addDetail(row, `Speech error: ${err.message}`, true);
    playNextSpeech();
  };
  current.update = () => {
    if (speech !== current) return;
    if (audio.error) return fail(audio.error);
    if (!last && !audio.done && audio.seconds - resumeSeconds < playbackBufferS) return; // still buffering
    for (; played < audio.pieces.length; played++) {
      const samples = audio.pieces[played];
      const offset = pieceOffset;
      pieceOffset += samples.length / audio.rate;
      if (pieceOffset <= resumeSeconds) continue; // already heard this whole piece
      const skip = Math.max(0, resumeSeconds - offset);
      if (stretch) stretch.append(samples.subarray(Math.round(skip * audio.rate)));
      else schedule(samples, skip, offset + skip, samples.length / audio.rate - skip);
    }
    if (stretch) {
      const samples = stretch.read(audio.done);
      const duration = samples.length / audio.rate;
      schedule(samples, 0, resumeSeconds + stretchedSeconds * speed, duration * speed);
      stretchedSeconds += duration;
    }
    updateStatus();
    if (!audio.done) return;
    audio.listeners.delete(current.update);
    if (!last) return audio.seconds ? finished() : fail(new Error("no audio received"));
    if (ctx.currentTime >= playAt) finished(); // already played out
    else last.onended = finished;
  };
  audio.listeners.add(current.update);
  current.update(); // fetched ahead: plays at once
}

// Automatic translation playback: each finished translation is queued and
// spoken, in order, once the speaker's turn has ended: every running source
// quiet for turn_pause_ms ([tts]; longer than the pause that splits phrases).
// Each one's speech is fetched as soon as it's queued (see loadSpeech), so
// it's ready, or nearly, when its turn comes. If someone starts speaking
// during playback, it stops and that translation goes back to the front of
// the queue, keeping its audio and resume position. The backlog is the speech waiting (not the
// one playing), counting only audio fetched in full: once it's over
// max_backlog_ms, the oldest are dropped until it isn't, always keeping the
// newest. Stopping playback (or starting a recording) drops the waiting ones.
// The ▶ buttons play at once.
const autoPlayToggle = document.getElementById("auto-play");
autoPlayToggle.addEventListener("change", () => updateStatus());
const speechQueue = []; // [{ row, btn, text, audio, resumeSeconds? }]
let turnPauseMs = 2000;
let maxBacklogS = 30;
let lastSpeechAt = 0; // performance.now() of the last speech from any source
let turnTimer = null;

// Called by the sources as they hear speech. sustained: speech that should
// interrupt automatic playback (not just a cough or a click).
function noteSpeech(sustained) {
  lastSpeechAt = performance.now();
  if (sustained && speech?.item && !playbackPaused) {
    const item = speech.item;
    const position = speech.position();
    // Don't repeatedly rewind when interrupted before resumed audio starts.
    if (position > (item.resumeSeconds || 0)) {
      item.resumeSeconds = Math.max(0, position - SPEECH_RESUME_REWIND_S);
    }
    stopSpeech();
    speechQueue.unshift(item); // resume at the next turn's end, using the fetched audio
    trimBacklog();
    playNextSpeech();
  }
}

function queueSpeech(row, btn, text) {
  const audio = loadSpeech(row, text);
  audio.listeners.add(() => {
    if (audio.done) { trimBacklog(); updateStatus(); } // its length counts once it's all in
  });
  speechQueue.push({ row, btn, text, audio });
  playNextSpeech();
  updateStatus();
}

// Seconds of speech waiting to be played, fetched in full.
function backlogSeconds() {
  return speechQueue.reduce((sum, { row, audio, resumeSeconds = 0 }) =>
    sum + (audio.done && !audio.error ? Math.max(0, audio.seconds - resumeSeconds) / playbackSpeed(row) : 0), 0);
}

// Drop the oldest waiting translations while the backlog is over the limit,
// keeping at least the newest.
function trimBacklog() {
  let dropped = false;
  while (speechQueue.length > 1 && backlogSeconds() > maxBacklogS) {
    const oldest = speechQueue.shift();
    cancelSpeech(oldest.audio);
    addDetail(oldest.row, `Not spoken automatically: over ${maxBacklogS} s of speech waiting`);
    dropped = true;
  }
  if (dropped) updateStatus();
}

// Speak the next waiting translation, once nothing is playing and the
// speaker's turn has ended; until then, check again when it could have.
function playNextSpeech() {
  clearTimeout(turnTimer);
  if (playbackPaused || speech || playingBtn || !speechQueue.length) return;
  const wait = turnPauseMs - (performance.now() - lastSpeechAt);
  if (wait > 0) {
    turnTimer = setTimeout(playNextSpeech, wait + 20);
    return;
  }
  const next = speechQueue.shift();
  speakTranslation(next.row, next.btn, next.text, next);
}

// Playback controls share the live queue, including manually played row audio.
const pausePlaybackBtn = document.getElementById("playback-pause");
const restartPlaybackBtn = document.getElementById("playback-restart");
const stopPlaybackBtn = document.getElementById("playback-stop");
const nextPlaybackBtn = document.getElementById("playback-next");
const speedInputs = [document.getElementById("speed1"), document.getElementById("speed2")];
const playbackPosition = document.getElementById("playback-position");
let playbackSeeking = false;

function playbackTime(seconds) {
  const total = Math.max(0, Math.floor(seconds));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

function updatePlaybackTimeline() {
  const duration = speech?.audio.seconds ?? (playingBtn && Number.isFinite(player.duration) ? player.duration : 0);
  const position = speech?.position() ?? (playingBtn ? player.currentTime : 0);
  const growing = speech && !speech.audio.done;
  playbackPosition.disabled = !duration;
  playbackPosition.max = String(duration || 1);
  if (!playbackSeeking) playbackPosition.value = String(Math.min(position, duration));
  const elapsed = playbackTime(playbackSeeking ? Number(playbackPosition.value) : position);
  const length = `${playbackTime(Math.ceil(duration - 0.000001))}${growing ? "+" : ""}`;
  document.getElementById("playback-elapsed").textContent = elapsed;
  document.getElementById("playback-duration").textContent = `/ ${length}`;
  playbackPosition.setAttribute("aria-valuetext", `${elapsed} of ${length}${growing ? ", still generating" : ""}`);
  playbackPosition.title = growing ? "Seek within received audio; total duration is still growing" : "Seek within current clip";
}

playbackPosition.addEventListener("input", () => {
  playbackSeeking = true;
  updatePlaybackTimeline();
});
playbackPosition.addEventListener("change", () => {
  const position = Number(playbackPosition.value);
  playbackSeeking = false;
  if (speech) restartCurrentSpeech(position);
  else if (playingBtn) player.currentTime = position;
  updatePlaybackTimeline();
});

function playbackSpeed(row) {
  return Number(speedInputs[speechTarget(row) === "language2" ? 1 : 0].value);
}

function updatePlaybackPanel() {
  const active = !!(speech || playingBtn);
  const waiting = speechQueue.length;
  const label = playbackPaused ? "Paused" : speech
    ? (speech.btn.title === "Stop speaking" ? "Speaking" : "Buffering")
    : playingBtn ? "Playing recording" : waiting ? "Waiting for speaker to pause" : "Idle";
  document.getElementById("playback-status").textContent = speech
    ? `${label} · ${speechTarget(speech.row) === "language2" ? language2 : language1}` : label;
  document.getElementById("playback-queue").textContent = `${waiting} queued · ${Math.ceil(backlogSeconds())} s waiting`;
  pausePlaybackBtn.disabled = !active && !waiting && !playbackPaused;
  pausePlaybackBtn.title = playbackPaused ? "Resume playback" : "Pause playback";
  pausePlaybackBtn.setAttribute("aria-label", pausePlaybackBtn.title);
  pausePlaybackBtn.setAttribute("aria-pressed", String(playbackPaused));
  pausePlaybackBtn.querySelector("path").setAttribute("d", playbackPaused ? "M6 3l15 9-15 9z" : "M6 4h4v16H6zm8 0h4v16h-4z");
  stopPlaybackBtn.disabled = !active && !waiting && !playbackPaused;
  restartPlaybackBtn.disabled = !active;
  nextPlaybackBtn.disabled = !waiting;
  updatePlaybackTimeline();
}

function restartCurrentSpeech(position) {
  const current = speech;
  if (!current) return;
  if (current.item) current.item.resumeSeconds = position;
  speakTranslation(current.row, current.btn, current.text, current.item,
    { audio: current.audio, position });
}

pausePlaybackBtn.addEventListener("click", () => {
  playbackPaused = !playbackPaused;
  if (speech) (playbackPaused ? speech.ctx.suspend() : speech.ctx.resume()).catch(console.error);
  if (playingBtn) {
    if (playbackPaused) player.pause();
    else player.play().catch(console.error);
  }
  if (!playbackPaused) playNextSpeech();
  updateStatus();
});
stopPlaybackBtn.addEventListener("click", stopPlayback);
restartPlaybackBtn.addEventListener("click", () => {
  if (speech) restartCurrentSpeech(0);
  else if (playingBtn) player.currentTime = 0;
});
nextPlaybackBtn.addEventListener("click", () => {
  cancelSpeech(speech?.audio);
  stopSpeech();
  stopRecordingPlayback();
  const next = speechQueue.shift();
  if (next) speakTranslation(next.row, next.btn, next.text, next);
  updateStatus();
});
speedInputs.forEach((input, index) => {
  try { input.value = localStorage.getItem(`playbackSpeed${index + 1}`) || "1"; } catch {}
  const show = () => { input.previousElementSibling.value = `${Number(input.value).toFixed(2)}×`; };
  show();
  input.addEventListener("input", () => {
    show();
    try { localStorage.setItem(`playbackSpeed${index + 1}`, input.value); } catch {}
    if (speech && speechTarget(speech.row) === `language${index + 1}`) restartCurrentSpeech(speech.position());
    updateStatus();
  });
});

function connectRecordingMeter() {
  if (!recordingOutput) {
    const ctx = new AudioContext();
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 2048;
    ctx.createMediaElementSource(player).connect(analyser).connect(ctx.destination);
    recordingOutput = { ctx, analyser };
  }
  recordingOutput.ctx.resume().catch(() => {});
}

const playbackMeter = document.getElementById("playback-meter");
const playbackSamples = new Float32Array(2048);
let playbackShownDb = METER_FLOOR_DB;
let playbackMeterFrame = null;
function drawPlaybackMeter() {
  updatePlaybackTimeline();
  const analyser = !playbackPaused && (speech?.analyser || (playingBtn && recordingOutput?.analyser));
  let rms = 0, peak = 0;
  if (analyser) {
    analyser.getFloatTimeDomainData(playbackSamples);
    ({ rms, peak } = blockLevel(playbackSamples));
  }
  const db = toDb(rms);
  playbackShownDb = Math.max(db, playbackShownDb - 1);
  playbackMeter.querySelector(".meter-fill").style.width = `${dbToPercent(playbackShownDb)}%`;
  playbackMeter.classList.toggle("active", !!analyser);
  playbackMeter.classList.toggle("hot", playbackShownDb > METER_HOT_DB);
  playbackMeter.classList.toggle("clip", peak >= 0.99);
  playbackMeter.classList.toggle("quiet", playbackShownDb < METER_QUIET_DB);
  playbackMeter.setAttribute("aria-valuenow", String(Math.round(playbackShownDb)));
  document.getElementById("playback-db").textContent = playbackShownDb <= METER_FLOOR_DB ? "-∞ dB" : `${Math.round(playbackShownDb)} dB`;
  playbackMeterFrame = requestAnimationFrame(drawPlaybackMeter);
}
playbackMeterFrame = requestAnimationFrame(drawPlaybackMeter);

// Add a speak button after a row's translated text (once it's complete).
// Returns the button.
function addSpeakButton(row, text) {
  const line = row.querySelector(".translation");
  const content = el("span", "translation-text", text);
  const { container, play: btn } = TranslationActions.create(text, content, () => {
    const wasThis = speech?.btn === btn;
    stopPlayback(); // also drops queued automatic speech
    if (!wasThis) speakTranslation(row, btn, text); // else the click was "cancel" or "stop"
  });
  setSpeakButton(btn, "idle");
  line.replaceChildren(content, container);
  return btn;
}

// Raw whisper/llama exchanges: a "{ }" button on a timing line opens a dialog
// with the request and full response as formatted JSON.
const rawDialog = document.getElementById("raw-dialog");
const rawTitle = document.getElementById("raw-title");
const rawBody = document.getElementById("raw-body");
const rawCopy = document.getElementById("raw-copy");

// raw: shown as formatted JSON, or as is if it's a string.
function showRaw(title, raw) {
  rawTitle.textContent = title;
  rawBody.textContent = typeof raw === "string" ? raw : JSON.stringify(raw, null, 2);
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

// Background information about a row's recording (how it was split, the saved
// file), kept out of the activity cell: an "ⓘ" button on the audio timing line
// (left of the play button) shows it in the viewer dialog.
function addInfo(row, text) {
  if (!row.info) {
    row.info = [];
    const btn = el("button", "row-btn", "ⓘ");
    btn.type = "button";
    btn.title = "Show recording details";
    btn.addEventListener("click", () => showRaw("Recording details", row.info.join("\n")));
    const line = row.querySelector('.timing[data-kind="audio"]');
    line.insertBefore(btn, line.querySelector(".row-btn"));
  }
  row.info.push(text);
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

// Server status in the header: /api/health checks whisper, llama and the TTS
// server; shown as a dot per server. The tooltip has the address and what the
// check found. Checked every 10 s, or every 2 s while any isn't ready (e.g.
// while they start: the dashboard starts first), so they turn green promptly.
const servicesEl = document.getElementById("services");
const SERVICE_LABELS = { stt: "Transcribe", llm: "Translate", tts: "Speak" };
const HEALTH_INTERVAL_MS = 10000;
const HEALTH_INTERVAL_STARTING_MS = 2000;

async function checkServices() {
  if (pageLifetime.signal.aborted) return;
  let allOk = false;
  try {
    const res = await pageFetch("/api/health");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const { services } = await res.json();
    servicesEl.replaceChildren(...services.map((s) => {
      const item = el("span", `service ${s.state}`, SERVICE_LABELS[s.role] || s.role);
      item.title = `${s.name} at ${s.url}: ${s.state} (${s.detail})`;
      return item;
    }));
    allOk = services.every((s) => s.state === "ok");
  } catch (err) {
    // The dashboard itself didn't answer (e.g. stopped): mark everything unknown.
    for (const item of servicesEl.children) {
      item.className = "service";
      item.title = `Dashboard not reachable: ${err.message}`;
    }
  }
  if (!pageLifetime.signal.aborted) {
    healthTimer = setTimeout(checkServices, allOk ? HEALTH_INTERVAL_MS : HEALTH_INTERVAL_STARTING_MS);
  }
}

let healthTimer = null;
checkServices();

async function loadConfig() {
  try {
    const res = await pageFetch("/api/config");
    const config = await res.json();
    targetSampleRate = config.sample_rate;
    language1 = config.language1 || "";
    language2 = config.language2 || "";
    document.getElementById("speed-language1").textContent = language1;
    document.getElementById("speed-language2").textContent = language2;
    languageChecks = config.language_checks !== false;
    Object.assign(audioProcessing, config.audio_processing);
    const { enabled, ...settings } = config.auto_split || {};
    Object.assign(autoSplit, settings);
    autoSplitToggle.checked = !!enabled;
    autoPlayToggle.checked = !!config.auto_play;
    turnPauseMs = config.turn_pause_ms ?? turnPauseMs;
    maxBacklogS = (config.max_backlog_ms ?? maxBacklogS * 1000) / 1000;
    playbackBufferS = (config.playback_buffer_ms ?? playbackBufferS * 1000) / 1000;
    updateStatus(); // the playback indicator depends on the switch
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
    addSpeakButton(row, item.translation); // speaks silence in mock mode
    setTiming(row, "audio", item.audio_seconds);
    // Silence of the listed length, so the play button looks and behaves as usual.
    const silence = new Float32Array(Math.round(item.audio_seconds * targetSampleRate));
    addPlayButton(row, encodeWav(silence, targetSampleRate));
    setTiming(row, "stt", item.stt_seconds);
    setTiming(row, "llm", item.llm_seconds);
    addRawButton(row, "stt", "whisper response (mock)", { mock: true, text: item.text, language: item.language });
    addRawButton(row, "llm", "llama response (mock)", { mock: true, text: item.text, translation: item.translation });
    addInfo(row, "Mock sample (not a real recording)");
  }
}

// auto: split on pauses (each phrase is sent as it ends) instead of keeping
// all audio for one recording.
async function startRecording(auto) {
  stopPlayback(); // speakers playing into the mic would end up in the recording
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      channelCount: 1,
      autoGainControl: audioProcessing.auto_gain_control,
      noiseSuppression: audioProcessing.noise_suppression,
      echoCancellation: audioProcessing.echo_cancellation,
    },
  });
  if (pageLifetime.signal.aborted) {
    stream.getTracks().forEach((t) => t.stop());
    pageLifetime.signal.throwIfAborted();
  }
  const ctx = new AudioContext();
  try {
    await ctx.audioWorklet.addModule("recorder-worklet.js");
    pageLifetime.signal.throwIfAborted();
    const source = ctx.createMediaStreamSource(stream);
    const gain = new GainNode(ctx, { gain: 10 ** (gainDb() / 20) });
    const node = new AudioWorkletNode(ctx, "recorder");
    const chunks = [];
    const splitter = auto ? createSplitter(ctx.sampleRate) : null;
    node.port.onmessage = (e) => {
      const rms = meterSample(e.data);
      if (splitter) splitterBlock(splitter, e.data, rms);
      else {
        chunks.push(e.data);
        noteSpeech(false); // a manual recording is a turn in progress
      }
    };
    source.connect(gain).connect(node);
    node.connect(ctx.destination); // keeps the node processing; it outputs silence
    session = { stream, ctx, source, gain, node, chunks, splitter };
    startMeter();
  } catch (err) {
    stream.getTracks().forEach((t) => t.stop());
    ctx.close();
    throw err;
  }
}

// Stops a recording session: the microphone's (session), or a capture's.
// Returns the recording as a WAV Blob; in auto-split mode, sends any phrase in
// progress and returns null.
async function stopRecording(recording = session) {
  const { stream, ctx, source, gain, node, chunks, splitter } = recording;
  if (recording === session) {
    session = null;
    stopMeter(); // the microphone's meter (a capture has its own, per channel)
  }
  source.disconnect();
  gain.disconnect();
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

// Each server gets one request at a time from this page, in the order they're
// asked for, and runs it with the whole machine; the stages still overlap
// (the next phrase is transcribed while this one is translated). toWhisper(task),
// toLlama(task) and toTts(task) (see loadSpeech) run task() once the ones
// before it have finished (or failed) and return its result.
function oneAtATime() {
  let last = Promise.resolve();
  return (task) => {
    const run = last.then(() => {
      pageLifetime.signal.throwIfAborted(); // discarded jobs must not start after leaving
      return task();
    });
    last = run.catch(() => {});
    return run;
  };
}

const toWhisper = oneAtATime();
const toLlama = oneAtATime();
window.translationQueues = { whisper: toWhisper, llama: toLlama, tts: toTts };

// Returns the response body; on failure, throws an Error that also carries
// the body (which may include the saved file's path).
async function uploadWav(wav) {
  const { res, body } = await toWhisper(async () => {
    const res = await pageFetch("/api/recordings", {
      method: "POST",
      headers: { "Content-Type": "audio/wav" },
      body: wav,
    });
    return { res, body: await res.json() };
  });
  if (!res.ok) throw Object.assign(new Error(body.error || `HTTP ${res.status}`), { body });
  return body;
}

// Translate in the direction established by Whisper, preserving the model's text.
// Returns the translation, timings and raw stream for the activity viewer.
async function requestTranslation(text, source, onText) {
  const start = performance.now();
  const res = await pageFetch("/api/translate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, source }),
  });
  if (!res.ok) {
    let message = `HTTP ${res.status}`;
    try {
      message = (await res.json()).error || message;
    } catch {}
    throw new Error(message);
  }

  const raw = { request: null, response: [] };
  let translation = "";
  let firstSeconds = null;
  let error = null;

  // One server-sent event: optional "event: name" plus "data: ..." line(s).
  const handleEvent = (block) => {
    let name = "message";
    const data = [];
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) name = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
    }
    if (!data.length) return;
    const payload = data.join("\n");
    if (name === "request") {
      raw.request = JSON.parse(payload);
      return;
    }
    if (name === "error") {
      error = JSON.parse(payload).error;
      raw.response.push({ event: "error", ...JSON.parse(payload) });
      return;
    }
    let chunk;
    try {
      chunk = JSON.parse(payload);
    } catch {
      raw.response.push(payload); // e.g. "[DONE]"
      return;
    }
    raw.response.push(chunk);
    if (chunk.error) error = chunk.error.message || JSON.stringify(chunk.error);
    const piece = chunk.choices?.[0]?.delta?.content;
    if (piece) {
      if (firstSeconds === null) firstSeconds = (performance.now() - start) / 1000;
      translation += piece;
      onText(translation.trim());
    }
  };

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    buffer = (buffer + decoder.decode(value, { stream: !done })).replace(/\r\n/g, "\n");
    let end;
    while ((end = buffer.indexOf("\n\n")) >= 0) {
      handleEvent(buffer.slice(0, end));
      buffer = buffer.slice(end + 2);
    }
    if (done) break;
  }
  if (buffer.trim()) handleEvent(buffer);

  const seconds = (performance.now() - start) / 1000;
  if (error) throw Object.assign(new Error(error), { raw, seconds });
  return { translation: translation.trim(), seconds, firstSeconds, raw };
}

function addFileInfo(row, path, temporary = false) {
  addInfo(row, temporary ? "Temporary recording (automatically deleted)" : `Saved as: ${path}`);
}

// Translate a row's text into its translation line (streamed), with its
// timing and raw viewer; throws (after showing the error) if it fails. Run
// again after a recheck, it replaces the earlier translation.
async function translateRow(row, text, source) {
  setLine(row, ".translation", "Translating...", "pending");
  setTiming(row, "llm", null);
  row.querySelector('.timing[data-kind="llm"] .row-btn')?.remove(); // an earlier attempt's raw viewer
  try {
    const result = await toLlama(() =>
      requestTranslation(text, source, (soFar) => setLine(row, ".translation", soFar)));
    setTiming(row, "llm", result.seconds);
    if (result.firstSeconds !== null) {
      setTimingSub(row, "llm", `${result.firstSeconds.toFixed(2)}s TTFT`, "time to first token");
    }
    addRawButton(row, "llm", "llama request and streamed response", result.raw);
    if (result.translation) setLine(row, ".translation", result.translation);
    return result;
  } catch (err) {
    setTiming(row, "llm", "failed");
    if (err.raw) addRawButton(row, "llm", "llama request and streamed response", err.raw);
    setLine(row, ".translation", `Error: ${err.message}`, "error");
    throw err;
  }
}

// The transcript seems to be in neither of the two languages (e.g. Russian for
// quiet Chinese); reason says who thought so. Show it, marked, while the server
// transcribes it again forced to each of the two, then swap in the more
// confident result. Returns the result to use (the original if this fails).
async function recheck(row, stt, reason) {
  setLine(row, ".original", `${stt.text} (${reason}; rechecking…)`, "pending");
  setTiming(row, "recheck", null);
  try {
    const { res, result } = await toWhisper(async () => {
      const res = await pageFetch("/api/recordings/recheck", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ file: stt.file.split(/[\\/]/).pop() }),
      });
      return { res, result: await res.json() };
    });
    if (!res.ok) throw new Error(result.error || `HTTP ${res.status}`);
    setTiming(row, "recheck", result.stt_seconds);
    addRawButton(row, "recheck", "whisper recheck: both languages", result.stt_raw);
    setLanguage(row, result.language);
    setLine(row, ".original", result.text);
    addDetail(row, `re-transcribed as ${result.language} (${reason})`);
    return { ...stt, ...result };
  } catch (err) {
    console.error(err);
    setTiming(row, "recheck", "failed");
    setLine(row, ".original", stt.text);
    addDetail(row, `Recheck failed: ${err.message}`, true);
    return stt;
  }
}

// Transcribe, then translate, in the background so the next recording can start.
// note: optional detail for the activity cell (e.g. how an auto-split phrase ended).
// source: "mic" or "capture" (for the status row's counts).
async function processRecording(wav, note = "", source = "mic") {
  if (pageLifetime.signal.aborted) return;
  const audioSeconds = (wav.size - 44) / 2 / targetSampleRate;
  const row = addRow();
  setMeta(row, new Date().toLocaleTimeString());
  setLine(row, ".original", "Transcribing...", "pending");
  setTiming(row, "audio", audioSeconds);
  addPlayButton(row, wav);
  setTiming(row, "stt", null);
  if (note) addInfo(row, `Split by: ${note}`);
  pending[source]++;
  updateStatus();
  try {
    let stt;
    try {
      stt = await uploadWav(wav);
    } catch (err) {
      setTiming(row, "stt", "failed");
      setLine(row, ".original", `Error: ${err.message}`, "error");
      if (err.body?.file) addFileInfo(row, err.body.file, err.body.temporary);
      throw err;
    }
    setTiming(row, "stt", stt.stt_seconds);
    if (stt.stt_raw) addRawButton(row, "stt", "whisper request and response", stt.stt_raw);
    addFileInfo(row, stt.file, stt.temporary);
    setLanguage(row, stt.language);
    if (!stt.text) {
      setLine(row, ".original", "(no speech detected)", "pending");
      return;
    }
    setLine(row, ".original", stt.text);
    if (stt.recheck) stt = await recheck(row, stt, `whisper heard ${stt.language}`);
    if (!stt.text) {
      setLine(row, ".original", "(no speech detected)", "pending");
      return;
    }

    const result = await translateRow(row, stt.text, stt.language);

    if (result.translation) {
      const speakBtn = addSpeakButton(row, result.translation);
      if (autoPlayToggle.checked) queueSpeech(row, speakBtn, result.translation);
    } else {
      setLine(row, ".translation", "(empty translation)", "pending");
    }
  } catch (err) {
    console.error(err);
  } finally {
    pending[source]--;
    updateStatus();
  }
}

recordBtn.addEventListener("click", async () => {
  // if (capture) return; // capturing instead (one source at a time: off for now)
  recordBtn.disabled = true;
  try {
    if (!session) {
      const auto = autoSplitToggle.checked;
      autoSplitToggle.disabled = true; // no mode change mid-recording
      await startRecording(auto);
      recordBtn.textContent = auto ? "Stop Listening" : "Stop Recording";
      recordBtn.classList.add("recording");
      updateStatus();
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
    setIndicator("mic", "error", "error");
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
    // captureBtn.disabled = !!session; // one source at a time (off for now)
  }
});

// --- Capture Audio (the sidebar): translate the sound of a browser tab, a
// window or the whole system instead of the microphone, through the browser's
// screen sharing (getDisplayMedia) used in reverse. The captured sound goes
// through the same pipeline as Continuous Audio Mode: one chosen channel, mono,
// split at pauses. The microphone and a capture can run at the same time
// (each with its own session and level meters).

const captureBtn = document.getElementById("capture-btn");
const captureStatus = document.getElementById("capture-status");
const captureChannelsEl = document.getElementById("capture-channels");
const captureMuteBox = document.getElementById("capture-mute");
const captureHelp = document.getElementById("capture-help");
const CHANNEL_SOUND_DB = -50; // the first channel louder than this is chosen automatically
const SURFACES = { browser: "a browser tab", window: "a window", monitor: "the entire screen" };

// While capturing: { session (its own, like the microphone's: see
// startRecording), channels: [{ analyser, gate, fill, peak, db, row, shownDb,
// maxDb }], selected (index, or null until a channel has sound), surface,
// track, muted, buffer, frame }. (Declared at the top, with session.)

document.getElementById("capture-help-btn").addEventListener("click", () => captureHelp.showModal());
document.getElementById("capture-help-close").addEventListener("click", () => captureHelp.close());
captureHelp.addEventListener("click", (e) => {
  if (e.target === captureHelp) captureHelp.close(); // click on the backdrop
});

function setCaptureStatus(text, isError = false) {
  captureStatus.textContent = text;
  captureStatus.classList.toggle("error", isError);
}

function describeCapture() {
  const what = `Capturing ${SURFACES[capture.surface] || "audio"}`;
  if (capture.selected === null) return `${what} · waiting for sound`;
  const name = capture.channels.length > 1 ? ["left", "right"][capture.selected] + " channel" : "mono";
  return `${what} · translating the ${name}`;
}

// One meter per channel; clicking one translates that channel.
function makeChannelRow(name, index) {
  const row = el("button", "channel");
  row.type = "button";
  row.title = `Translate the ${name.toLowerCase()} channel`;
  const bar = el("span", "meter active");
  bar.setAttribute("role", "meter");
  bar.setAttribute("aria-label", `${name} channel level`);
  bar.setAttribute("aria-valuemin", String(METER_FLOOR_DB));
  bar.setAttribute("aria-valuemax", "0");
  bar.setAttribute("aria-valuenow", String(METER_FLOOR_DB));
  const fill = el("span", "meter-fill");
  const peak = el("span", "meter-peak");
  bar.append(fill, peak);
  const db = el("span", "meter-db", "-∞ dB");
  row.append(el("span", "ch-name", name[0]), bar, db);
  row.addEventListener("click", () => selectChannel(index));
  captureChannelsEl.append(row);
  return { row, bar, fill, peak, db, shownDb: METER_FLOOR_DB, maxDb: METER_FLOOR_DB };
}

// Send channel index to the recorder (the others are gated off).
function selectChannel(index) {
  if (!capture) return;
  capture.selected = index;
  capture.channels.forEach((ch, i) => {
    ch.gate.gain.value = i === index ? 1 : 0;
    ch.row.classList.toggle("selected", i === index);
  });
  setCaptureStatus(describeCapture());
}

// The channel meters, every frame; until a channel is chosen, choose the first
// one with sound (left if both start together, as with most stereo).
function drawChannels() {
  if (!capture) return;
  let first = null;
  capture.channels.forEach((ch, i) => {
    ch.analyser.getFloatTimeDomainData(capture.buffer);
    let sum = 0;
    let peak = 0;
    for (const s of capture.buffer) {
      sum += s * s;
      peak = Math.max(peak, Math.abs(s));
    }
    const db = toDb(Math.sqrt(sum / capture.buffer.length));
    ch.shownDb = db > ch.shownDb ? db : Math.max(db, ch.shownDb - 1);
    ch.maxDb = Math.max(ch.maxDb, toDb(peak));
    ch.fill.style.width = `${dbToPercent(ch.shownDb)}%`;
    ch.bar.classList.toggle("clip", peak >= 0.99);
    ch.bar.classList.toggle("hot", ch.shownDb > METER_HOT_DB);
    ch.bar.classList.toggle("quiet", ch.shownDb < METER_QUIET_DB);
    ch.bar.setAttribute("aria-valuenow", String(Math.round(ch.shownDb)));
    ch.peak.style.left = `calc(${dbToPercent(ch.maxDb)}% - 1px)`;
    ch.db.textContent = `${ch.shownDb <= METER_FLOOR_DB ? "-∞" : Math.round(ch.shownDb)} dB`;
    if (first === null && db > CHANNEL_SOUND_DB) first = i;
  });
  if (capture.selected === null && first !== null) selectChannel(first);
  capture.frame = requestAnimationFrame(drawChannels);
}

async function startCapture() {
  const stream = await navigator.mediaDevices.getDisplayMedia({
    video: { width: { max: 320 }, frameRate: { max: 1 } }, // required by the browser; not used
    // Mute captured tab: the tab stops playing here but is still captured, so only
    // the translations are heard. Browsers honour this only when capture starts.
    audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false,
             suppressLocalAudioPlayback: captureMuteBox.checked },
    systemAudio: "include", // offer "Also share system audio" for the entire screen
  });
  if (pageLifetime.signal.aborted) {
    stream.getTracks().forEach((t) => t.stop());
    pageLifetime.signal.throwIfAborted();
  }
  const surface = stream.getVideoTracks()[0]?.getSettings().displaySurface || "";
  const track = stream.getAudioTracks()[0];
  if (!track) {
    stream.getTracks().forEach((t) => t.stop());
    throw new Error(surface === "window" ? "A window shares no sound; choose a tab or the entire screen."
      : 'No sound was shared: turn on "Also share tab audio" (or "system audio") in the picker.');
  }
  const ctx = new AudioContext();
  try {
    await ctx.audioWorklet.addModule("recorder-worklet.js");
    pageLifetime.signal.throwIfAborted();
    const source = ctx.createMediaStreamSource(new MediaStream([track]));
    const count = Math.min(2, track.getSettings().channelCount || source.channelCount || 1);
    const split = ctx.createChannelSplitter(count);
    const mix = new GainNode(ctx); // the chosen channel, mono, on to the recorder
    source.connect(split);
    captureChannelsEl.replaceChildren();
    const names = count > 1 ? ["Left", "Right"] : ["Mono"];
    const channels = names.map((name, i) => {
      const analyser = new AnalyserNode(ctx, { fftSize: 2048 });
      const gate = new GainNode(ctx, { gain: 0 });
      split.connect(analyser, i);
      split.connect(gate, i);
      gate.connect(mix);
      return { analyser, gate, ...makeChannelRow(name, i) };
    });
    const node = new AudioWorkletNode(ctx, "recorder");
    const splitter = createSplitter(ctx.sampleRate, "captured audio", "capture");
    // Its level goes to the phrase splitting only: the meter by Start Recording is
    // the microphone's (the sidebar has the capture's, per channel).
    node.port.onmessage = (e) => splitterBlock(splitter, e.data, blockLevel(e.data).rms);
    mix.connect(node);
    node.connect(ctx.destination); // keeps the node processing; it outputs silence
    capture = { session: { stream, ctx, source, gain: mix, node, chunks: [], splitter },
                channels, selected: null, surface, track, muted: captureMuteBox.checked, buffer: new Float32Array(2048), frame: 0 };
    if (count === 1) selectChannel(0);
    else setCaptureStatus(describeCapture());
    // The browser's own "Stop sharing" ends the tracks.
    for (const t of stream.getTracks()) t.addEventListener("ended", () => { if (capture) stopCapture(); });
    capture.frame = requestAnimationFrame(drawChannels);
  } catch (err) {
    stream.getTracks().forEach((t) => t.stop());
    ctx.close();
    captureChannelsEl.replaceChildren();
    throw err;
  }
}

async function stopCapture() {
  if (!capture) return;
  cancelAnimationFrame(capture.frame);
  const recording = capture.session;
  capture = null;
  await stopRecording(recording); // sends any phrase in progress, closes everything
  captureChannelsEl.replaceChildren();
  setCaptureStatus("Not capturing");
  setCaptureUi(false);
  updateStatus();
}

function setCaptureUi(capturing) {
  captureBtn.textContent = capturing ? "Stop Capture" : "Capture Audio";
  captureBtn.classList.toggle("capturing", capturing);
  // One source at a time (switched off for now, to try both at once):
  // recordBtn.disabled = capturing; // (Space does nothing either)
  // autoSplitToggle.disabled = capturing;
  captureMuteBox.disabled = capturing; // takes effect only when capture starts
}

captureBtn.addEventListener("click", async () => {
  if (capture) {
    stopCapture();
    return;
  }
  // if (session) return; // recording from the microphone (one source at a time: off for now)
  captureBtn.disabled = true;
  try {
    await startCapture();
    setCaptureUi(true);
    updateStatus();
  } catch (err) {
    console.error(err);
    // Closing the picker without sharing isn't an error worth showing.
    setCaptureStatus(err.name === "NotAllowedError" ? "Not capturing" : err.message, err.name !== "NotAllowedError");
  } finally {
    captureBtn.disabled = false;
  }
});

// Space starts/stops recording from anywhere on the page. It takes over Space
// from focused buttons and switches (which would otherwise also be toggled);
// Enter still activates buttons. Text fields keep Space for typing.
function isSpaceShortcut(e) {
  if (window.location.hash && window.location.hash !== "#live") return false;
  if (e.code !== "Space" || e.ctrlKey || e.altKey || e.metaKey) return false;
  if (rawDialog.open) return false; // Space scrolls/acts inside the viewer instead
  const target = e.target;
  if (target.closest("#playback")) return false; // Space operates focused transport controls
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

// Discard unfinished audio on navigation; normal Stop still sends the final phrase.
function discardRecording(recording) {
  if (!recording) return;
  recording.node.port.onmessage = null;
  recording.source.disconnect();
  recording.gain.disconnect();
  recording.node.disconnect();
  recording.stream.getTracks().forEach((track) => track.stop());
  if (recording.ctx.state !== "closed") recording.ctx.close().catch(() => {});
}

window.addEventListener("pagehide", () => {
  pageLifetime.abort();
  clearTimeout(healthTimer);
  clearTimeout(turnTimer);
  stopPlayback();
  const microphone = session;
  cancelAnimationFrame(playbackMeterFrame);
  recordingOutput?.ctx.close().catch(() => {});
  const captured = capture;
  session = null;
  capture = null; // track-ended handlers must not flush abandoned recordings
  stopMeter();
  if (captured) cancelAnimationFrame(captured.frame);
  discardRecording(microphone);
  discardRecording(captured?.session);
});

window.addEventListener("pageshow", (event) => {
  // A back/forward-cache restore otherwise revives aborted queues and closed audio contexts.
  if (event.persisted) window.location.reload();
});

loadConfig();
