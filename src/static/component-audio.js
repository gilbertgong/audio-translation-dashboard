// Component audio tests share one microphone/WS lifetime, framing and playback.
(() => {
  const panels = ["echo", "channel"].map(prefix => {
    const meterEl = document.getElementById(`${prefix}-meter`);
    return { prefix, echo: prefix === "echo", button: document.getElementById(`${prefix}-connect`),
      meterEl, meterFill: meterEl.querySelector(".meter-fill"), meterPeak: meterEl.querySelector(".meter-peak"),
      meterDb: document.getElementById(`${prefix}-db`), roundTrip: document.getElementById(`${prefix}-rtt`) };
  });
  const delay = document.getElementById("echo-delay");
  const label = document.getElementById("echo-delay-value");
  const opusToggle = document.getElementById("echo-opus");
  const opusStatus = document.getElementById("echo-opus-status");
  const opusConfig = { codec: "opus", sampleRate: 16000, numberOfChannels: 1, bitrate: 24000,
    opus: { format: "opus", frameDuration: 20000 } };
  let opusAvailable = false;
  let useOpus = false;
  const toDb = value => value > 0 ? Math.max(-60, 20 * Math.log10(value)) : -60;
  const percent = db => (db + 60) / 60 * 100;
  let current = null;

  (async () => {
    try {
      if (typeof AudioEncoder !== "undefined" && typeof AudioDecoder !== "undefined" &&
          typeof AudioData !== "undefined" && typeof EncodedAudioChunk !== "undefined") {
        const [encoder, decoder] = await Promise.all([
          AudioEncoder.isConfigSupported(opusConfig),
          AudioDecoder.isConfigSupported({ codec: "opus", sampleRate: 16000, numberOfChannels: 1 }),
        ]);
        opusAvailable = encoder.supported && decoder.supported;
      }
    } catch {} // missing/unsupported WebCodecs leaves the PCM path available
    opusToggle.disabled = !opusAvailable;
    I18n.text(opusStatus, I18n.msg(opusAvailable ? "Opus available" : "Opus unavailable"));
  })();

  opusToggle.addEventListener("click", () => {
    if (!opusAvailable) return;
    useOpus = !useOpus;
    I18n.text(opusToggle, useOpus ? "Opus" : "PCM");
    opusToggle.setAttribute("aria-pressed", String(useOpus));
    if (current?.panel.echo) {
      const state = current;
      if (state.connected && !state.micStreams.length) setStatus("echo-status",
        I18n.msg("Connected: {codec}", { codec: useOpus ? "opus" : "PCM" }));
    }
  });

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
    state.playbackEpoch++;
    for (const stream of state.decoders) stream.ignoreBefore = stream.timestamp;
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
    for (const stream of state.micStreams) if (stream.encoder?.state !== "closed") stream.encoder?.close();
    for (const stream of state.decoders) if (stream.decoder.state !== "closed") stream.decoder.close();
    if (state.ctx.state !== "closed") state.ctx.close().catch(() => {});
    try {
      const outgoing = state.micStreams[0];
      if (state.socket.readyState === WebSocket.OPEN && outgoing?.id !== undefined) {
        state.socket.send(JSON.stringify({ command: "stop_stream", channel: state.channel, stream_id: outgoing.id }));
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
    if (state.panel.echo) opusToggle.disabled = !opusAvailable;
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

  function resultMessage(state, event, messageId = event.message_id ?? state.streamResults.get(event.stream_id)?.message_id, rawResponse) {
    if (state.panel.echo) return;
    if (event.command === "on_transcription") {
      const stream = state.streamResults.get(event.stream_id) || {};
      stream.transcription = event;
      stream.rawTranscription = rawResponse;
      state.streamResults.set(event.stream_id, stream);
      if (stream.message_id === undefined) return; // start may arrive after the text
      messageId = stream.message_id;
      delete stream.transcription;
      delete stream.rawTranscription;
    } else if (event.message_id !== undefined && event.stream_id !== undefined) {
      const stream = state.streamResults.get(event.stream_id) || {};
      stream.message_id = event.message_id;
      state.streamResults.set(event.stream_id, stream);
      if (stream.transcription) resultMessage(state, stream.transcription, undefined, stream.rawTranscription);
    }
    const results = document.getElementById("channel-results");
    let row = state.rows.get(messageId);
    if (!row) {
      const element = document.createElement("div");
      element.className = "result";
      const original = document.createElement("div"), translated = document.createElement("div");
      original.className = translated.className = "text";
      const meta = document.createElement("div"), error = document.createElement("div");
      meta.className = "meta"; error.className = "status error";
      const details = document.createElement("details"), summary = document.createElement("summary"), raw = document.createElement("div");
      I18n.text(summary, I18n.msg("Raw response"));
      const postSection = document.createElement("details"), postLabel = document.createElement("summary"), post = document.createElement("pre");
      I18n.text(postLabel, I18n.msg("POST response"));
      postSection.hidden = true;
      postSection.append(postLabel, post);
      details.append(summary, raw); element.append(original, translated, meta, error, details, postSection);
      results.append(element); results.hidden = false;
      row = { original, translated, meta, error, raw, postSection, post, data: {} };
      state.rows.set(messageId, row);
    }
    if (event.command === "post_response") {
      row.postSection.hidden = false;
      row.post.textContent = JSON.stringify(JSON.parse(event.response), null, 2);
    } else row.data[event.command] = event.error || event;
    if (event.command === "on_transcription") {
      row.original.textContent = event.text;
      row.translated.textContent = event.translations.map(item => item.message).join("\n");
    }
    if (event.error) I18n.text(row.error, I18n.msg("Error: {error}", { error: event.error }));
    const stt = row.data.on_transcription;
    if (stt) I18n.text(row.meta, I18n.msg("{language} · whisper {stt}s · llama {llm}s", {
      language: stt.language || "?", stt: stt.stt_seconds,
      llm: stt.llm_seconds?.toFixed(2) ?? "—",
    }));
    if (rawResponse !== undefined) {
      const message = document.createElement("pre");
      message.textContent = JSON.stringify(JSON.parse(rawResponse), null, 2);
      row.raw.append(message);
    }
  }

  async function uploadNext(state) {
    if (state.posting || current !== state) return;
    state.posting = true;
    try {
      while (current === state && state.uploads.length) {
        const { samples } = state.uploads.shift();
        try {
          const converted = await resample(samples, state.ctx.sampleRate, sampleRate);
          if (current !== state) return;
          const response = await pageFetch(`/api/channel/translate?channel=${encodeURIComponent(state.channel)}`, {
            method: "POST", headers: { "Content-Type": "audio/wav" },
            body: encodeWav(converted, sampleRate), signal: state.abort.signal,
          });
          if (!response.ok) throw new Error(await errorMessage(response));
          const rawResponse = await response.text(); // keep one POST in flight through completed processing
          const body = JSON.parse(rawResponse);
          if (current === state) resultMessage(state, { command: "post_response", response: rawResponse }, body.message_id);
        } catch (error) {
          if (current === state) resultMessage(state, { command: "upload_error", error: I18n.errorText(error) }, Symbol());
        }
      }
    } finally { state.posting = false; }
  }

  function finishMic(state, stream) {
    state.socket.send(JSON.stringify({ command: "stop_stream", channel: state.channel, stream_id: stream.id }));
    state.micStreams.shift();
    if (state.panel.echo) {
      const codec = state.micStreams[0]?.codec ?? (useOpus ? "opus" : "pcm_s16le");
      setStatus("echo-status", I18n.msg("Connected: {codec}", { codec: codec === "opus" ? "opus" : "PCM" }));
    }
    sendMic(state);
  }

  function sendOpus(state, stream) {
    if (!stream.encoder) {
      stream.encoder = new AudioEncoder({
        output: chunk => {
          if (current !== state) return;
          try {
            const packet = new Uint8Array(9 + chunk.byteLength);
            const header = new DataView(packet.buffer);
            header.setUint8(0, 1); header.setUint32(1, stream.id); header.setUint32(5, stream.packetId);
            chunk.copyTo(packet.subarray(9));
            state.sentAt.set(`${stream.id}:${stream.packetId}`, performance.now());
            state.socket.send(packet.buffer);
            stream.packetId = (stream.packetId + 1) >>> 0;
          } catch (error) { fail(state, error); }
        }, error: error => fail(state, error),
      });
      stream.encoder.configure(opusConfig);
      stream.frame = new Float32Array(stream.samples);
      stream.timestamp = 0;
    }
    const encode = () => {
      const audio = new AudioData({ format: "f32", sampleRate: state.ctx.sampleRate,
        numberOfFrames: stream.samples, numberOfChannels: 1, timestamp: stream.timestamp, data: stream.frame });
      try { stream.encoder.encode(audio); } finally { audio.close(); }
      stream.timestamp += stream.frameMs * 1000;
      stream.frame.fill(0); stream.position = 0;
    };
    for (const block of stream.blocks) for (const sample of block) {
      stream.frame[stream.position++] = sample;
      if (stream.position === stream.samples) encode();
    }
    stream.blocks.length = 0;
    if (stream.ended && !stream.flushing) {
      if (stream.position) encode();
      stream.flushing = true;
      stream.encoder.flush().then(() => {
        if (current !== state) return;
        stream.encoder.close();
        finishMic(state, stream);
      }).catch(error => fail(state, error));
    }
  }

  // Speech streams wait for their own start acknowledgement before sending audio.
  // A pause can finish one while its acknowledgement is still in flight.
  function sendMic(state) {
    const stream = state.micStreams[0];
    if (!stream) return;
    if (!stream.requested) {
      stream.requested = true;
      state.socket.send(JSON.stringify({ command: "start_stream", seq: stream.seq,
        channel: state.channel, type: "audio", codec: stream.codec, codec_header: stream.header, packet_duration: stream.frameMs }));
    }
    if (stream.id === undefined) return;
    if (stream.codec === "opus") { sendOpus(state, stream); return; }
    const sendPacket = () => {
      stream.packet.setUint8(0, 1);
      stream.packet.setUint32(1, stream.id);
      stream.packet.setUint32(5, stream.packetId);
      if (state.panel.echo) state.sentAt.set(`${stream.id}:${stream.packetId}`, performance.now());
      state.socket.send(stream.packet.buffer);
      stream.packetId = (stream.packetId + 1) >>> 0;
      stream.packet = new DataView(new ArrayBuffer(9 + stream.samples * 2));
      stream.position = 0;
    };
    for (const block of stream.blocks) for (const sample of block) {
      const value = Math.max(-1, Math.min(1, sample));
      stream.packet.setInt16(9 + stream.position * 2, Math.min(32767, Math.round(value * 32768)), true);
      if (++stream.position === stream.samples) sendPacket();
    }
    stream.blocks.length = 0;
    if (stream.ended) {
      if (stream.position) sendPacket(); // zero-pad only the final partial packet
      finishMic(state, stream);
    }
  }

  function micAudio(state, block) {
    let stream = state.micStreams.at(-1);
    if (!stream || stream.ended) {
      const codec = state.panel.echo && useOpus ? "opus" : "pcm_s16le";
      const frameMs = codec === "opus" ? 20 : 16;
      const samples = state.ctx.sampleRate * frameMs / 1000;
      const header = new DataView(new ArrayBuffer(4));
      header.setUint16(0, state.ctx.sampleRate, true);
      header.setUint8(2, 1); header.setUint8(3, frameMs);
      stream = { seq: state.nextStreamSeq++, blocks: [], position: 0, packetId: 0, codec, frameMs, samples,
        header: btoa(String.fromCharCode(...new Uint8Array(header.buffer))),
        packet: new DataView(new ArrayBuffer(9 + samples * 2)) };
      state.micStreams.push(stream);
    }
    stream.blocks.push(block);
    sendMic(state);
  }

  function receive(state, data) {
    if (typeof data === "string") {
      const event = JSON.parse(data);
      if (event.seq === state.micStreams[0]?.seq && !event.command) {
        if (!event.success || !Number.isInteger(event.stream_id)) throw I18n.error("Audio stream could not start.");
        state.micStreams[0].id = event.stream_id;
        sendMic(state);
      } else if (event.command === "on_stream_start") {
        if (!state.panel.echo) resultMessage(state, event, undefined, data);
        const header = Uint8Array.from(atob(event.codec_header), c => c.charCodeAt(0));
        const rate = new DataView(header.buffer).getUint16(0, true);
        const samples = rate * event.packet_duration / 1000;
        if (!["pcm_s16le", "opus"].includes(event.codec) || header.length !== 4 || !rate ||
            header[2] * header[3] !== event.packet_duration || !Number.isInteger(samples)) {
          throw I18n.error("Unexpected audio response.");
        }
        const stream = { id: event.stream_id, rate, samples, codec: event.codec,
          timestamp: 0, ignoreBefore: 0, ready: state.decodeTail };
        if (event.codec === "opus") {
          stream.decoder = new AudioDecoder({ output: audio => {
            try {
              if (current !== state || audio.timestamp < stream.ignoreBefore) return;
              const buffer = state.ctx.createBuffer(1, audio.numberOfFrames, audio.sampleRate);
              audio.copyTo(buffer.getChannelData(0), { planeIndex: 0, format: "f32-planar" });
              playBuffer(state, buffer);
            } catch (error) { fail(state, error); }
            finally { audio.close(); }
          }, error: error => fail(state, error) });
          state.decoders.add(stream);
          stream.decoder.configure({ codec: "opus", sampleRate: rate, numberOfChannels: 1 });
        }
        state.receiving = stream;
      } else if (event.command === "on_stream_stop") {
        if (!state.panel.echo) resultMessage(state, event, undefined, data);
        const stream = state.receiving;
        if (stream?.decoder) state.decodeTail = stream.ready.then(() => {
          if (current === state) return stream.decoder.flush();
        }).then(() => {
          if (stream.decoder.state !== "closed") stream.decoder.close();
          state.decoders.delete(stream);
        }).catch(error => fail(state, error));
        state.receiving = null;
      } else if (["on_transcription", "on_speech_error"].includes(event.command)) resultMessage(state, event, undefined, data);
      return;
    }
    const packet = new DataView(data);
    if (!state.receiving || packet.byteLength <= 9 ||
        (state.receiving.codec === "pcm_s16le" && packet.byteLength !== 9 + state.receiving.samples * 2) || packet.getUint8(0) !== 1 ||
        packet.getUint32(1) !== state.receiving.id) throw I18n.error("Unexpected audio response.");
    // This echo route returns exactly one numbered packet per send, in order.
    const id = packet.getUint32(5);
    const key = `${packet.getUint32(1)}:${id}`;
    const sent = state.sentAt.get(key);
    if (sent !== undefined) {
      state.sentAt.delete(key);
      state.rttSum += performance.now() - sent;
      state.rttCount++;
    }
    if (state.receiving.codec === "opus") {
      const stream = state.receiving;
      const timestamp = stream.timestamp;
      stream.timestamp += stream.samples / stream.rate * 1000000;
      stream.ready = stream.ready.then(() => {
        if (current !== state) return;
        stream.decoder.decode(new EncodedAudioChunk({ type: "key", timestamp, data: new Uint8Array(data, 9) }));
      }).catch(error => fail(state, error));
      return;
    }
    if (state.panel.echo && Number(delay.value) === 5.5) return; // still transmit/receive; only local playback is disabled
    const buffer = state.ctx.createBuffer(1, state.receiving.samples, state.receiving.rate);
    const samples = buffer.getChannelData(0);
    for (let i = 0; i < samples.length; i++) samples[i] = packet.getInt16(9 + i * 2, true) / 32768;
    if (state.decoders.size) {
      const epoch = state.playbackEpoch;
      state.decodeTail.then(() => {
        if (current === state && epoch === state.playbackEpoch) playBuffer(state, buffer);
      }).catch(error => fail(state, error));
    } else playBuffer(state, buffer);
  }

  function playBuffer(state, buffer) {
    if (state.panel.echo && Number(delay.value) === 5.5) return;
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
      state = { panel, ctx, socket, abort: new AbortController(), uploads: [], rows: new Map(), streamResults: new Map(), posting: false, channel: url.searchParams.get("channel"), playing: new Set(), next: 0,
        playbackEpoch: 0,
        decoders: new Set(), decodeTail: Promise.resolve(), micStreams: [], nextStreamSeq: 1, sentAt: new Map(),
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
      await Promise.all([resumed, new Promise((resolve, reject) => {
        socket.addEventListener("open", resolve);
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
      state.splitter = SegmentSplitter.create(ctx.sampleRate,
        { ...SegmentSplitter.defaults, ...splitSettings }, {
          onAudio: block => micAudio(state, block),
          onPause: () => { state.micStreams.at(-1).ended = true; sendMic(state); },
          onSegment: panel.echo ? undefined : samples => {
            state.uploads.push({ samples }); uploadNext(state);
          },
        });
      state.node.port.onmessage = ({ data }) => {
        if (current !== state) return;
        try {
          let sum = 0, peak = 0;
          for (const sample of data) {
            sum += sample * sample;
            peak = Math.max(peak, Math.abs(sample));
          }
          const rms = Math.sqrt(sum / data.length);
          SegmentSplitter.push(state.splitter, data, rms);
          state.meter.rms = Math.max(state.meter.rms, rms);
          state.meter.peak = Math.max(state.meter.peak, peak);
        } catch (error) { fail(state, error); }
      };
      state.source.connect(state.node);
      state.node.connect(ctx.destination); // the recorder outputs silence
      meterEl.classList.add("active");
      state.rttAt = performance.now();
      state.meter.frame = requestAnimationFrame(now => drawMeter(state, now));
      state.connected = true;
      setStatus(`${prefix}-status`, panel.echo
        ? I18n.msg("Connected: {codec}", { codec: useOpus ? "opus" : "PCM" }) : I18n.msg("Connected"));
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
