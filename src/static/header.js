// Shared browser-side header for the three independent application pages.
(() => {
  const host = document.getElementById("app-header");
  const page = document.body.dataset.page;
  // Preserve links bookmarked when the app used hash navigation.
  const legacyPage = page === "live" && { "#text": "translate.html", "#tests": "test.html" }[window.location.hash];
  if (legacyPage) { window.location.replace(legacyPage); return; }
  const pages = [
    { name: "live", title: "Live translation", href: "/" },
    { name: "text", title: "Text translation", href: "translate.html" },
    { name: "tests", title: "Component tests", href: "test.html" },
  ];
  function node(tag, className, message) {
    const element = document.createElement(tag);
    element.className = className;
    if (message !== undefined) I18n.text(element, I18n.msg(message));
    return element;
  }
  function branding(container) {
    const title = node("h1", "");
    title.append(node("span", "", "Audio Translation Dashboard"));
    container.append(title);
  }
  function controls(container) {
    const button = node("button", "ui-language header-link");
    button.id = "ui-language";
    button.type = "button";
    button.translate = false;
    container.append(button);
  }
  function statusBar(container) {
    const menu = node("details", "page-menu");
    const summary = node("summary", "header-link");
    summary.append(node("span", "", pages.find((item) => item.name === page).title));
    const navigation = node("nav", "page-options");
    I18n.attr(navigation, "aria-label", I18n.msg("Translation views"));
    for (const item of pages) {
      const link = node("a", "page-option", item.title);
      link.href = item.href;
      if (item.name === page) link.setAttribute("aria-current", "page");
      navigation.append(link);
    }
    menu.append(summary, navigation);
    const services = node("div", "services");
    services.id = "services";
    services.setAttribute("aria-live", "polite");
    container.append(menu, services);
    if (page === "tests") {
      const smoke = node("a", "header-link", "Smoke test ↗");
      smoke.href = "smoke.html";
      smoke.target = "_blank";
      smoke.rel = "noopener";
      container.append(smoke);
    }
    document.addEventListener("click", (event) => {
      if (!menu.contains(event.target)) menu.open = false;
    });
    menu.addEventListener("keydown", (event) => {
      if (event.key !== "Escape" || !menu.open) return;
      menu.open = false;
      summary.focus();
      event.stopPropagation();
    });
  }
  const defaults = { branding, controls, statusBar };
  const context = { page, defaults };
  const heading = node("div", "app-heading");
  const brand = node("div", "header-branding");
  brand.id = "header-branding";
  const buttons = node("div", "header-controls");
  buttons.id = "header-controls";
  const status = node("div", "header-status-bar");
  status.id = "header-status-bar";
  heading.append(brand, buttons);
  host.append(heading, status);
  for (const [name, container] of [["branding", brand], ["controls", buttons], ["statusBar", status]]) {
    (window.HeaderCustom?.[name] || defaults[name])(container, context);
  }

  // The shared health poll supplies both reachability and engine states.
  // Custom status bars may omit #services without affecting fetch retries.
  const services = host.querySelector("#services");
  // Restore a fresh page rather than reviving closed audio resources from the back/forward cache.
  window.addEventListener("pageshow", (event) => { if (event.persisted) window.location.reload(); });
  if (!services) return;
  const labels = { stt: "Transcribe", llm: "Translate", tts: "Speak" };
  const coordinator = node("span", "service", "Coordinator");
  let engines = Object.values(labels).map((label) => node("span", "service", label));
  services.append(coordinator, ...engines);
  Coordinator.subscribe(({ state, error, services: health }) => {
    coordinator.className = `service ${state === "up" ? "ok" : state === "unknown" ? "loading" : "down"}`;
    I18n.attr(coordinator, "title", error
      ? I18n.msg("Coordinator error: {error}", { error: I18n.errorText(error) })
      : I18n.msg(state === "up" ? "Coordinator is reachable." : "Waiting for Coordinator"));
    if (health) {
      engines = health.map((service) => {
        const item = node("span", `service ${service.state}`, labels[service.role] || service.role);
        item.title = `${service.name} at ${service.url}: ${service.state} (${service.detail})`;
        return item;
      });
      services.replaceChildren(coordinator, ...engines);
    } else if (state !== "unknown") {
      for (const item of engines) {
        item.className = "service";
        I18n.attr(item, "title", error
          ? I18n.msg("Coordinator error: {error}", { error: I18n.errorText(error) })
          : I18n.msg("Waiting for Coordinator"));
      }
    }
  });
})();
