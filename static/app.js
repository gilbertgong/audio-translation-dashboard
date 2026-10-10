// Dashboard front end: microphone capture -> 16-bit mono WAV -> POST to backend.

const recordBtn = document.getElementById("record-btn");
const statusEl = document.getElementById("status");
const logEl = document.getElementById("activity-log");
const sidebarToggle = document.getElementById("sidebar-toggle");
const clearLogBtn = document.getElementById("clear-log");

let targetSampleRate = 16000;
let session = null; // active recording: { stream, ctx, source, node, chunks }

function setStatus(text) {
  statusEl.textContent = text;
}

// Activity log (sidebar)

function addEntry(text, isError = false) {
  const placeholder = logEl.querySelector(".placeholder");
  if (placeholder) placeholder.remove();
  const div = document.createElement("div");
  div.className = isError ? "entry error" : "entry";
  const time = document.createElement("time");
  time.textContent = new Date().toLocaleTimeString();
  div.append(time, text);
  logEl.appendChild(div);
  logEl.scrollTop = logEl.scrollHeight;
}

clearLogBtn.addEventListener("click", () => {
  logEl.innerHTML = '<p class="placeholder">No activity yet.</p>';
});

// Sidebar visibility, remembered per browser.

function setSidebarHidden(hidden) {
  document.body.classList.toggle("sidebar-hidden", hidden);
  sidebarToggle.setAttribute("aria-expanded", String(!hidden));
  try {
    localStorage.setItem("sidebarHidden", hidden ? "1" : "0");
  } catch {}
}

sidebarToggle.addEventListener("click", () => {
  setSidebarHidden(!document.body.classList.contains("sidebar-hidden"));
});

try {
  const saved = localStorage.getItem("sidebarHidden");
  // Default: visible on wide screens, hidden on narrow ones.
  setSidebarHidden(saved === null ? window.innerWidth <= 700 : saved === "1");
} catch {}

async function loadConfig() {
  try {
    const res = await fetch("/api/config");
    targetSampleRate = (await res.json()).sample_rate;
  } catch (err) {
    console.warn("Could not load config, using defaults", err);
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

async function uploadWav(wav) {
  const res = await fetch("/api/recordings", {
    method: "POST",
    headers: { "Content-Type": "audio/wav" },
    body: wav,
  });
  const body = await res.json();
  if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
  return body;
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
      setStatus("Saving...");
      const wav = await stopRecording();
      const seconds = (wav.size - 44) / 2 / targetSampleRate;
      const result = await uploadWav(wav);
      addEntry(`Saved ${seconds.toFixed(1)} s recording to ${result.file}`);
      setStatus("Idle");
    }
  } catch (err) {
    console.error(err);
    addEntry(`Error: ${err.message}`, true);
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
