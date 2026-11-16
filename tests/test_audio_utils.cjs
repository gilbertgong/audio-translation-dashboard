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
    for (let i=0; i<60; i++) SegmentSplitter.push(sp, quiet, 0);
  `, ctx);
  assert.equal(segments.length, 1);
  assert.equal(segments[0].reason, 'pause');
  assert.equal([...segments[0].samples].filter(value => value > 0).length, 200);
  assert.ok(segments[0].samples.length > 200); // retained pre-roll and tail
  vm.runInContext(`
    for (let i=0; i<20; i++) SegmentSplitter.push(sp, quiet, 0);
    for (let i=0; i<5; i++) SegmentSplitter.push(sp, loud, 0.1);
    for (let i=0; i<60; i++) SegmentSplitter.push(sp, quiet, 0);
  `, ctx);
  assert.equal(segments.length, 1); // short noise does not create a segment
  vm.runInContext(`
    globalThis.splitAfter = (speechMs, quietMs, overrides = {}) => {
      const results = [];
      const settings = {...SegmentSplitter.defaults, pre_roll_ms: 0, threshold_db: 0, ...overrides};
      const splitter = SegmentSplitter.create(1000, settings, {
        onSegment: (samples, reason) => results.push({length: samples.length, reason}),
      });
      const silence = new Float32Array(1), voice = new Float32Array([0.1]);
      for (let i=0; i<400; i++) SegmentSplitter.push(splitter, silence, 0);
      for (let i=0; i<speechMs; i++) SegmentSplitter.push(splitter, voice, 0.1);
      for (let i=0; i<quietMs; i++) SegmentSplitter.push(splitter, silence, 0);
      return results;
    };
  `, ctx);
  for (const [speechMs, pauseMs] of [[1000, 600], [15000, 300], [22500, 150], [30000, 50]]) {
    assert.equal(vm.runInContext(`splitAfter(${speechMs}, ${pauseMs - 1}).length`, ctx), 0);
    assert.equal(vm.runInContext(`splitAfter(${speechMs}, ${pauseMs})[0].reason`, ctx), 'pause');
  }
  // An already-running pause becomes sufficient exactly at each duration boundary.
  for (const [speechMs, boundaryMs] of [[14700, 15000], [22350, 22500], [29950, 30000]]) {
    assert.equal(vm.runInContext(`splitAfter(${speechMs}, ${boundaryMs - speechMs - 1}).length`, ctx), 0);
    assert.equal(vm.runInContext(`splitAfter(${speechMs}, ${boundaryMs - speechMs})[0].reason`, ctx), 'pause');
  }
  assert.equal(vm.runInContext('splitAfter(34999, 0).length', ctx), 0);
  assert.equal(vm.runInContext('splitAfter(35000, 0)[0].reason', ctx), 'max length');
  assert.equal(vm.runInContext('splitAfter(35000, 0)[0].length', ctx), 35000);
  // Custom duration/pause settings scale the tiers; grace settings are independent.
  for (const [speechMs, pauseMs] of [[1000, 800], [10000, 400], [15000, 200], [20000, 80]]) {
    const overrides = '{max_phrase_s: 20, split_pause_ms: 800, grace_period_s: 2, grace_pause_ms: 80}';
    assert.equal(vm.runInContext(`splitAfter(${speechMs}, ${pauseMs - 1}, ${overrides}).length`, ctx), 0);
    assert.equal(vm.runInContext(`splitAfter(${speechMs}, ${pauseMs}, ${overrides})[0].reason`, ctx), 'pause');
  }
  assert.equal(vm.runInContext('splitAfter(21999, 0, {max_phrase_s: 20, grace_period_s: 2}).length', ctx), 0);
  assert.equal(vm.runInContext('splitAfter(22000, 0, {max_phrase_s: 20, grace_period_s: 2})[0].reason', ctx), 'max length');
  console.log('Shared audio checks passed: PCM WAV values, calibration, speech segmentation, pre-roll/tail and short-noise rejection.');
})().catch(error => { console.error(error); process.exitCode = 1; });
