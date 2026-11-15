// Component echo: one microphone, connection-local channel and browser-only delay.
(() => {
  const button = document.getElementById("echo-connect");
  const delay = document.getElementById("echo-delay");
  const label = document.getElementById("echo-delay-value");
  const meterEl = document.getElementById("echo-meter");
  const meterFill = meterEl.querySelector(".meter-fill");
  const meterPeak = meterEl.querySelector(".meter-peak");
  const meterDb = document.getElementById("echo-db");
  const roundTrip = document.getElementById("echo-rtt");
  const toDb = value => value > 0 ? Math.max(-60, 20 * Math.log10(value)) : -60;
  const percent = db => (db + 60) / 60 * 100;
  let current = null;

  // Same scale, smoothing, peak hold and clipping indication as the Live microphone meter.
  function drawMeter(state, now) {
    if (current !== state) return;
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
    if (now - state.rttAt >= 500) {
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
    current = null; // late permission/setup results must not revive a stopped test
    if (state) {
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
    }
    meterEl.classList.remove("active", "quiet", "hot", "clip");
    meterEl.setAttribute("aria-valuenow", "-60");
    meterFill.style.width = "0";
    meterPeak.style.left = "0";
    meterDb.classList.remove("clip");
    I18n.text(meterDb, "-∞ dB");
    I18n.text(roundTrip, "—");
    button.classList.remove("active");
    I18n.text(button, I18n.msg("Connect"));
  }

  function fail(state, error) {
    if (current !== state) return;
    stop();
    setStatus("echo-status", I18n.msg("Echo test: {error}", { error: I18n.errorText(error) }), true);
  }

  function updateDelay() {
    const message = I18n.msg(Number(delay.value) === 5.5 ? "Disabled" : "{seconds} s", { seconds: delay.value });
    I18n.text(label, message);
    I18n.attr(delay, "aria-valuetext", message);
    if (current) clearPlayback(current); // a change discards old echoes rather than reordering them
  }
  delay.addEventListener("input", updateDelay);
  updateDelay();

  function receive(state, data) {
    if (typeof data === "string") {
      const event = JSON.parse(data);
      if (event.seq === 1) {
        if (!event.success || !Number.isInteger(event.stream_id)) throw I18n.error("Echo stream could not start.");
        state.streamId = event.stream_id;
        state.ready();
      } else if (event.command === "on_stream_start") {
        if (event.stream_id !== state.streamId || event.codec !== "pcm_s16le" || event.codec_header !== state.header) {
          throw I18n.error("Unexpected echo response.");
        }
        state.receiving = true;
      } else if (event.command === "on_stream_stop") state.receiving = false;
      return;
    }
    const packet = new DataView(data);
    if (!state.receiving || packet.byteLength !== state.packet.byteLength || packet.getUint8(0) !== 1 ||
        packet.getUint32(1) !== state.streamId) throw I18n.error("Unexpected echo response.");
    // This echo route returns exactly one numbered packet per send, in order.
    const id = packet.getUint32(5);
    const sent = state.sentAt.get(id);
    if (sent !== undefined) {
      state.sentAt.delete(id);
      state.rttSum += performance.now() - sent;
      state.rttCount++;
    }
    if (Number(delay.value) === 5.5) return; // still transmit/receive; only local playback is disabled
    const buffer = state.ctx.createBuffer(1, state.samples, state.ctx.sampleRate);
    const samples = buffer.getChannelData(0);
    for (let i = 0; i < samples.length; i++) samples[i] = packet.getInt16(9 + i * 2, true) / 32768;
    const source = state.ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(state.ctx.destination);
    // A small scheduling buffer is independent of the user-selected additional delay.
    const at = state.next < state.ctx.currentTime || !state.next
      ? state.ctx.currentTime + 0.032 + Number(delay.value) : state.next;
    state.next = at + buffer.duration;
    state.playing.add(source);
    source.onended = () => { state.playing.delete(source); source.disconnect(); };
    source.start(at);
  }

  button.addEventListener("click", async () => {
    if (current) { stop(); setStatus("echo-status", I18n.msg("Disconnected")); return; }
    let state, ctx, socket;
    try {
      ctx = new AudioContext({ sampleRate: 48000 });
      const url = new URL("api/audio", window.location.href);
      url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
      url.searchParams.set("channel", crypto.randomUUID());
      url.searchParams.set("echo", "1");
      socket = new WebSocket(url);
      socket.binaryType = "arraybuffer";
      state = { ctx, socket, channel: url.searchParams.get("channel"), playing: new Set(), next: 0,
        samples: ctx.sampleRate * 16 / 1000, position: 0, sentAt: new Map(), sentCount: 0,
        rttSum: 0, rttCount: 0, rttAt: 0,
        meter: { rms: 0, peak: 0, shown: -60, max: -60, clipUntil: 0, frame: 0 } };
      current = state;
      I18n.text(button, I18n.msg("Disconnect"));
      button.classList.add("active");
      setStatus("echo-status", I18n.msg("Connecting..."));
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
              state.sentAt.set(state.sentCount, performance.now());
              state.sentCount = (state.sentCount + 1) >>> 0;
              socket.send(state.packet.buffer);
              state.position = 0;
              newPacket();
            }
          }
          state.meter.rms = Math.max(state.meter.rms, Math.sqrt(sum / data.length));
          state.meter.peak = Math.max(state.meter.peak, peak);
        } catch (error) { fail(state, error); }
      };
      state.source.connect(state.node);
      state.node.connect(ctx.destination); // the recorder outputs silence
      meterEl.classList.add("active");
      state.rttAt = performance.now();
      state.meter.frame = requestAnimationFrame(now => drawMeter(state, now));
      setStatus("echo-status", I18n.msg("Connected"));
    } catch (error) {
      if (state) fail(state, error);
      else {
        if (ctx && ctx.state !== "closed") ctx.close().catch(() => {});
        socket?.close();
        setStatus("echo-status", I18n.msg("Echo test: {error}", { error: I18n.errorText(error) }), true);
      }
    }
  });
  window.addEventListener("pagehide", stop);
})();
