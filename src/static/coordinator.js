// One health poll supplies engine status and Coordinator reachability, outside service queues.
const Coordinator = (() => {
  const lifetime = new AbortController();
  const listeners = new Set();
  let state = "unknown";
  let error = null;
  let services = null;
  let timer;
  let checking = null;
  function publish(next, failure = null, health = null) {
    state = next;
    error = failure;
    services = health;
    for (const listener of listeners) listener({ state, error, services });
  }
  function subscribe(listener) {
    listeners.add(listener);
    listener({ state, error, services });
    return () => listeners.delete(listener);
  }
  function check() {
    if (checking || lifetime.signal.aborted) return checking;
    clearTimeout(timer);
    checking = (async () => {
      try {
        const response = await fetch("/api/health", {
          cache: "no-store",
          signal: AbortSignal.any([lifetime.signal, AbortSignal.timeout(5000)]),
        });
        if (lifetime.signal.aborted) return;
        if (response.redirected) throw I18n.error("Coordinator access requires attention.");
        if (!response.ok) {
          if ([502, 503, 504].includes(response.status)) { publish("down"); return; }
          throw I18n.error("Coordinator request failed ({status})", { status: response.status });
        }
        const body = await response.json();
        if (!Array.isArray(body?.services)) throw I18n.error("Unexpected Coordinator response.");
        // A successful health reply proves app reachability even if every engine is down.
        if (!lifetime.signal.aborted) publish("up", null, body.services);
      } catch (err) {
        if (lifetime.signal.aborted) return;
        // Access errors or unexpected replies must fail waiting jobs, not masquerade as an outage.
        if (err instanceof TypeError || err.name === "TimeoutError") publish("down");
        else publish("blocked", err);
      }
    })().finally(() => {
      checking = null;
      if (!lifetime.signal.aborted) timer = setTimeout(check,
        state === "up" && services.every((service) => service.state === "ok") ? 10000 : 2000);
    });
    return checking;
  }
  async function wait(signal) {
    const combined = AbortSignal.any([lifetime.signal, signal]);
    combined.throwIfAborted();
    if (state === "up") return;
    if (state === "blocked") throw error;
    await new Promise((resolve, reject) => {
      const cleanup = () => { listeners.delete(changed); combined.removeEventListener("abort", aborted); };
      const aborted = () => { cleanup(); reject(combined.reason); };
      const changed = () => {
        if (state === "up") { cleanup(); resolve(); }
        else if (state === "blocked") { cleanup(); reject(error); }
      };
      listeners.add(changed);
      combined.addEventListener("abort", aborted, { once: true });
      if (combined.aborted) aborted();
      else changed();
    });
  }
  window.addEventListener("pagehide", () => { lifetime.abort(); clearTimeout(timer); listeners.clear(); });
  check();
  return { check, wait, subscribe, get state() { return state; } };
})();
