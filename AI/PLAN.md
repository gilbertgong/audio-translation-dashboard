# Plan

Scope: audio-translation-dashboard only. If embedded as a subtree, this plan does not define the
containing repository's architecture, Git workflow or work scope; see MEMORY.md's scope boundary.

The present state, rewritten when it changes. History is in `AI/PROJECT_LOG.md`; the detailed design in `DESIGN.md`.

## Goal

A local, self-hosted live translator between two languages (English and Chinese by default): speech from the
microphone or a captured browser tab is transcribed, translated, shown side by side with timings and debugging
details, and spoken aloud in a natural voice. Good means: text appears on a breath, speech follows at the end of the
speaker's turn, nothing needs the cloud, and it runs on anything from a 24 GB desktop GPU to a 6 GB laptop (and a
Mac).

## Current direction

Feature-complete for everyday use; now polishing playback and capture from real use, and making the smaller-machine
recipes good.

## Approach (decisions in force)

- **Existing engines as local HTTP servers:** whisper.cpp (STT), llama.cpp (translation), Kokoro or Qwen3-TTS (speech).
  *Why:* best quality per effort, and any engine, model or machine can be swapped by config.
- **Standard-library Python and a plain page**, no frameworks (`microweb.py` is ours). Engine packages live in each
  server's own folder (`pip install --target`). *Why:* nothing to install beyond Python 3.11+ and a browser.
- **Configuration fixed at startup,** chosen before launch (Generate config: recipes of validated fragments).
  *Why:* keeps the app simple; the page only has controls you'd change while using it.
- **Whisper owns language direction:** out-of-pair transcriptions are rechecked before translation;
  llama gets an explicit source and target and returns no language tags.
  *Why:* translation depends on transcription accuracy; avoid making llama infer direction again.
- **Kokoro through misaki** (`kokoro_server.py`), not sherpa-onnx's front end. *Why:* the model was trained on
  misaki's phonemes, matching the model reference front end.
- **Default voices:** stock Kokoro af_maple (English) and zf_001 (Chinese); Qwen qwen_demo clones
  the official hosted sample. Reference audio is downloaded/cached locally, not bundled in Git.
- **Recipes:** 24 GB: whisper large-v3, Qwen3.8-27B, Qwen3-TTS 1.7B. 8 GB: whisper large-v3, Qwen3.5-4B UD-Q4_K_XL,
  Kokoro on CPU. 6 GB: whisper large-v3-q5_0, Qwen3.5-4B UD-IQ3_XXS, Kokoro on CPU. The laptop llama runs with
  `--ctx-size 2048`. *Why:* a smaller translation model leaves room for a better whisper; one phrase needs little
  context.
- **Automatic playback waits for the end of a turn** (`turn_pause_ms`, 2 s) on every running source, except a muted
  captured tab, which it talks over. The backlog is limited by seconds of speech (`max_backlog_ms`, 30 s), oldest
  dropped. *Why:* never talk over a live speaker; a muted tab can't be heard, so there's nothing to talk over.
- **Each server gets one request at a time per tab,** with the stages overlapping. *Why:* servers stay responsive and
  results stay in order.

## Results so far

- Pipeline terms are documented in DESIGN.md: session (browser tab), segment (browser-submitted
  audio/text), request (model task), attempt (backend execution/retry), and reserved app-side chunk.
  App recorder/splitter names and comments follow those terms; compatibility fields/keys are unchanged.
  No telemetry, correlation IDs, session registration or app-side chunk/VAD behavior added.

- Mac setup defaults to the pinned native Qwen3-TTS Apple Silicon Metal release,
  with the existing 1.7B base model/codec and cached reference voice; `TTS=kokoro` selects CPU speech.
  Existing generated configs are preserved. Real Mac installation/audition remains untested.

- Minimum detected speech per phrase is now 100 ms, to admit brief isolated words;
  pre-roll remains 300 ms. User audition will determine the effect on missed words and noise.

- Qwen voice config accepts HTTP(S) references cached under voices/cache/ before base64 registration.
  Custom voice assets are no longer tracked; Kokoro uses stock voices. Smoke clips are generated locally.

- Reopening Generate config restores advanced selections and endpoints immediately, before GPU
  detection; later GPU suggestions preserve these backend choices. Nine generator checks passed.

- Generate config has Advanced mode: individual local fragments, remote host/port or Off per
  backend, with one grouped speech selector. Remote choices use defaults for other fields.
  Remote choices appear last. Fragment size comments feed per-selection sizes and local totals,
  with advisory +10%/+20% VRAM estimates (CPU/remote excluded from local VRAM).
  File sizes appear in dropdown labels, VRAM alongside (CPU zero shows a `Runs on CPU` tooltip),
  with a centered, top-aligned total VRAM label whose entire text reveals the estimate explanation.
  Missing size comments fall back to the combined size of complete downloaded model files;
  missing files remain unavailable, with no network access or downloads.
  Preview colors TOML syntax. Six isolated generator checks passed, including actual Tk mode
  switching and saving to a scratch file; existing services/config files were untouched.

- Kokoro v1.0 fragment uses the existing ONNX engine with package-specific reference
  preprocessing and stock voices. Pipeline setup/chunking is checked against a pinned upstream
  KPipeline; known target languages are passed explicitly. Deliberate differences are documented
  in DESIGN.md. Existing recipes/configs stay on v1.1; v1.0 model audio has not been auditioned.

- Session audio uses automatically deleted temporary files by default, retained as open handles
  only while needed for rechecks. `[diagnostics] save_session_data = true` restores saved WAVs
  and application content logging. Operational diagnostics and API-server logs are unchanged.

- Playback panel opposite Capture audio: pause/resume, stop/clear, restart and next,
  seek bar with elapsed/total clip time, output dB meter, queue status and persistent per-language 0.75×–1.5× speed controls.
  Browser-side PCM WSOLA prototype preserves pitch at non-unit speeds; 1× bypasses processing.
  Offline signal/transport checks passed; actual voice quality remains for user audition.

- Repository/download URLs use `audio-translation-dashboard`.

- Launcher app updates: `app.version` tracks app/launcher versions independently. ZIP updates replace
  app and launcher source files while preserving user data. The running launcher version is captured
  separately; a newer installed version prompts for OK/Cancel to restart. OK uses the existing restart
  action with no session restoration; Cancel resumes the old run and leaves a restart-needed label.
  Small App/Launcher version lines sit in the upper right. Sixteen offline updater checks passed.

- Generate config includes `remote-nvidia-24gb`: enter one desktop IP/hostname for Whisper, llama
  and Qwen3-TTS; all local engines are skipped using existing `start = false` behavior.

- Interrupted automatic speech resumes from its heard position with a 100 ms rewind, rather than
  starting over; waiting backlog counts the remaining audio.

- Kokoro CPU threading defaults to ONNX Runtime automatic selection (`--threads 0`).

- Live, text and component-test views stay mounted in a persistent shell. Capture, engine queues,
  playback, conversation history and drafts survive switching views; typed requests share live queues.

- Text translation uses a dashboard-styled chat layout: scrolling history, bottom composer, Enter to
  send / Shift+Enter for new lines, source/target labels with swap, streamed replies with Stop/Retry, and
  per-response Copy/Listen. Linked from the dashboard and component tests.
- Generate config offers GPU selection on multi-GPU NVIDIA machines, remembers the last generated card,
  and updates the recipe suggestion/preview on selection. Save refreshes the launcher settings.

- The full pipeline works end to end, from the microphone and from captured tabs, with streamed translation and
  speech starting about as soon as the first audio arrives.
- Wrong-language transcriptions run about 1 in 100 (quiet Chinese heard as Russian); the recheck fixes them.
- Kokoro fp32 on CPU runs about 4–9× real time; Qwen3-TTS about 5.5× on the GPU with first audio in 0.03–0.05 s.
- The browser's microphone echo cancellation transcribes the user even while talking over a video playing on the
  speakers.

Reference hardware for figures in the docs: a desktop with an AMD Ryzen 7 9800X3D, an RTX 3090 Ti 24 GB and an
RTX 3060 Ti 8 GB (used to stand in for the 8 GB laptop recipe).

## Orientation

- `launcher.pyw` (Windows) or `scripts/launch.sh`: install, download, run. `scripts/setup_mac.sh` first on a Mac.
- `src/app.py` (backend, endpoints), `src/microweb.py` (HTTP framework), `src/run_servers.py`,
  `src/install_api_servers.py`, `src/download_models.py`, `src/configuration.py`, `src/kokoro_server.py`.
- `src/static/`: `index.html` + `app.js` (dashboard), `test.html` (component tests), `smoke.html` (end-to-end smoke
  test with recorded speech).
- `config/defaults.toml` (every setting), `config/fragments/`, `config/recipes/`.
- `voices/README.md`: stock voices, URL references and generated local test audio.
- Full detail: `DESIGN.md`.

## Open questions

- Should `scripts/setup_mac.sh`'s tiers under 32 GB also move from the 9B to the 4B fragments, like the laptop recipes?

## Known issues

- With a muted captured tab and a fast speaker, playback falls about 30 s behind and stays there (the backlog is
  full; the oldest are dropped). Workaround: none yet; see next steps.
- The new 4B laptop recipes haven't been tested on an 8 GB or 6 GB laptop yet.
- `scripts/setup_mac.sh` / `INSTALL_MAC.md` haven't been tried on a real Mac.
- More in `DESIGN.md`, Known limitations.

## Next steps

- Future telemetry planning: token counts for every model are the top priority, with recheck/retry
  attribution and performance timings. Consider MessageProxy routing/capacity and independent app/proxy
  observations, Prometheus aggregates and separate detailed accounting/admin access. None is implemented;
  this first pass only establishes terminology. Chunk IDs remain reserved for later app-side splitting.

- User-requested work is tracked in `AI/TODO.md`. Language direction was the highest priority;
  implemented explicit source/target prompts from Whisper after rechecks, with no llama language tags.
- Remaining requests include investigation of a better llama model for the 6 GB recipe.
  Kokoro Chinese voice and app update/version checking are implemented; see `AI/TODO.md`.
- Shell entry points live in `scripts/`. `scripts/release.sh VERSION` now releases already committed
  code: metadata commit, annotated tag, atomic branch/tag push, remote verification and gh release.
  It bumps launcherVersion when launcher.pyw or its three imported project modules differ from the
  recorded launcher tag. Keep that list synchronized with import/file changes (see MEMORY.md).
  Isolated local-remote/fake-gh checks passed; no real release was performed.
- Try the 4B recipes on real laptops (speed, memory, translation quality).
- Try the Mac install on a real Mac.
- Longer-term ideas: `DESIGN.md`, Extension points.
