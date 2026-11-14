// Deterministic time for network recovery tests; never sleeps or contacts a server.
class Clock {
  constructor() { this.now = 0; this.next = 0; this.timers = new Map(); }
  setTimeout = (fn, delay) => { const id = ++this.next; this.timers.set(id, { fn, due: this.now + delay }); return id; };
  clearTimeout = (id) => { this.timers.delete(id); };
  flush = () => new Promise(setImmediate);
  async advance(ms) {
    const target = this.now + ms;
    for (;;) {
      const next = [...this.timers].filter(([, timer]) => timer.due <= target).sort((a, b) => a[1].due - b[1].due)[0];
      if (!next) break;
      this.now = next[1].due;
      this.timers.delete(next[0]); next[1].fn();
      await this.flush();
    }
    this.now = target;
    await this.flush();
  }
}
module.exports = Clock;
