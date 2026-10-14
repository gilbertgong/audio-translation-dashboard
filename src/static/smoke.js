// Smoke test page: the real dashboard in a frame, with known speech
// (voices/beth_thomas/test_audio/, served by /api/test-audio) as its microphone and its
// captured audio. The dashboard's getUserMedia and getDisplayMedia are
// replaced with streams this page plays the clips into, on a schedule; then
// the dashboard's rows, statuses and playback are checked. Nothing in the
// dashboard knows it's being tested. Uses the running servers.

const $ = (id) => document.getElementById(id);
const frame = $("dashboard");

// The scenario. Each source plays its clips in order, a gap apart, starting
// at its offset (seconds after both sources are running; the splitter
// measures the background for the first moments). expect: the language
// whisper should hear, or "other" for one outside [languages] (rechecked).
const GAP_S = 2;
const SOURCES = {
  mic: { start: 1, clips: [{ file: "beth_thomas/beth_thomas.wav", name: "English", expect: "english" }] },
  capture: { start: 3, channel: 1, clips: [ // right channel; the left stays silent
    { file: "beth_thomas/test_audio/beth_chinese.wav", name: "Chinese", expect: "chinese" },
    { file: "beth_thomas/test_audio/beth_japanese.wav", name: "Japanese", expect: "other" },
  ] },
};
const SETTLE_MS = 90000; // after the audio ends: for the last translations to finish
const SPEECH_MS = 30000; // for automatic playback to start, and its audio to begin

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitFor(what, test, timeoutMs, everyMs = 100) {
  const end = performance.now() + timeoutMs;
  for (;;) {
    const value = test();
    if (value) return value;
    if (performance.now() > end) throw new Error(`timed out waiting for ${what}`);
    await sleep(everyMs);
  }
}

// --- Results

function setStatus(text, isError = false) {
  $("run-status").textContent = text;
  $("run-status").classList.toggle("error", isError);
}

const results = [];

function check(name, ok, detail = "") {
  results.push({ name, ok, detail });
  const li = document.createElement("li");
  li.className = ok ? "pass" : "fail";
  li.textContent = `${ok ? "✓" : "✗"} ${name}`;
  if (detail) {
    const span = document.createElement("span");
    span.className = "detail";
    span.textContent = ` — ${detail}`;
    li.append(span);
  }
  $("checks").append(li);
  return ok;
}

// --- The dashboard in the frame

// The dashboard's own top-level variables (session, capture, pending, speech,
// ...), read inside the frame.
const dash = (expr) => frame.contentWindow.eval(expr);
const dashEl = (id) => frame.contentDocument.getElementById(id);

async function loadDashboard() {
  frame.src = `/?smoke=${Date.now()}`;
  await new Promise((resolve) => frame.addEventListener("load", resolve, { once: true }));
  // loadConfig has run once the languages are known.
  await waitFor("the dashboard's settings", () => dash("language1"), 10000);
}

// The fake sources: each a stream from this page's AudioContext, silent until
// its clips are scheduled. The dashboard asks for them on its buttons' clicks.
function fakeSources(ctx) {
  const mic = ctx.createMediaStreamDestination();
  mic.channelCount = 1;
  const capture = ctx.createMediaStreamDestination();
  capture.channelCount = 2;
  capture.channelInterpretation = "discrete";
  const merger = ctx.createChannelMerger(2);
  merger.connect(capture);
  const devices = frame.contentWindow.navigator.mediaDevices;
  devices.getUserMedia = async () => mic.stream;
  devices.getDisplayMedia = async () => {
    const video = document.createElement("canvas").captureStream(1).getVideoTracks();
    return new MediaStream([...capture.stream.getAudioTracks(), ...video]);
  };
  return {
    mic: (node) => node.connect(mic),
    capture: (node) => node.connect(merger, 0, SOURCES.capture.channel),
  };
}

// Schedule every clip from t0 (ctx time); returns [{source, name, expect, from, to}]
// in scenario seconds.
function schedule(ctx, buffers, connect, t0) {
  const timeline = [];
  for (const [source, { start, clips }] of Object.entries(SOURCES)) {
    let at = start;
    for (const clip of clips) {
      const node = new AudioBufferSourceNode(ctx, { buffer: buffers[clip.file] });
      connect[source](node);
      node.start(t0 + at);
      timeline.push({ source, name: clip.name, expect: clip.expect, from: at, to: at + node.buffer.duration });
      at += node.buffer.duration + GAP_S;
    }
  }
  return timeline;
}

// --- The run

async function run() {
  results.length = 0;
  $("checks").replaceChildren();
  $("rows").replaceChildren();
  $("rows-details").hidden = true;
  let ctx = null;
  try {
    setStatus("Checking the servers…");
    const config = await (await fetch("/api/config")).json();
    if (config.mock) throw new Error("the dashboard is in mock mode: the smoke test needs the real servers");
    const health = await (await fetch("/api/health")).json();
    const down = health.services.filter((s) => s.state !== "ok");
    if (!check("servers are up", !down.length, down.map((s) => `${s.name}: ${s.state}`).join(", "))) return;

    setStatus("Loading the clips…");
    ctx = new AudioContext(); // created during the click, so it may play
    const buffers = {};
    for (const { clips } of Object.values(SOURCES)) {
      for (const clip of clips) {
        const res = await fetch(`/api/test-audio?file=${encodeURIComponent(clip.file)}`);
        if (!res.ok) throw new Error(`${clip.file}: HTTP ${res.status} (see src/tools/generate_test_audio.py)`);
        buffers[clip.file] = await ctx.decodeAudioData(await res.arrayBuffer());
      }
    }

    setStatus("Loading the dashboard…");
    await loadDashboard();
    const connect = fakeSources(ctx);
    // Continuous Audio Mode for the microphone, and automatic playback.
    dashEl("auto-split").checked = true;
    const autoPlay = dashEl("auto-play");
    autoPlay.checked = true;
    autoPlay.dispatchEvent(new Event("change"));

    // Note each row as it appears: when (scenario seconds), which source.
    const rows = new Map(); // row element -> seconds
    let t0 = Infinity;
    new frame.contentWindow.MutationObserver((records) => {
      for (const r of records) {
        for (const node of r.addedNodes) {
          if (node.classList?.contains("row")) rows.set(node, ctx.currentTime - t0);
        }
      }
    }).observe(dashEl("translation-window"), { childList: true });

    setStatus("Starting the microphone and the capture…");
    dashEl("record-btn").click();
    await waitFor("the microphone to start", () => dash("session"), 5000);
    dashEl("capture-btn").click();
    await waitFor("the capture to start", () => dash("capture"), 5000);
    t0 = ctx.currentTime + 0.2;
    const timeline = schedule(ctx, buffers, connect, t0);
    const audioEnd = Math.max(...timeline.map((c) => c.to));
    const now = () => ctx.currentTime - t0;

    // While the audio plays: automatic playback must wait for the end of the
    // turn (nothing spoken while the sources are busy); translations queue.
    let earlySpeech = null;
    let maxQueue = 0;
    while (now() < audioEnd) {
      setStatus(`Playing the clips… ${Math.round(now())} / ${Math.round(audioEnd)} s`);
      maxQueue = Math.max(maxQueue, dash("speechQueue.length"));
      if (dash("speech?.item") && !earlySpeech) earlySpeech = now();
      // The capture's channel is chosen by its meters (animation frames); if
      // the frame isn't being drawn, choose it as they would.
      if (now() > SOURCES.capture.start + 3 && dash("capture.selected") === null) {
        dash(`selectChannel(${SOURCES.capture.channel})`);
      }
      await sleep(100);
    }
    check("the capture translated the channel with sound",
          dash("capture?.selected") === SOURCES.capture.channel,
          `channel ${dash("capture?.selected")}`);
    check("nothing was spoken automatically while speech was going on", earlySpeech === null,
          earlySpeech === null ? "" : `started at ${earlySpeech.toFixed(1)} s`);

    setStatus("Waiting for automatic playback…");
    const turnPauseMs = dash("turnPauseMs");
    const started = await waitFor("automatic playback", () => {
      maxQueue = Math.max(maxQueue, dash("speechQueue.length"));
      return dash("speech?.item") ? { quietMs: dash("performance.now() - lastSpeechAt") } : null;
    }, SPEECH_MS, 20).catch(() => null);
    check("translations queued for automatic playback", maxQueue > 0, `up to ${maxQueue} waiting`);
    check("automatic playback started after the pause", !!started && started.quietMs >= turnPauseMs - 50,
          started ? `after ${(started.quietMs / 1000).toFixed(1)} s of quiet (turn pause ${turnPauseMs / 1000} s)`
            : "it didn't start");
    if (started) {
      const playing = await waitFor("the speech's audio", () =>
        frame.contentDocument.querySelector('.timing[data-kind="tts"]:not(.pending) .secs')
          ?.textContent.match(/\d/), SPEECH_MS, 100).catch(() => null);
      check("the speech's audio arrived", !!playing);
      await sleep(3000); // a moment of it
    }
    dash("stopPlayback()");

    setStatus("Waiting for the last translations…");
    await waitFor("the translations to finish", () => dash("pending.mic + pending.capture") === 0, SETTLE_MS, 250)
      .catch(() => {});
    dashEl("record-btn").click();
    await waitFor("the microphone to stop", () => !dash("session"), 5000).catch(() => {});
    dashEl("capture-btn").click();
    await waitFor("the capture to stop", () => !dash("capture"), 5000).catch(() => {});
    await sleep(300);

    checkRows(rows, timeline, dash("languageChecks"));
    const state = (key) => dashEl(`status-${key}`).textContent;
    check("the statuses are back to off", state("capture") === "off" && state("mic") === "off"
          && !["active", "waiting"].includes(dashEl("status-playback").dataset.state),
          `${state("capture")} | ${state("mic")} | ${state("playback")}`);
  } catch (err) {
    console.error(err);
    check("the test ran to the end", false, err.message);
  } finally {
    ctx?.close();
    const failed = results.filter((r) => !r.ok).length;
    setStatus(failed ? `${failed} of ${results.length} checks failed` : `All ${results.length} checks passed`,
              failed > 0);
  }
}

// The rows: each source produced some, each was transcribed and translated in
// the expected language, none failed.
function checkRows(rows, timeline, languageChecks) {
  const found = [...rows].map(([row, at]) => {
    // How the phrase was split, which names its source ("captured audio (pause)").
    const source = (row.info || []).some((i) => i.includes("captured audio")) ? "capture" : "mic";
    // The clip playing when the phrase ended (the row appears just after).
    const clip = timeline.filter((c) => c.source === source && c.from <= at).pop();
    return {
      row, at, source, clip,
      language: (row.dataset.language || "").toLowerCase(),
      text: row.querySelector(".original").textContent,
      // The text alone, without the ▶ button that follows it.
      translation: [...row.querySelector(".translation").childNodes]
        .filter((n) => n.nodeType === Node.TEXT_NODE).map((n) => n.textContent).join(""),
      failed: !!row.querySelector(".error"), // a failed step's line, timing or detail
      rechecked: !!row.querySelector('.timing[data-kind="recheck"]'),
    };
  });
  showRows(found);

  for (const clip of timeline) {
    const mine = found.filter((r) => r.clip === clip);
    const label = `${clip.source}: ${clip.name}`;
    if (!check(`${label}: split into phrases`, mine.length >= 2, `${mine.length} rows`)) continue;
    const empty = mine.filter((r) => !r.text || r.text === "(no speech detected)" || !r.translation
                              || r.translation.startsWith("("));
    check(`${label}: every phrase transcribed and translated`, !empty.length && !mine.some((r) => r.failed),
          [empty.length && `${empty.length} empty`, mine.filter((r) => r.failed).length && "errors"]
            .filter(Boolean).join(", "));
    if (clip.expect === "other") {
      if (languageChecks) {
        const rechecked = mine.filter((r) => r.rechecked).length;
        check(`${label}: rechecked as outside the language pair`, rechecked === mine.length,
              `${rechecked} of ${mine.length} rechecked`);
      }
    } else {
      const wrong = mine.filter((r) => r.language !== clip.expect);
      check(`${label}: heard as ${clip.expect}`, !wrong.length,
            wrong.map((r) => r.language || "none").join(", "));
    }
    const tagged = mine.filter((r) => /^\s*\[[^\]]*\]|\[[^\]]*\]\s*$/.test(r.translation));
    check(`${label}: language tags stripped from the translations`, !tagged.length, `${tagged.length} left`);
  }
}

function showRows(found) {
  for (const r of found) {
    const tr = document.createElement("tr");
    if (r.failed) tr.className = "fail";
    for (const text of [r.at.toFixed(1), r.source, r.clip?.name || "?", r.language + (r.rechecked ? " (rechecked)" : ""),
                        r.text, r.translation]) {
      const td = document.createElement("td");
      td.textContent = text;
      tr.append(td);
    }
    $("rows").append(tr);
  }
  $("rows-details").hidden = false;
}

$("run").addEventListener("click", async () => {
  $("run").disabled = true;
  try {
    await run();
  } finally {
    $("run").disabled = false;
  }
});
