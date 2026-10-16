// Each entry is translated independently; conversation history is display-only.
const $ = (id) => document.getElementById(id);
let languages = [];
let sourceIndex = 0;
let translating = null;
let speaking = null;

// Share engine capacity with live capture, while each view keeps its own UI state.
function engineQueue(name, signal, task) {
  const queue = window.parent.translationQueues?.[name] || ((run) => run());
  return queue(() => { signal.throwIfAborted(); return task(); });
}

function status(message, error = false) {
  $("translation-status").textContent = message;
  $("translation-status").classList.toggle("error", error);
}

function updateControls() {
  const ready = languages.length === 2;
  const busy = !!translating;
  $("source-text").disabled = !ready;
  $("swap").disabled = !ready || busy;
  $("clear").disabled = busy || !$("messages").children.length;
  $("translate").disabled = !ready || (!busy && !$("source-text").value.trim());
  $("translate").textContent = busy ? "Stop" : "Translate";
  document.querySelectorAll(".retry").forEach((btn) => { btn.disabled = busy; });
}

function resizeComposer() {
  $("source-text").style.height = "auto";
  $("source-text").style.height = Math.min($("source-text").scrollHeight, 180) + "px";
  updateControls();
}

function stopSpeech() {
  if (!speaking) return;
  speaking.controller.abort();
  speaking.ctx.close();
  setPlayButton(speaking.message.listen, false);
  speaking = null;
}

function updateDirection() {
  $("source-language").textContent = languages[sourceIndex];
  $("target-language").textContent = languages[1 - sourceIndex];
  updateControls();
}

$("source-text").addEventListener("input", resizeComposer);
$("swap").addEventListener("click", () => {
  sourceIndex = 1 - sourceIndex;
  updateDirection();
  $("source-text").focus();
});
$("clear").addEventListener("click", () => {
  stopSpeech();
  $("messages").replaceChildren();
  $("empty-conversation").hidden = false;
  status("");
  updateControls();
  $("source-text").focus();
});

function element(tag, className, text = "") {
  const node = document.createElement(tag);
  node.className = className;
  node.textContent = text;
  return node;
}

function scrollToLatest() {
  $("conversation").scrollTop = $("conversation").scrollHeight;
}

function updateMessage(message, text) {
  const panel = $("conversation");
  const follow = panel.scrollHeight - panel.clientHeight - panel.scrollTop < 80;
  message.text.textContent = text;
  if (follow) scrollToLatest();
}

function action(parent, label, callback) {
  const button = element("button", "", label);
  button.type = "button";
  button.title = label;
  button.setAttribute("aria-label", label);
  button.addEventListener("click", callback);
  parent.append(button);
  return button;
}

function addResultActions(message) {
  const content = element("span", "translation-text", message.text.textContent);
  const { container, play } = TranslationActions.create(content.textContent, content,
    () => listen(message), (text) => status(text));
  message.listen = play;
  message.text.replaceChildren(content, container);
  setPlayButton(message.listen, false);
}

function setPlayButton(button, playing) {
  button.textContent = playing ? "■" : "▶";
  button.title = playing ? "Stop speaking" : "Speak this translation";
  button.setAttribute("aria-label", button.title);
}

async function responseError(res) {
  try { return (await res.json()).error || `Request failed (${res.status})`; }
  catch { return `Request failed (${res.status})`; }
}

// Consume complete SSE events even when UTF-8 text or event boundaries span reads.
async function readTranslation(res, onText) {
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let text = "";
  let finished = false;
  const event = (block) => {
    const lines = block.split("\n");
    const name = lines.find((line) => line.startsWith("event:"))?.slice(6).trim();
    const data = lines.filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trimStart()).join("\n");
    if (!data) return;
    if (name === "error") throw new Error(JSON.parse(data).error);
    if (name) return; // request metadata is only used by the debug pages
    if (data === "[DONE]") { finished = true; return; }
    const chunk = JSON.parse(data);
    if (chunk.error) throw new Error(chunk.error.message || "Translation failed");
    text += chunk.choices?.[0]?.delta?.content || "";
    onText(text);
  };
  try {
    for (;;) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      buffer = buffer.replace(/\r\n/g, "\n");
      let end;
      while ((end = buffer.indexOf("\n\n")) >= 0) {
        event(buffer.slice(0, end));
        buffer = buffer.slice(end + 2);
      }
      if (done) break;
    }
    if (buffer.trim()) event(buffer);
    if (!finished) throw new Error("Translation was interrupted. Please try again.");
    return text.trim();
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

async function runTranslation(message) {
  if (translating) return;
  stopSpeech();
  const controller = new AbortController();
  translating = controller;
  message.actions.replaceChildren();
  message.row.setAttribute("aria-busy", "true");
  updateMessage(message, "Translating…");
  status("Translating…");
  updateControls();
  try {
    const result = await engineQueue("llama", controller.signal, async () => {
      const res = await fetch("/api/translate", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: message.original, source: message.source }), signal: controller.signal,
      });
      if (!res.ok) throw new Error(await responseError(res));
      return readTranslation(res, (text) => updateMessage(message, text));
    });
    if (!result) throw new Error("No translation returned. Please try again.");
    updateMessage(message, result);
    message.originalTranslation = result;
    addResultActions(message);
    status("Translation ready.");
  } catch (err) {
    const cancelled = controller.signal.aborted;
    if (message.text.textContent === "Translating…") updateMessage(message, "");
    message.actions.append(element("span", "message-note" + (cancelled ? "" : " error"),
      cancelled ? "Translation stopped." : err.message));
    if (message.text.textContent) message.actions.append(element("span", "message-note", "Incomplete translation"));
    action(message.actions, "Retry", () => runTranslation(message)).classList.add("retry");
    status(cancelled ? "Translation stopped." : err.message, !cancelled);
  } finally {
    translating = null;
    message.row.setAttribute("aria-busy", "false");
    updateControls();
  }
}

function sendMessage() {
  if (translating) return;
  const text = $("source-text").value.trim();
  if (!text || languages.length !== 2) return;
  const targetIndex = sourceIndex === 0 ? 1 : 0;
  $("empty-conversation").hidden = true;
  const row = element("article", "utterance" + (sourceIndex === 1 ? " lang2" : ""));
  const meta = element("div", "meta", `${new Date().toLocaleTimeString()} · ${languages[sourceIndex]}`);
  const original = element("div", "original", text);
  const translation = element("div", "translation");
  row.append(meta, original, translation);
  $("messages").append(row);
  const message = { row, text: translation };
  message.original = text;
  message.source = languages[sourceIndex];
  message.target = targetIndex === 0 ? "language1" : "language2";
  message.actions = element("div", "message-actions");
  message.row.append(message.actions);
  $("source-text").value = "";
  resizeComposer();
  scrollToLatest();
  runTranslation(message);
  $("source-text").focus();
}

$("translate").addEventListener("click", () => {
  if (translating) translating.abort();
  else sendMessage();
});
$("source-text").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault();
    sendMessage();
  }
});

// Play the endpoint's 16-bit PCM in memory, using the target language's voice.
async function listen(message) {
  if (speaking?.message === message) { stopSpeech(); status("Playback stopped."); return; }
  stopSpeech();
  const current = { ctx: new AudioContext(), controller: new AbortController(), message };
  speaking = current;
  setPlayButton(message.listen, true);
  status("Preparing speech…");
  try {
    await current.ctx.resume();
    const { bytes, rate } = await engineQueue("tts", current.controller.signal, async () => {
      const res = await fetch("/api/speak", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: message.originalTranslation, target: message.target }), signal: current.controller.signal,
      });
      if (!res.ok) throw new Error(await responseError(res));
      const bytes = await res.arrayBuffer();
      const rate = Number(/rate=(\d+)/.exec(res.headers.get("Content-Type") || "")?.[1]) || 24000;
      return { bytes, rate };
    });
    if (speaking !== current) return;
    if (!bytes.byteLength || bytes.byteLength % 2) throw new Error("No complete audio received. Please try again.");
    const buffer = current.ctx.createBuffer(1, bytes.byteLength / 2, rate);
    const view = new DataView(bytes);
    const samples = buffer.getChannelData(0);
    for (let i = 0; i < samples.length; i++) samples[i] = view.getInt16(i * 2, true) / 32768;
    const source = current.ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(current.ctx.destination);
    source.onended = () => { if (speaking === current) { stopSpeech(); status("Translation ready."); } };
    source.start();
    status("Playing translation…");
  } catch (err) {
    if (speaking !== current) return;
    stopSpeech();
    status(err.message, true);
  }
}

async function init() {
  try {
    const res = await fetch("/api/config");
    if (!res.ok) throw new Error(await responseError(res));
    const config = await res.json();
    languages = [config.language1, config.language2];
    if (!languages.every((name) => typeof name === "string" && name)) throw new Error("Languages are not configured.");
    updateDirection();
    if (config.mock) status("Mock mode: translations are sample results.");
    $("source-text").focus();
  } catch (err) {
    languages = [];
    updateControls();
    status(`Could not load languages. Reload to try again. ${err.message}`, true);
  }
}

window.addEventListener("pagehide", () => { translating?.abort(); stopSpeech(); });
init();
