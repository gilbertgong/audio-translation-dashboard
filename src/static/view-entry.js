// Old page URLs remain usable, but normal navigation lives in the persistent shell.
if (window === window.top) {
  window.location.replace("/#" + document.currentScript.dataset.view);
} else {
  document.documentElement.classList.add("embedded-view");
}
