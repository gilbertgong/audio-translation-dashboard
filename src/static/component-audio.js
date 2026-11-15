// Component audio tests share one microphone/WS lifetime, PCM framing and playback.
(() => {
  const panels = ["echo", "channel"].map(prefix => {
    const meterEl = document.getElementById(`${prefix}-meter`);
    return { prefix, echo: prefix === "echo", button: document.getElementById(`${prefix}-connect`),
      meterEl, meterFill: meterEl.querySelector(".meter-fill"), meterPeak: meterEl.querySelector(".meter-peak"),
      meterDb: document.getElementById(`${prefix}-db`), roundTrip: document.getElementById(`${prefix}-rtt`) };
  });
  const delay = document.getElementById("echo-delay");
  const label = document.getElementById("echo-delay-value");
  const toDb = value => value > 0 ? Math.max(-60, 20 * Math.log10(value)) : -60;
  const percent = db => (db + 60) / 60 * 100;
  let current = null;

  // Same scale, smoothing, peak hold and clipping indication as the Live microphone meter.
  function drawMeter(state, now) {
    if (current !== state) return;
    const { meterEl, meterFill, meterPeak, meterDb, roundTrip } = state.panel;
    const meter = state.meter;
    const db = toDb(meter.rms);
    meter.shown = db > meter.shown ? db : Math.max(db, meter.shown - 1);
    meter.max = Math.max(meter.max, toDb(meter.peak));
    if (meter.peak >= 0.99) meter.clipUntil = now + 1500;
    meter.rms = meter.peak = 0;
    const clipping = now < meter.clipUntil;
    meterFill.style.width = `${percent(meter.shown)}%`;
    meterPeak.style.left = `calc(${percent(meter.max)}% - 2px)`;
    meterEl.classList.toggle("clip", clipping);
    meterEl.classList.toggle("hot", !clipping && meter.shown > -10);
    meterEl.classList.toggle("quiet", meter.shown < -45);
    meterEl.setAttribute("aria-valuenow", String(Math.round(meter.shown)));
    meterDb.classList.toggle("clip", clipping);
    if (clipping) I18n.text(meterDb, I18n.msg("CLIP"));
    else I18n.text(meterDb, meter.shown <= -60 ? "-∞ dB" : `${Math.round(meter.shown)} dB`);
    if (roundTrip && now - state.rttAt >= 500) {
      I18n.text(roundTrip, state.rttCount
        ? I18n.msg("{milliseconds} ms", { milliseconds: (state.rttSum / state.rttCount).toFixed(1) }) : "—");
      state.rttSum = state.rttCount = 0;
      state.rttAt = now;
    }
    meter.frame = requestAnimationFrame(time => drawMeter(state, time));
  }

  function clearPlayback(state) {
    for (const source of state.playing) {
      source.onended = null;
      source.stop();
      source.disconnect();
    }
    state.playing.clear();
    state.next = 0;
  }

  function stop() {
    const state = current;
    if (!state) return;
    current = null; // late permission/setup results must not revive a stopped test
    const { button, meterEl, meterFill, meterPeak, meterDb, roundTrip, prefix } = state.panel;
    state.abort.abort();
    state.uploads.length = 0;
    cancelAnimationFrame(state.meter.frame);
    state.sentAt.clear();
    if (state.node) { state.node.port.onmessage = null; state.node.disconnect(); }
    state.source?.disconnect();
    state.stream?.getTracks().forEach(track => track.stop());
    clearPlayback(state);
    if (state.ctx.state !== "closed") state.ctx.close().catch(() => {});
    try {
      if (state.socket.readyState === WebSocket.OPEN && state.streamId !== undefined) {
        state.socket.send(JSON.stringify({ command: "stop_stream", channel: state.channel, stream_id: state.streamId }));
      }
    } catch {} // the peer may already have disconnected
    state.socket.close();
    meterEl.classList.remove("active", "quiet", "hot", "clip");
    meterEl.setAttribute("aria-valuenow", "-60");
    meterFill.style.width = "0";
    meterPeak.style.left = "0";
    meterDb.classList.remove("clip");
    I18n.text(meterDb, "-∞ dB");
    if (roundTrip) I18n.text(roundTrip, "—");
    button.classList.remove("active");
    I18n.text(button, I18n.msg("Connect"));
    setStatus(`${prefix}-status`, I18n.msg("Disconnected"));
  }

  function fail(state, error) {
    if (current !== state) return;
    stop();
    setStatus(`${state.panel.prefix}-status`, I18n.msg("Audio test: {error}", { error: I18n.errorText(error) }), true);
  }

  function updateDelay() {
    const message = I18n.msg(Number(delay.value) === 5.5 ? "Disabled" : "{seconds} s", { seconds: delay.value });
    I18n.text(label, message);
    I18n.attr(delay, "aria-valuetext", message);
    if (current?.panel.echo) clearPlayback(current); // a change discards old echoes rather than reordering them
  }
  delay.addEventListener("input", updateDelay);
  updateDelay();

  function resultMessage(state, event) {
    if (state.panel.echo) return;
    const results = document.getElementById("channel-results");
    let row = state.rows.get(event.seq);
    if (!row) {
      const element = document.createElement("div");
      element.className = "result";
      const original = document.createElement("div"), translated = document.createElement("div");
      original.className = translated.className = "text";
      const meta = document.createElement("div"), error = document.createElement("div");
      meta.className = "meta"; error.className = "status error";
      const details = document.createElement("details"), summary = document.createElement("summary"), raw = document.createElement("pre");
      I18n.text(summary, I18n.msg("Raw response"));
      details.append(summary, raw); element.append(original, translated, meta, error, details);
      results.append(element); results.hidden = false;
      row = { original, translated, meta, error, raw, data: {} };
      state.rows.set(event.seq, row);
    }
    row.data[event.command] = event.result || event.error;
    if (event.command === "on_transcription") row.original.textContent = event.result.text;
    if (event.command === "on_translation") row.translated.textContent = event.result.translation;
    if (event.error) I18n.text(row.error, I18n.msg("Error: {error}", { error: event.error }));
    const stt = row.data.on_transcription;
    if (stt) I18n.text(row.meta, I18n.msg("{language} · whisper {stt}s · llama {llm}s", {
      language: stt.language || "?", stt: stt.stt_seconds,
      llm: row.data.on_translation?.seconds.toFixed(2) ?? "—",
    }));
    row.raw.textContent = JSON.stringify(row.data, null, 2);
  }

  async function uploadNext(state) {
    if (state.posting || current !== state) return;
    state.posting = true;
    try {
      while (current === state && state.uploads.length) {
        const { samples, seq } = state.uploads.shift();
        try {
          const converted = await resample(samples, state.ctx.sampleRate, sampleRate);
          if (current !== state) return;
          const response = await pageFetch(`/api/channel/translate?channel=${encodeURIComponent(state.channel)}&seq=${seq}`, {
            method: "POST", headers: { "Content-Type": "audio/wav" },
            body: encodeWav(converted, sampleRate), signal: state.abort.signal,
          });
          if (!response.ok) throw new Error(await errorMessage(response));
          await response.json(); // exactly one WAV POST until this completed-text response finishes
        } catch (error) {
          if (current === state) resultMessage(state, { command: "upload_error", seq, error: I18n.errorText(error) });
        }
      }
    } finally { state.posting = false; }
  }

  function receive(state, data) {
    if (typeof data === "string") {
      const event = JSON.parse(data);
      if (event.seq === 1 && !event.command) {
        if (!event.success || !Number.isInteger(event.stream_id)) throw I18n.error("Audio stream could not start.");
        state.streamId = event.stream_id;
        state.ready();
      } else if (event.command === "on_stream_start") {
        const header = Uint8Array.from(atob(event.codec_header), c => c.charCodeAt(0));
        const rate = new DataView(header.buffer).getUint16(0, true);
        const samples = rate * event.packet_duration / 1000;
        if (event.codec !== "pcm_s16le" || header.length !== 4 || !rate ||
            header[2] * header[3] !== event.packet_duration || !Number.isInteger(samples)) {
          throw I18n.error("Unexpected audio response.");
        }
        state.receiving = { id: event.stream_id, rate, samples };
      } else if (event.command === "on_stream_stop") state.receiving = null;
      else if (["on_transcription", "on_translation", "on_speech_error"].includes(event.command)) resultMessage(state, event);
      return;
    }
    const packet = new DataView(data);
    if (!state.receiving || packet.byteLength !== 9 + state.receiving.samples * 2 || packet.getUint8(0) !== 1 ||
        packet.getUint32(1) !== state.receiving.id) throw I18n.error("Unexpected audio response.");
    // This echo route returns exactly one numbered packet per send, in order.
    const id = packet.getUint32(5);
    const sent = state.sentAt.get(id);
    if (sent !== undefined) {
      state.sentAt.delete(id);
      state.rttSum += performance.now() - sent;
      state.rttCount++;
    }
    if (state.panel.echo && Number(delay.value) === 5.5) return; // still transmit/receive; only local playback is disabled
    const buffer = state.ctx.createBuffer(1, state.receiving.samples, state.receiving.rate);
    const samples = buffer.getChannelData(0);
    for (let i = 0; i < samples.length; i++) samples[i] = packet.getInt16(9 + i * 2, true) / 32768;
    const source = state.ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(state.ctx.destination);
    // A small scheduling buffer is independent of the user-selected additional delay.
    const at = state.next < state.ctx.currentTime || !state.next
      ? state.ctx.currentTime + (state.panel.echo ? 0.032 + Number(delay.value) : playbackBufferMs / 1000) : state.next;
    state.next = at + buffer.duration;
    state.playing.add(source);
    source.onended = () => { state.playing.delete(source); source.disconnect(); };
    source.start(at);
  }

  for (const panel of panels) panel.button.addEventListener("click", async () => {
    if (current?.panel === panel) { stop(); return; }
    stop(); // switching panels also cancels pending permission/setup/upload work
    const { button, meterEl, prefix } = panel;
    let state, ctx, socket;
    try {
      // Web Audio resamples the mic into 16 kHz for both WS packets and segmentation.
      ctx = new AudioContext({ sampleRate: 16000 });
      const url = new URL("api/audio", window.location.href);
      url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
      url.searchParams.set("channel", crypto.randomUUID());
      url.searchParams.set("echo", panel.echo ? "1" : "0");
      socket = new WebSocket(url);
      socket.binaryType = "arraybuffer";
      state = { panel, ctx, socket, abort: new AbortController(), uploads: [], rows: new Map(), nextSeq: 1, posting: false, channel: url.searchParams.get("channel"), playing: new Set(), next: 0,
        samples: ctx.sampleRate * 16 / 1000, position: 0, sentAt: new Map(), sentCount: 0,
        rttSum: 0, rttCount: 0, rttAt: 0,
        meter: { rms: 0, peak: 0, shown: -60, max: -60, clipUntil: 0, frame: 0 } };
      current = state;
      if (!panel.echo) {
        const results = document.getElementById("channel-results");
        results.replaceChildren(); results.hidden = true;
      }
      I18n.text(button, I18n.msg("Disconnect"));
      button.classList.add("active");
      setStatus(`${prefix}-status`, I18n.msg("Connecting..."));
      const resumed = ctx.resume(); // invoked in the button gesture, before network/permission awaits
      const header = new DataView(new ArrayBuffer(4));
      header.setUint16(0, ctx.sampleRate, true);
      header.setUint8(2, 1);
      header.setUint8(3, 16);
      state.header = btoa(String.fromCharCode(...new Uint8Array(header.buffer)));
      await Promise.all([resumed, new Promise((resolve, reject) => {
        state.ready = resolve;
        socket.addEventListener("open", () => socket.send(JSON.stringify({ command: "start_stream", seq: 1,
          channel: state.channel, type: "audio", codec: "pcm_s16le", codec_header: state.header, packet_duration: 16 })));
        socket.addEventListener("message", ({ data }) => {
          if (current !== state) return;
          try { receive(state, data); } catch (error) { reject(error); fail(state, error); }
        });
        socket.addEventListener("error", () => { const error = I18n.error("WebSocket connection failed."); reject(error); fail(state, error); });
        socket.addEventListener("close", () => {
          reject(I18n.error("WebSocket connection closed."));
          fail(state, I18n.error("WebSocket connection closed."));
        });
      })]);
      if (current !== state) return;
      const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1,
        autoGainControl: audioProcessing.auto_gain_control ?? false,
        noiseSuppression: audioProcessing.noise_suppression ?? true,
        echoCancellation: audioProcessing.echo_cancellation ?? true } });
      if (current !== state) { stream.getTracks().forEach(track => track.stop()); return; }
      state.stream = stream;
      await ctx.audioWorklet.addModule("recorder-worklet.js");
      if (current !== state) return;
      state.source = ctx.createMediaStreamSource(stream);
      state.node = new AudioWorkletNode(ctx, "recorder");
      const newPacket = () => {
        state.packet = new DataView(new ArrayBuffer(9 + state.samples * 2));
        state.packet.setUint8(0, 1);
        state.packet.setUint32(1, state.streamId);
      };
      newPacket();
      if (!panel.echo) state.splitter = SegmentSplitter.create(ctx.sampleRate,
        { ...SegmentSplitter.defaults, ...splitSettings }, {
          onSegment: samples => { state.uploads.push({ samples, seq: String(state.nextSeq++) }); uploadNext(state); },
        });
      state.node.port.onmessage = ({ data }) => {
        if (current !== state) return;
        try {
          let sum = 0, peak = 0;
          for (const sample of data) {
            sum += sample * sample;
            peak = Math.max(peak, Math.abs(sample));
            const value = Math.max(-1, Math.min(1, sample));
            state.packet.setInt16(9 + state.position * 2, Math.min(32767, Math.round(value * 32768)), true);
            if (++state.position === state.samples) {
              if (panel.echo) {
                state.sentAt.set(state.sentCount, performance.now());
                state.sentCount = (state.sentCount + 1) >>> 0;
              }
              socket.send(state.packet.buffer);
              state.position = 0;
              newPacket();
            }
          }
          const rms = Math.sqrt(sum / data.length);
          if (state.splitter) SegmentSplitter.push(state.splitter, data, rms);
          state.meter.rms = Math.max(state.meter.rms, rms);
          state.meter.peak = Math.max(state.meter.peak, peak);
        } catch (error) { fail(state, error); }
      };
      state.source.connect(state.node);
      state.node.connect(ctx.destination); // the recorder outputs silence
      meterEl.classList.add("active");
      state.rttAt = performance.now();
      state.meter.frame = requestAnimationFrame(now => drawMeter(state, now));
      setStatus(`${prefix}-status`, I18n.msg("Connected"));
    } catch (error) {
      if (state) fail(state, error);
      else {
        if (ctx && ctx.state !== "closed") ctx.close().catch(() => {});
        socket?.close();
        setStatus(`${prefix}-status`, I18n.msg("Audio test: {error}", { error: I18n.errorText(error) }), true);
      }
    }
  });
  window.addEventListener("pagehide", stop);
})();
