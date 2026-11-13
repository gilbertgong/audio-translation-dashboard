// Interface messages only. Model output, user text and raw diagnostics are never looked up.
const I18n = (() => {
  const storageKey = "uiLanguage";
  const bindings = new WeakMap();
  const catalogs = { "zh-Hans": UI_ZH_HANS, "zh-Hant": UI_ZH_HANT };
  function detect(languages) {
    const tag = (languages?.[0] || "en").toLowerCase().replaceAll("_", "-");
    if (!/^zh(?:-|$)/.test(tag)) return "en";
    if (tag.includes("-hant")) return "zh-Hant";
    if (tag.includes("-hans")) return "zh-Hans";
    return /-(tw|hk|mo)(?:-|$)/.test(tag) ? "zh-Hant" : "zh-Hans";
  }
  const detected = detect(navigator.languages?.length ? navigator.languages : [navigator.language]);
  // The visible choices stay English/Chinese; browser preference chooses the Chinese script.
  const chineseLanguage = detected === "zh-Hant" ? "zh-Hant" : "zh-Hans";
  let language = detected;
  try {
    const saved = localStorage.getItem(storageKey);
    if (saved === "en") language = "en";
    else if (Object.hasOwn(catalogs, saved)) language = chineseLanguage;
  } catch {} // The language switch also works when storage is unavailable.

  function msg(key, values = {}) { return { key, values }; }
  // Keep app-owned message identity across throws; external errors remain literal.
  function error(key, values = {}) { return Object.assign(new Error(key), { uiMessage: msg(key, values) }); }
  function errorText(err) { return err.uiMessage ?? err.message; }
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
    if (attribute === "rich") {
      const parts = text.split(/(\{\w+\})/g).map((part) =>
        value.slots[part.slice(1, -1)] && /^\{\w+\}$/.test(part)
          ? value.slots[part.slice(1, -1)] : document.createTextNode(part));
      node.replaceChildren(...parts);
    } else if (attribute === "textContent") node.textContent = text;
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
      button.lang = language === "en" ? chineseLanguage : "en";
      button.title = language === "en"
        ? (chineseLanguage === "zh-Hant" ? "切換到中文" : "切换到中文") : "Switch to English";
      button.setAttribute("aria-label", button.title);
    }
  }
  function change(next, persist = true) {
    if (next !== "en" && !Object.hasOwn(catalogs, next)) return;
    language = next;
    if (persist) { try { localStorage.setItem(storageKey, language); } catch {} }
    refresh();
    window.dispatchEvent(new Event("uilanguagechange"));
  }
  window.addEventListener("storage", (event) => {
    if (event.key !== storageKey) return;
    if (!event.newValue) change(detected, false);
    else if (event.newValue === "en") change("en", false);
    else if (Object.hasOwn(catalogs, event.newValue)) change(chineseLanguage, false);
  });
  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("[data-i18n]").forEach((node) => {
      if (!bindings.get(node)?.has("textContent")) text(node, msg(node.dataset.i18n));
    });
    for (const name of ["title", "aria-label", "placeholder"]) {
      document.querySelectorAll(`[data-i18n-${name}]`).forEach((node) => {
        if (!bindings.get(node)?.has(name)) attr(node, name, msg(node.getAttribute(`data-i18n-${name}`)));
      });
    }
    document.querySelectorAll("[data-i18n-rich]").forEach((node) => {
      const slots = Object.fromEntries([...node.querySelectorAll("[data-i18n-slot]")]
        .map((slot) => [slot.dataset.i18nSlot, slot]));
      set(node, "rich", { ...msg(node.dataset.i18nRich), slots });
    });
    refresh();
    document.getElementById("ui-language")?.addEventListener("click", () => change(language === "en" ? chineseLanguage : "en"));
  });
  return { detect, t, msg, error, errorText, text, attr, refresh, change, get language() { return language; } };
})();
// Shared by page scripts and optional header customization.
window.I18n = I18n;
