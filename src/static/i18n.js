// Interface messages only. Model output, user text and raw diagnostics are never looked up.
const I18n = (() => {
  const storageKey = "uiLanguage";
  const bindings = new WeakMap();
  const catalogs = { "zh-Hans": UI_ZH_HANS };
  function detect(languages) {
    const tag = (languages?.[0] || "en").toLowerCase().replaceAll("_", "-");
    if (!/^zh(?:-|$)/.test(tag)) return "en";
    if (tag.includes("-hant")) return "zh-Hant";
    if (tag.includes("-hans")) return "zh-Hans";
    return /-(tw|hk|mo)(?:-|$)/.test(tag) ? "zh-Hant" : "zh-Hans";
  }
  // Traditional detection is retained separately; until its catalog exists, use Simplified.
  function supported(language) { return language === "zh-Hant" ? "zh-Hans" : language; }
  let language = supported(detect(navigator.languages?.length ? navigator.languages : [navigator.language]));
  try {
    const saved = localStorage.getItem(storageKey);
    if (saved === "en" || Object.hasOwn(catalogs, saved)) language = saved;
  } catch {} // The language switch also works when storage is unavailable.

  function msg(key, values = {}) { return { key, values }; }
  function isMessage(value) { return value !== null && typeof value === "object" && typeof value.key === "string"; }
  function t(key, values = {}) {
    const catalog = catalogs[language];
    const template = catalog && Object.hasOwn(catalog, key) ? catalog[key] : key;
    return template.replace(/\{(\w+)\}/g, (match, name) => {
      const value = values[name];
      return value === undefined ? match : String(isMessage(value) ? t(value.key, value.values) : value);
    });
  }
  function render(node, attribute, value) {
    const text = isMessage(value) ? t(value.key, value.values) : String(value ?? "");
    if (attribute === "textContent") node.textContent = text;
    else node.setAttribute(attribute, text);
  }
  function set(node, attribute, value) {
    let entries = bindings.get(node);
    if (!entries) { entries = new Map(); bindings.set(node, entries); }
    // Remember literal writes too, so startup markers cannot overwrite an early response.
    entries.set(attribute, value);
    if ([...entries.values()].some(isMessage)) node.setAttribute("data-ui-message", "");
    else node.removeAttribute("data-ui-message");
    render(node, attribute, value);
    return node;
  }
  function text(node, value) { return set(node, "textContent", value); }
  function attr(node, name, value) { return set(node, name, value); }
  function refresh() {
    document.documentElement.lang = language;
    document.querySelectorAll("[data-ui-message]").forEach((node) => {
      for (const [attribute, value] of bindings.get(node) || []) {
        if (isMessage(value)) render(node, attribute, value);
      }
    });
    const button = document.getElementById("ui-language");
    if (button) {
      button.textContent = language === "en" ? "中文" : "EN";
      button.lang = language === "en" ? "zh-Hans" : "en";
      button.title = language === "en" ? "切换到简体中文" : "Switch to English";
      button.setAttribute("aria-label", button.title);
    }
  }
  function change(next, persist = true) {
    if (next !== "en" && !Object.hasOwn(catalogs, next)) return;
    language = next;
    if (persist) { try { localStorage.setItem(storageKey, language); } catch {} }
    refresh();
    // Same-origin mounted views keep their work; no reload or network request is needed.
    document.querySelectorAll("iframe.app-view-frame").forEach((frame) => {
      frame.contentWindow?.I18n?.change(language, false);
    });
    window.dispatchEvent(new Event("uilanguagechange"));
  }
  window.addEventListener("storage", (event) => {
    if (event.key === storageKey) change(event.newValue || supported(detect(navigator.languages)), false);
  });
  document.addEventListener("DOMContentLoaded", () => {
    if (window !== window.parent && window.parent.I18n) language = window.parent.I18n.language;
    document.querySelectorAll("[data-i18n]").forEach((node) => {
      if (!bindings.get(node)?.has("textContent")) text(node, msg(node.dataset.i18n));
    });
    for (const name of ["title", "aria-label", "placeholder"]) {
      document.querySelectorAll(`[data-i18n-${name}]`).forEach((node) => {
        if (!bindings.get(node)?.has(name)) attr(node, name, msg(node.getAttribute(`data-i18n-${name}`)));
      });
    }
    refresh();
    document.getElementById("ui-language")?.addEventListener("click", () => change(language === "en" ? "zh-Hans" : "en"));
  });
  return { detect, t, msg, text, attr, refresh, change, get language() { return language; } };
})();
// Expose only the localization API to the application's same-origin child views.
window.I18n = I18n;
