// Each submitted text segment is translated independently; conversation history is display-only.
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
  I18n.text($("translation-status"), message);
  $("translation-status").classList.toggle("error", error);
}

function updateControls() {
  const ready = languages.length === 2;
  const busy = !!translating;
  $("source-text").disabled = !ready;
  $("swap").disabled = !ready || busy;
  $("clear").disabled = busy || !$("messages").children.length;
  $("translate").disabled = !ready || (!busy && !$("source-text").value.trim());
  I18n.text($("translate"), I18n.msg(busy ? "Stop" : "Translate"));
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
  I18n.text(node, text);
  return node;
}

function scrollToLatest() {
  $("conversation").scrollTop = $("conversation").scrollHeight;
}

function updateMessage(message, text) {
  const panel = $("conversation");
  const follow = panel.scrollHeight - panel.clientHeight - panel.scrollTop < 80;
  I18n.text(message.text, text);
  if (follow) scrollToLatest();
}

function action(parent, label, callback) {
  const button = element("button", "", label);
  button.type = "button";
  I18n.attr(button, "title", label);
  I18n.attr(button, "aria-label", label);
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
  const label = I18n.msg(playing ? "Stop speaking" : "Speak this translation");
  I18n.attr(button, "title", label);
  I18n.attr(button, "aria-label", label);
}

async function responseError(res) {
  try {
    const detail = (await res.json()).error;
    return detail ? new Error(detail) : I18n.error("Request failed ({status})", { status: res.status });
  }
  catch { return I18n.error("Request failed ({status})", { status: res.status }); }
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
    if (chunk.error) throw chunk.error.message ? new Error(chunk.error.message) : I18n.error("Translation failed");
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
    if (!finished) throw I18n.error("Translation was interrupted. Please try again.");
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
  message.receivedText = false;
  updateMessage(message, I18n.msg("Translating…"));
  status(I18n.msg("Translating…"));
  updateControls();
  try {
    const result = await engineQueue("llama", controller.signal, async () => {
      const res = await fetch("/api/translate", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: message.original, source: message.source }), signal: controller.signal,
      });
      if (!res.ok) throw await responseError(res);
      return readTranslation(res, (text) => { message.receivedText = !!text; updateMessage(message, text); });
    });
    if (!result) throw I18n.error("No translation returned. Please try again.");
    updateMessage(message, result);
    message.originalTranslation = result;
    addResultActions(message);
    status(I18n.msg("Translation ready."));
  } catch (err) {
    const cancelled = controller.signal.aborted;
    if (!message.receivedText) updateMessage(message, "");
    message.actions.append(element("span", "message-note" + (cancelled ? "" : " error"),
      cancelled ? I18n.msg("Translation stopped.") : I18n.errorText(err)));
    if (message.text.textContent) message.actions.append(element("span", "message-note", I18n.msg("Incomplete translation")));
    action(message.actions, I18n.msg("Retry"), () => runTranslation(message)).classList.add("retry");
    status(cancelled ? I18n.msg("Translation stopped.") : I18n.errorText(err), !cancelled);
  } finally {
    translating = null;
    message.row.setAttribute("aria-busy", "false");
    updateControls();
  }
}

function submitTextSegment() {
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
  else submitTextSegment();
});
$("source-text").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault();
    submitTextSegment();
  }
});

// Play the endpoint's 16-bit PCM in memory, using the target language's voice.
async function listen(message) {
  if (speaking?.message === message) { stopSpeech(); status(I18n.msg("Playback stopped.")); return; }
  stopSpeech();
  const current = { ctx: new AudioContext(), controller: new AbortController(), message };
  speaking = current;
  setPlayButton(message.listen, true);
  status(I18n.msg("Preparing speech…"));
  try {
    await current.ctx.resume();
    const { bytes, rate } = await engineQueue("tts", current.controller.signal, async () => {
      const res = await fetch("/api/speak", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: message.originalTranslation, target: message.target }), signal: current.controller.signal,
      });
      if (!res.ok) throw await responseError(res);
      const bytes = await res.arrayBuffer();
      const rate = Number(/rate=(\d+)/.exec(res.headers.get("Content-Type") || "")?.[1]) || 24000;
      return { bytes, rate };
    });
    if (speaking !== current) return;
    if (!bytes.byteLength || bytes.byteLength % 2) throw I18n.error("No complete audio received. Please try again.");
    const buffer = current.ctx.createBuffer(1, bytes.byteLength / 2, rate);
    const view = new DataView(bytes);
    const samples = buffer.getChannelData(0);
    for (let i = 0; i < samples.length; i++) samples[i] = view.getInt16(i * 2, true) / 32768;
    const source = current.ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(current.ctx.destination);
    source.onended = () => { if (speaking === current) { stopSpeech(); status(I18n.msg("Translation ready.")); } };
    source.start();
    status(I18n.msg("Playing translation…"));
  } catch (err) {
    if (speaking !== current) return;
    stopSpeech();
    status(I18n.errorText(err), true);
  }
}

async function init() {
  try {
    const res = await fetch("/api/config");
    if (!res.ok) throw await responseError(res);
    const config = await res.json();
    languages = [config.language1, config.language2];
    if (!languages.every((name) => typeof name === "string" && name)) throw I18n.error("Languages are not configured.");
    updateDirection();
    if (config.mock) status(I18n.msg("Mock mode: translations are sample results."));
    $("source-text").focus();
  } catch (err) {
    languages = [];
    updateControls();
    status(I18n.msg("Could not load languages. Reload to try again. {error}", { error: I18n.errorText(err) }), true);
  }
}

window.addEventListener("pagehide", () => { translating?.abort(); stopSpeech(); });
init();
