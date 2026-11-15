// The Live speech splitter, independent of page UI and upload handling.
const SegmentSplitter = (() => {
  const defaults = { split_pause_ms: 250, threshold_db: 12, min_threshold_db: -50,
    pre_roll_ms: 300, tail_ms: 200, min_speech_ms: 100, max_phrase_s: 25 };
  const CALIBRATION_MS = 400;
  const NOISE_FALL_MS = 50;
  const NOISE_RISE_MS = 2000;
  const toDb = x => x > 0 ? Math.max(-60, 20 * Math.log10(x)) : -60;
  function create(sampleRate, settings, { onSegment, onSpeech, onChange } = {}) {
    return {
      sampleRate,
      settings, onSegment, onSpeech, onChange,
      elapsedMs: 0,
      calibrating: true,
      noiseDb: null, // background level estimate
      thresholdDb: settings.min_threshold_db,
      speaking: false,
      preRoll: [], // recent blocks kept while waiting for speech
      preRollSamples: 0,
      blocks: [], // the current segment
      segmentSamples: 0,
      speechMs: 0, // loud time in the current segment
      silenceMs: 0, // quiet time since the last loud block
    };
  }

  // Feed one audio block (Float32Array) and its RMS level.
  function push(sp, block, rms) {
    const ms = (block.length / sp.sampleRate) * 1000;
    const db = toDb(rms);
    sp.elapsedMs += ms;

    // Track the background noise level.
    if (sp.noiseDb === null) sp.noiseDb = db;
    const tau = db < sp.noiseDb ? NOISE_FALL_MS : NOISE_RISE_MS * (sp.speaking ? 4 : 1);
    sp.noiseDb += (db - sp.noiseDb) * (1 - Math.exp(-ms / tau));
    sp.thresholdDb = Math.max(sp.noiseDb + sp.settings.threshold_db, sp.settings.min_threshold_db);

    if (sp.calibrating) {
      if (sp.elapsedMs < CALIBRATION_MS) return;
      sp.calibrating = false;
      sp.onChange?.();
    }
    const loud = db > sp.thresholdDb;
    // For automatic playback: speech now; sustained (past a cough) mid-segment.
    // A muted captured tab isn't heard here, so the translations talk over it.
    if (loud) sp.onSpeech?.(sp.speaking && sp.speechMs + ms >= sp.settings.min_speech_ms);

    if (!sp.speaking) {
      sp.preRoll.push(block);
      sp.preRollSamples += block.length;
      const keep = (sp.settings.pre_roll_ms / 1000) * sp.sampleRate;
      while (sp.preRoll.length > 1 && sp.preRollSamples - sp.preRoll[0].length >= keep) {
        sp.preRollSamples -= sp.preRoll.shift().length;
      }
      if (loud) {
        Object.assign(sp, { speaking: true, blocks: sp.preRoll, segmentSamples: sp.preRollSamples,
                            speechMs: ms, silenceMs: 0, preRoll: [], preRollSamples: 0 });
        sp.onChange?.();
      }
      return;
    }

    sp.blocks.push(block);
    sp.segmentSamples += block.length;
    if (loud) {
      sp.speechMs += ms;
      sp.silenceMs = 0;
    } else {
      sp.silenceMs += ms;
    }
    if (sp.silenceMs >= sp.settings.split_pause_ms) end(sp, "pause");
    else if (sp.segmentSamples / sp.sampleRate >= sp.settings.max_phrase_s) end(sp, "max length");
  }

  // Finish the current segment and send it (if it had enough speech).
  // reason: "pause", "max length" (speech continues into a new segment) or "stop".
  function end(sp, reason) {
    if (!sp.speaking) return;
    let samples = concat(sp.blocks);
    if (reason === "pause") {
      // Keep only tail_ms of the trailing silence.
      const extraMs = Math.max(0, sp.silenceMs - sp.settings.tail_ms);
      const extra = Math.floor((extraMs / 1000) * sp.sampleRate);
      samples = samples.subarray(0, Math.max(0, samples.length - extra));
    }
    const speechMs = sp.speechMs;
    const continuing = reason === "max length";
    Object.assign(sp, { speaking: continuing, blocks: [], segmentSamples: 0, speechMs: 0, silenceMs: 0 });
    sp.onChange?.();

    if (speechMs < sp.settings.min_speech_ms) return; // too little speech: a click or cough
    sp.onSegment?.(samples, reason);
  }

  return { defaults, create, push, end };
})();
