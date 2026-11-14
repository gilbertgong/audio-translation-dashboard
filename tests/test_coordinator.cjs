const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const Clock = require("./helpers/fake_clock.cjs");

function fixture() {
  const clock = new Clock(), events = {}, requests = [];
  const sandbox = vm.createContext({
    AbortController, TypeError,
    AbortSignal: { any: AbortSignal.any, timeout(ms) {
      const controller = new AbortController();
      clock.setTimeout(() => controller.abort(new DOMException("timeout", "TimeoutError")), ms);
      return controller.signal;
    } },
    I18n: { error(key, values) { return Object.assign(new Error(key), { uiMessage: { key, values } }); } },
    window: { addEventListener(name, fn) { events[name] = fn; } },
    setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout,
    fetch(url, options) {
      return new Promise((resolve, reject) => {
        requests.push({ url, options, resolve, reject });
        options.signal.addEventListener("abort", () => reject(options.signal.reason), { once: true });
      });
    },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../src/static/coordinator.js"), "utf8"), sandbox);
  const coordinator = vm.runInContext("Coordinator", sandbox);
  const answer = (status = 200, body = { services: [{ role: "stt", state: "ok" }] }, extra = {}) => requests.at(-1).resolve({
    ok: status === 200, status, json: async () => body, ...extra,
  });
  return { clock, events, requests, coordinator, answer };
}

(async () => {
  const f = fixture(), controller = new AbortController();
  assert.equal(f.requests[0].url, "/api/health");
  let released = 0;
  const waiting = f.coordinator.wait(controller.signal).then(() => released++);
  f.requests[0].reject(new TypeError("network")); await f.clock.flush();
  assert.equal(f.coordinator.state, "down"); assert.equal(released, 0);
  await f.clock.advance(2000);
  assert.equal(f.requests.length, 2);
  f.answer(); await waiting;
  assert.equal(f.coordinator.state, "up"); assert.equal(released, 1);
  await f.clock.advance(9999); assert.equal(f.requests.length, 2);
  await f.clock.advance(1); assert.equal(f.requests.length, 3);
  f.answer(503); await f.clock.flush(); assert.equal(f.coordinator.state, "down");
  const cancel = new AbortController();
  const cancelled = assert.rejects(f.coordinator.wait(cancel.signal), { name: "AbortError" });
  cancel.abort(); await cancelled;
  // Individual cancellation doesn't stop the independent probe or other waiting work.
  const survivor = f.coordinator.wait(controller.signal);
  await f.clock.advance(2000); f.answer(); await survivor;
  const probe = f.coordinator.check();
  assert.equal(f.coordinator.check(), probe); // coalesced, not a probe per waiting segment
  const count = f.requests.length;
  await f.clock.advance(5000); await probe;
  assert.equal(f.coordinator.state, "down");
  assert.equal(f.requests.length, count);
  const exitWait = assert.rejects(f.coordinator.wait(controller.signal), { name: "AbortError" });
  f.events.pagehide(); await exitWait;
  await f.clock.advance(20000); assert.equal(f.requests.length, count);

  for (const status of [400, 401, 403, 404, 500]) {
    const f = fixture();
    const waiting = assert.rejects(f.coordinator.wait(new AbortController().signal), (err) => err.uiMessage.values.status === status);
    f.answer(status); await waiting;
    assert.equal(f.coordinator.state, "blocked");
  }
  for (const extra of [{ redirected: true }, { json: async () => ({ login: true }) }, { json: async () => null },
    { json: async () => { throw new SyntaxError("HTML instead of JSON"); } }]) {
    const f = fixture();
    const waiting = assert.rejects(f.coordinator.wait(new AbortController().signal));
    f.answer(200, { ok: true }, extra); await waiting;
    assert.equal(f.coordinator.state, "blocked");
  }
  { const f = fixture(); let health;
    f.coordinator.subscribe((snapshot) => { health = snapshot.services; });
    const waiting = f.coordinator.wait(new AbortController().signal);
    const engines = ["stt", "llm", "tts"].map((role) => ({ role, state: "down" }));
    f.answer(200, { services: engines }); await waiting; await f.clock.flush();
    assert.equal(f.coordinator.state, "up"); assert.deepEqual(health, engines);
    await f.clock.advance(1999); assert.equal(f.requests.length, 1);
    await f.clock.advance(1); assert.equal(f.requests.length, 2);
    f.events.pagehide(); }
  console.log("Coordinator checks passed: shared health polling, engines down with Coordinator up, outage/recovery, access errors, timeout and cancellation.");
})().catch((err) => { console.error(err); process.exitCode = 1; });
