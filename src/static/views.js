// Navigation changes the visible view, never the running dashboard document.
const appViews = {
  live: document.getElementById("live-view"),
  text: document.getElementById("text-view"),
  tests: document.getElementById("tests-view"),
};

function showAppView() {
  const requested = window.location.hash.slice(1);
  const active = Object.hasOwn(appViews, requested) ? requested : "live";
  for (const [name, view] of Object.entries(appViews)) {
    view.hidden = name !== active;
    if (name === active && view.dataset.src && !view.hasAttribute("src")) {
      view.src = view.dataset.src;
    }
  }
  document.querySelectorAll("header [data-view]").forEach((link) => {
    if (link.dataset.view === active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  document.getElementById("smoke-link").hidden = active !== "tests";
}

window.addEventListener("hashchange", showAppView);
showAppView();
