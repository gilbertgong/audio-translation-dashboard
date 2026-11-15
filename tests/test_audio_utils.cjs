// Shared Live/component capture primitives: node tests/test_audio_utils.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const ctx = vm.createContext({ Float32Array, DataView, ArrayBuffer, Blob });
for (const file of ['audio-utils.js', 'segment-splitter.js']) {
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../src/static/' + file), 'utf8'), ctx);
}
(async () => {
  const wave = await vm.runInContext('encodeWav(Float32Array.from([-1, 0, 1]), 16000).arrayBuffer()', ctx);
  const view = new DataView(wave);
  assert.equal(Buffer.from(wave).subarray(0, 4).toString(), 'RIFF');
  assert.equal(view.getUint32(24, true), 16000);
  assert.deepEqual([0, 1, 2].map(i => view.getInt16(44 + i * 2, true)), [-32768, 0, 32767]);
  const segments = [];
  ctx.onSegment = (samples, reason) => segments.push({ samples, reason });
  vm.runInContext(`
    const settings = {...SegmentSplitter.defaults};
    const sp = SegmentSplitter.create(1000, settings, {onSegment});
    const quiet = new Float32Array(10), loud = new Float32Array(10).fill(0.1);
    for (let i=0; i<40; i++) SegmentSplitter.push(sp, quiet, 0); // calibration
    for (let i=0; i<20; i++) SegmentSplitter.push(sp, quiet, 0); // pre-roll
    for (let i=0; i<20; i++) SegmentSplitter.push(sp, loud, 0.1);
    for (let i=0; i<25; i++) SegmentSplitter.push(sp, quiet, 0);
  `, ctx);
  assert.equal(segments.length, 1);
  assert.equal(segments[0].reason, 'pause');
  assert.equal([...segments[0].samples].filter(value => value > 0).length, 200);
  assert.ok(segments[0].samples.length > 200); // retained pre-roll and tail
  vm.runInContext(`
    for (let i=0; i<20; i++) SegmentSplitter.push(sp, quiet, 0);
    for (let i=0; i<5; i++) SegmentSplitter.push(sp, loud, 0.1);
    for (let i=0; i<25; i++) SegmentSplitter.push(sp, quiet, 0);
  `, ctx);
  assert.equal(segments.length, 1); // short noise does not create a segment
  console.log('Shared audio checks passed: PCM WAV values, calibration, speech segmentation, pre-roll/tail and short-noise rejection.');
})().catch(error => { console.error(error); process.exitCode = 1; });
