// Shared inline controls for completed translations on both pages.
const TranslationActions = {
  create(text, content, onPlay, feedback = () => {}) {
    const container = document.createElement("span");
    container.className = "translation-actions";
    const play = document.createElement("button");
    play.type = "button";
    play.className = "row-btn speak-btn";
    play.textContent = "▶";
    play.title = "Speak this translation";
    play.setAttribute("aria-label", play.title);
    play.addEventListener("click", onPlay);
    const copy = document.createElement("button");
    copy.type = "button";
    copy.className = "row-btn copy-btn";
    copy.title = "Copy translation";
    copy.setAttribute("aria-label", copy.title);
    copy.innerHTML = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true" focusable="false"><rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3"/></svg>';
    copy.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(text);
        copy.title = "Copied";
        feedback("Translation copied.");
      } catch {
        const range = document.createRange();
        range.selectNodeContents(content);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        copy.title = "Text selected. Press Ctrl+C or ⌘C to copy.";
        feedback(copy.title);
      }
    });
    copy.addEventListener("mouseleave", () => { copy.title = "Copy translation"; });
    container.append(play, copy);
    return { container, play };
  },
};
