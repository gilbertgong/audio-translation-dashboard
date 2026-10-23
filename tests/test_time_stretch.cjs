// Offline signal checks: node tests/test_time_stretch.cjs
const assert = require("node:assert/strict");
const { SpeechTimeStretch } = require("../src/static/time-stretch.js");
const rate = 24000;
const input = Float32Array.from({ length: rate * 2 }, (_, i) =>
  0.5 * Math.sin(2 * Math.PI * 220 * i / rate));

function render(samples, speed, chunkSize) {
  const processor = new SpeechTimeStretch(rate, speed);
  const output = [];
  for (let at = 0; at < samples.length; at += chunkSize) {
    processor.append(samples.subarray(at, at + chunkSize));
    output.push(processor.read(false));
  }
  output.push(processor.read(true));
  assert.equal(processor.read(true).length, 0); // EOF cannot repeat the tail.
  const result = new Float32Array(output.reduce((sum, block) => sum + block.length, 0));
  let offset = 0;
  for (const block of output) { result.set(block, offset); offset += block.length; }
  return result;
}

for (const speed of [0.75, 0.9, 1.1, 1.25, 1.5]) {
  const output = render(input, speed, 733);
  assert.equal(output.length, Math.round(input.length / speed));
  assert.deepEqual(output, render(input, speed, input.length)); // network boundaries have no effect
  let crossings = 0, square = 0;
  const low = Math.round(rate * 0.1), high = output.length - low;
  for (let i = low + 1; i < high; i++) {
    assert.ok(Number.isFinite(output[i]) && Math.abs(output[i]) <= 0.501);
    if (output[i - 1] <= 0 && output[i] > 0) crossings++;
    square += output[i] ** 2;
  }
  const hz = crossings * rate / (high - low);
  assert.ok(Math.abs(hz - 220) < 3, `${speed}×: pitch ${hz} Hz, expected ~220`);
  assert.ok(Math.sqrt(square / (high - low)) > 0.3, "matching should not cancel the waveform");
}
for (const length of [0, 1, 239, 480, 1001]) {
  for (const speed of [0.75, 1.5]) {
    const output = render(new Float32Array(length), speed, 37);
    assert.equal(output.length, Math.round(length / speed));
    assert.ok(output.every(value => value === 0));
  }
}
console.log("Time-stretch checks passed: pitch, duration, streaming boundaries, silence, short input and EOF.");
