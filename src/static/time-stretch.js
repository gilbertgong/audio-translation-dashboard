// Small mono WSOLA prototype. Keep one processor across network chunks so
// waveform matching and overlap tails never reset at transport boundaries.
// The output plays at its original sample rate; speed changes its length only.
class SpeechTimeStretch {
  constructor(rate, speed) {
    this.speed = speed;
    this.hop = Math.max(1, Math.round(rate * 0.020));
    this.search = Math.max(1, Math.round(rate * 0.010));
    this.input = new Float32Array(0);
    this.length = 0;
    this.output = 0;
    this.tail = null;
    this.finished = false;
  }

  append(samples) {
    if (this.length + samples.length > this.input.length) {
      const grown = new Float32Array(Math.max(this.length + samples.length, this.input.length * 2, 4096));
      grown.set(this.input);
      this.input = grown;
    }
    this.input.set(samples, this.length);
    this.length += samples.length;
  }

  sample(index) {
    return index >= 0 && index < this.length ? this.input[index] : 0;
  }

  match(expected) {
    const low = Math.max(0, expected - this.search);
    const high = Math.min(this.length - 1, expected + this.search);
    let best = expected;
    let score = -Infinity;
    let energy = 0;
    for (let i = 0; i < this.hop; i += 4) energy += this.tail[i] ** 2;
    if (energy < 1e-10) return expected; // Silence needs no phase matching.
    const correlation = (at) => {
      let dot = 0, power = 0;
      for (let i = 0; i < this.hop; i += 4) {
        const value = this.sample(at + i);
        dot += this.tail[i] * value;
        power += value * value;
      }
      // Prefer the nominal position when correlations are otherwise alike.
      return dot / Math.sqrt(energy * power + 1e-20) - 0.001 * Math.abs(at - expected) / this.search;
    };
    for (let at = low; at <= high; at += 4) {
      const value = correlation(at);
      if (value > score) { score = value; best = at; }
    }
    const coarse = best;
    for (let at = Math.max(low, coarse - 3); at <= Math.min(high, coarse + 3); at++) {
      const value = correlation(at);
      if (value > score) { score = value; best = at; }
    }
    return best;
  }

  read(done = false) {
    if (this.finished) return new Float32Array(0);
    const blocks = [];
    let count = 0;
    const total = Math.round(this.length / this.speed);
    while (this.output < total) {
      const expected = Math.round(this.output * this.speed);
      // Wait for the whole search window and frame, regardless of chunk size.
      if (!done && expected + this.search + 2 * this.hop > this.length) break;
      const at = this.tail ? this.match(expected) : 0;
      const length = Math.min(this.hop, total - this.output);
      const block = new Float32Array(length);
      for (let i = 0; i < length; i++) {
        const weight = (i + 0.5) / this.hop;
        block[i] = this.tail ? this.tail[i] * (1 - weight) + this.sample(at + i) * weight : this.sample(i);
      }
      const tail = new Float32Array(this.hop);
      for (let i = 0; i < this.hop; i++) tail[i] = this.sample(at + this.hop + i);
      this.tail = tail;
      blocks.push(block);
      count += length;
      this.output += length;
    }
    if (done) this.finished = true;
    const result = new Float32Array(count);
    let offset = 0;
    for (const block of blocks) { result.set(block, offset); offset += block.length; }
    return result;
  }
}

if (typeof module !== "undefined") module.exports = { SpeechTimeStretch };
