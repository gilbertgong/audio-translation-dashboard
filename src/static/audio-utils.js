// Shared mono PCM accumulation, browser resampling and WAV encoding.
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

