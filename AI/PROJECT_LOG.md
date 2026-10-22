# Project log

The project's history: what was tried, found and decided, and why. Appended to, newest last; the current state is
in `AI/PLAN.md`. Facts plain from the code or the git history aren't repeated here.

## 2026-10-09 to 2026-10-14: milestones 1–14

Built in milestones, each recorded when done (moved here from DESIGN.md on 2026-10-15; references to "above" mean
DESIGN.md).

### Milestone 1 — Web server and audio capture ✅

- Standard-library Python server, a page with a Start/Stop button, and 16 kHz mono WAV capture in the browser, saved by the backend.

### Milestone 2 — STT via whisper.cpp ✅

- Install, model-download and launch scripts; shared logging.
- Each recording is transcribed by whisper-server (language auto-detected) and shown as soon as it's ready.

### Milestone 3 — Translation via llama.cpp ✅

- Configurable two-language system prompt; translation with thinking off. Measured on Qwen3.8-27B: with thinking on, "Hello, testing." took 3.5 s and 118 tokens; with it off, 0.3 s and 5 tokens.
- Mock mode for working on the page without the servers.
- Completes the MVP pipeline: record → whisper → llama → display.

### Milestone 4 — Automatic splitting on pauses ✅ (tuning)

- Continuous Audio Mode (see DESIGN.md, Flow). The detector measures background noise for the first 400 ms, then tracks it continuously (falls fast, rises slowly — more slowly mid-phrase); speech is anything `threshold_db` above it, never below `min_threshold_db`. A phrase starts at the first loud block (with `pre_roll_ms` of earlier audio) and ends after `split_pause_ms` of quiet (250 ms; first 600 ms, shortened so text appears on a breath) (keeping `tail_ms`) or at `max_phrase_s`; phrases with less than `min_speech_ms` of speech are dropped.
- **Known limitation:** a perfectly steady sound longer than about 9 s is absorbed into the background estimate and ends the phrase (deliberate, so constant noise can't hold it open).

### Milestone 5 — Dashboard layout and debugging aids ✅

- Conversation/activity grid with left/right alignment by language; timings as the main activity information; collapsible Activity column.
- Controls panel aligned with the window; level meter; gain slider; Space shortcut.
- Play buttons and raw whisper/llama request/response viewers.
- Browser microphone processing in the config, with automatic gain control off by default: Chrome's AGC restarts every time the microphone opens and can swing the level (including the Windows microphone level) during the first seconds, which mangled short recordings on a laptop's built-in mic.

### Milestone 6 — microweb ✅

- `microweb.py`: Flask-style, not Flask-compatible. `@app.get`/`@app.post` routes with exact paths; handlers take a request (`method`, `path`, `query`, `headers`, `client`, `body()`, `json()`) and return a dict/list (JSON), str, bytes, `None` or `(body, status)`; `HTTPError(status, message, **extra)` becomes a JSON error. Static files with explicit content types and `no-cache`; no directory listings.
- Request bodies need a `Content-Length` (411 otherwise: browsers always send one, and it lets oversized bodies be rejected before reading) and are limited to 1 MB unless a handler allows more (recordings use `max_upload_mb`).
- Wrong method → 405 with `Allow`; unexpected exceptions → 500 with the traceback logged. `app.handle()` runs a request without a network connection, for tests.
- On Windows, Python's HTTP server would let a second server bind a port already in use; microweb disables that, so a taken port gives a clear error.
- Handlers use module-level state in `app.py` (`config`, `mock`), set in `main()`.

### Milestone 7 — Streaming translation ✅

- `/api/translate` streams (see DESIGN.md, Backend): llama's `stream: true` response is relayed byte for byte after one `request` event of our own, so the raw viewer shows llama's actual chunks. The page shows the text as it arrives and the time to first token under the translate time.
- microweb: a handler can return `Stream(chunks, content_type)`, which sends each piece as it's produced (no `Content-Length`; the connection closes at the end).

### Milestone 8 — Text-to-speech ✅

- Surveyed current TTS models and servers; chose Qwen3-TTS via qwentts.cpp (good English and Chinese, voice cloning, streaming, a small C++ server). Compared the 0.6B and 1.7B base models on CUDA and Vulkan with a cloned voice; chose the 1.7B on Vulkan (see DESIGN.md, Engines). Measured first audio after about 0.03–0.05 s at the server; end to end through the dashboard, 3.4 s of Chinese speech took 0.84 s in total.
- tts-server installed, its two model files downloaded, and its voices registered at start by the scripts; the default voice is cloned from Beth Thomas's LibriVox reading (see `voices/beth_thomas/README.md`).
- `/api/speak`, the ▶ button after each translation, streamed playback, and Automatic Translation Playback with a queue.
- Dashboard cleanup: Continuous Audio Mode on by default; time to first token on the translate line; split and file details moved behind the `ⓘ` button.
- **Known limitation:** with Continuous Audio Mode and automatic playback both on, the microphone is open while the translation plays; if speakers get past echo cancellation, the speech is picked up as a new phrase. Headphones avoid it.

### Milestone 9 — Kokoro on sherpa-onnx ✅

- Qwen3-TTS works well but takes 2.2 GB of model files and 4 GB of GPU memory. Kokoro (a descendant of StyleTTS 2, 82M parameters) is a fraction of that; the only Windows binaries for it are sherpa-onnx's library, hence `sherpa_server_win.py`.
- Benchmarked against a StyleTTS 2 baseline (same sentence, 10 runs): StyleTTS 2 0.75× real time on one CPU thread, 3–4× on eight, 24× on GPU; Kokoro fp32 3×, 8× and 26×. The first Kokoro test used the int8 model and looked slow (about 1.5×); the cause was int8 itself (above), not the model or the binding.
- Listening: Kokoro's Chinese voices sound great in Chinese but poor in English; Qwen's cloned Beth sounds great in both. So the desktop keeps Qwen3-TTS, and Kokoro serves machines without GPU memory to spare.
- The scripts gained `.tar.bz2` support, models inside archives, and servers that are project scripts; one `[api_servers]` section per engine, chosen by `[tts] server`.
- The same server got SenseVoice STT and Silero VAD endpoints for later (above). Lessons there: sherpa-onnx's newest SenseVoice release (2025-09-09) is a Cantonese fine-tune, the general model is 2024-07-17; and for SenseVoice, unlike Kokoro, int8 is the fastest and smallest on CPU (an fp16 export saves disk but not RAM: the CPU converts it to fp32).

### Milestone 10 — Component test page ✅

- `test.html` (see DESIGN.md, Component tests): transcribe, translate and speak one at a time, with a voice picker, so each engine can be checked on its own — e.g. Kokoro's English voices without a Chinese speaker at hand. `/api/speak` takes an optional voice; `/api/voices` lists them.

### Milestone 11 — Configuration: layers, fragments, recipes ✅

- Every server's models in its own `[models.<name>]` table (whisper and llama included, now passed as `--model`), so fragments never share a table.
- Fragments for the nine validated server + model pairs, recipes for 24, 8 and 6 GB NVIDIA cards, and the launcher's Generate config (GPU detection, languages, preview, save).
- `config_generated.toml` as a layer between the defaults and `config_local.toml`, all read by one module, `configuration.py`, replacing the copies of the merge code in `app.py`, the scripts and the launcher.
- `[languages]` split out of `[llama_prompt]`, since more than the prompt uses them; TTS fragments set `[tts] server`, and each TTS server's voices (`default_voice`, `language_voices`, Qwen's registered `voices`) live with it in `defaults.toml`, so switching engines never leaves another engine's voice names behind; per-language overrides `voice_language1`/`voice_language2`.

### Milestone 12 — Project layout and launcher ✅

- All Python (and the pages) moved into `src/`, leaving `launcher.pyw` as the obvious thing to run; modules import each other directly, with no path workarounds.
- Launcher: Open dashboard on top; colored ▶ Launch and ■ Stop that fade when they don't apply; Generate config; Kill servers; Mock (optional), switching cleanly with Launch.
- Dashboard: server status in the header from `/api/health`.
- Servers on another machine work as configured (above); `local_proxy.py` for quick tests from a laptop.

### Milestone 13 — Language checks ✅

- whisper's language label and llama's language tag, with a recheck that transcribes again forced to each of the two languages and keeps the more confident result; the row is corrected while you watch (see DESIGN.md, Flow). From the logs: 1 wrong-language transcription (Russian, for quiet Chinese) in 96, which the recheck fixes.
- whisper-server's `verbose_json` turned out to include `language_probabilities` and per-segment `avg_logprob` and `no_speech_prob`; the recheck uses `avg_logprob`.

### Milestone 14 — Kokoro with misaki ✅

- A Kokoro voicepack for Beth (`voices/beth_thomas/RC3`), found by a search on the PyTorch `kokoro` pipeline, sounded bad through `sherpa_server_win.py`, Chinese most of all. Same model data (the pack's tensor checked: float32, `[510, 1, 256]`, contiguous), so the suspect was the text front end, as with StyleTTS 2, where swapping back the phonemizer it was trained with was a sudden great improvement.
- Tested three ways on the same sentences: sherpa-onnx's front end; misaki on sherpa-onnx's ONNX model (a prototype); PyTorch `kokoro`. misaki on ONNX and PyTorch gave identical phonemes and identical durations, and her Chinese sounded top tier in both: the front end, not the ONNX export. sherpa-onnx's C API takes only text, so it can't be given misaki's phonemes; hence `kokoro_server.py`.
- misaki's English still misreads some names (Beth as "bait"); PyTorch does the same.
- Japanese added: lines with kana go to misaki's Japanese G2P (before, they were read as Chinese, or as "Japanese letter" over and over by the English G2P). All its phonemes are in v1.1-zh's vocabulary. misaki reads a decimal point as a sentence end (23.5度 as "nijūsan. go do") and leaves Latin letters as they are; the model mostly drops those. A line of kanji only (東京駅) is still read as Chinese.

## 2026-10-14: Japanese, playback, laptop recipes

- **Japanese in `kokoro_server.py`:** misaki's cutlet G2P (fugashi + unidic-lite, with jaconv and mojimoji). `pyopenjtalk`
  is replaced by an empty module: misaki.ja imports it but only its non-default G2P calls it, it has no Windows wheel,
  and `pyopenjtalk-plus` would add about 370 MB. Phonemes were checked identical with it installed.
  `[api_servers.kokoro.language_voices] japanese = "beth_thomas"`.
  - Kokoro v1.1-zh isn't trained on Japanese, but the user's hands-on work showed it speaks it well from misaki's
    phonemes; the gap was our front end, not the model.
  - A first batch of test WAVs came out garbled because curl from Git Bash mangled the UTF-8 text; sending from Python
    was fine. Not a server problem.
- **One request at a time per server, per tab** (`oneAtATime()`: `toWhisper`, `toLlama`, `toTts`). The stages still
  overlap; each server just never gets two requests from one page at once. A whisper stress test on an 8 GB card
  passed 140/140 this way.
- **Speech fetched ahead** when queued for automatic playback (`loadSpeech`, `cancelSpeech`), so it starts at once
  when its turn comes.
- **`[tts] playback_buffer_ms`** (800): audio buffered before speech starts, so a slow start doesn't stutter. The
  user chose 800 ms by ear; the page fallback and the test page's Buffer field match.
- **Backlog by time, not count:** `max_backlog_ms = 30000` replaced `max_backlog = 3`. It counts only waiting speech
  fetched in full, drops the oldest while over, and always keeps the newest. A count says nothing about how far
  behind playback is; seconds do. Wasted synthesis of dropped items was judged not to matter.
- **`voices/` reorganized** into a folder per speaker (`voices/beth_thomas/`: the Qwen reference WAV, the Kokoro
  voicepack `TB2BP2G80.pt` and its `RC3/` search notes, and `test_audio/`).
- **Launcher:** reloads the config (rebuilding the server buttons) after Generate config saves; a dev-only
  Restart launcher button.
- **Mac:** `setup_mac.sh` and `INSTALL_MAC.md` (Homebrew's Metal builds of whisper-server and llama-server, since
  whisper.cpp publishes no macOS server build). Untested on a real Mac.

## 2026-10-15: laptop models, mute captured tab, AI docs

- **Laptop recipes moved to Qwen3.5-4B** with `--ctx-size 2048`: 8 GB gets UD-Q4_K_XL (2.91 GB) with whisper
  large-v3 (from large-v3-q5_0); 6 GB gets UD-IQ3_XXS (1.95 GB) with whisper large-v3-q5_0 (from
  large-v3-turbo-q5_0). The smaller translation model frees memory for a better whisper; a 2048 context is plenty
  for one phrase plus the prompt. Not yet tested on the laptops. The 9B fragments remain, used only by
  `setup_mac.sh`.
- **Mute captured tab** became a checkbox set before capturing. The old button changed `suppressLocalAudioPlayback`
  with `applyConstraints` mid-capture, which Chrome silently ignores; it's only honoured in the `getDisplayMedia`
  request.
- **Automatic playback talks over a muted captured tab** and waits only for the microphone: the tab can't be heard,
  so there's nothing to talk over. Works well. With a fast speaker the backlog then stays full (playback about 30 s
  behind); the user wants a manual playback speed control for that, not automatic speed-up (a TODO, low priority).
- **Open dashboard reusing an existing tab** was considered and dropped: no browser lets another program bring an
  existing tab forward without an extension or a launcher-owned window.
- Observed: the browser's echo cancellation on the microphone is good enough to transcribe the user talking over a
  video playing on the speakers.
- **Requested work:** tracked in `TODO.md`: language direction (highest priority; discuss before building),
  confidential mode, a convenient typed translation page, and advanced controls including playback speed.
- **AI docs:** `AI/` set up per `AI/STRATEGY.md` (MEMORY, PLAN, this log, a git-ignored LOCAL), with `AGENTS.md`
  and `CLAUDE.md` pointing to it, and a `README.md` for people. DESIGN.md's milestones moved here; it gained a
  Known limitations section.

## 2026-10-15: explicit translation direction

- The user prioritized language direction: larger Whisper models and smaller llama models on the 8 GB/6 GB recipes reflect transcription accuracy being the critical stage.
- Whisper's final language (after any out-of-pair recheck) now selects the source and opposite target in each translation prompt. Missing or out-of-pair sources fail explicitly instead of guessing.
- Removed llama language tags, tag stripping and the llama-triggered recheck. Kept Whisper's recheck. Component tests now select a source language and preserve bracketed text.
- Validation passed: Python/JavaScript syntax, both prompt directions, invalid-source errors, forced-language recheck selection, dashboard source propagation and bracket preservation, and a separate mock dashboard on port 8090 (stopped afterward). No real-model or GPU tests; the user's running services were left alone.

## 2026-10-15: config generator GPU selection

- Added a GPU dropdown only for multiple detected NVIDIA cards, labelled with index, name and VRAM. Defaults to the saved generated GPU when still present, else the largest card. Selection updates the suggested recipe and generated GPU index.
- Confirmed Save already calls the launcher's config reload after replacing the generated file; made launcher button refresh unconditional. Running services are not restarted by Save.
- Save closes the config generation window after the file is saved and the launcher refresh completes.
- Validation passed with mocked GPU output/widgets: enumeration, largest/saved defaults, switching cards and preview index, hiding the selector on single/no-GPU machines, save/reload order, launcher refresh, Python syntax and diff whitespace. No GPU workloads, live launcher UI or running-service changes.

## 2026-10-15: dedicated text translation page

- Added `translate.html`, `translate.css` and `translate.js` for everyday typed translations, linked
  from the dashboard and component tests. Explicit language direction, swap/reverse translation,
  streamed results, cancel, clear, copy, listen/stop and Ctrl/Command+Enter; mobile panels stack.
- Uses the existing config, translation and speech endpoints. No debug output in the page. Failed or
  cancelled streams retain partial text labelled incomplete and disable result actions.
- Browser checks passed against a separate mock dashboard on port 8090: streaming with brackets,
  keyboard submit, swap, clear, cancellation, server errors, mobile overflow and no JS exceptions.
  JavaScript syntax and diff whitespace checked. Mock stopped afterward; real model servers and GPUs
  untouched. The sandbox denied localhost access, so browser checks used an approved external retry.

## 2026-10-15: text translation as a chat

- At the user's request, replaced the two-panel translator with a scrolling conversation and a bottom
  composer. Enter sends, Shift+Enter adds a line; IME composition is preserved. Messages and streamed
  translations remain in order, with source/target labels and per-response Copy/Listen.
- Matched dashboard colors, type, compact controls, panel borders and the controls-panel background.
- Simplified the chat's direction controls to source/target labels and Swap, removing the redundant
  source-language dropdown at the user's request.
- Moved New conversation left and language controls right; matched dashboard translation accent color
  and font sizes, with compact copy and play/stop icons, tooltips and accessible labels.
- Kept direction switching, Stop and new conversation; added Retry in place. Drafts survive ongoing
  translations and clearing the conversation. Translation context remains one independent message.
- Browser checks passed on an isolated mock dashboard: multiple turns, Enter/Shift+Enter, bracketed
  text, per-response copy and speech target, retry without duplicate messages, Stop, preserved drafts,
  mobile composer visibility and new conversation. No browser exceptions. Mock stopped afterward;
  real services left running. Speech response was mocked; real audio quality was not tested.

- Matched chat playback button dimensions to the live dashboard: removed the 24px height override,
  keeping the shared button font size, line height, padding, border and minimum width.

- Shared translation controls between the chat and live dashboard: equal-size play and copy icons
  inline after the translated text. Both use `translation-actions.js` and one CSS sizing rule;
  added copy to live translations. Speech/copy use translation text without icon labels. JavaScript
  syntax and diff whitespace checked; no running services restarted.

## 2026-10-15: preserve live capture across views

- User reported live issues after navigating to text translation and returning; later recognized the
  same behavior with component tests. The exact original failure was not reproduced or established.
  User chose preserving active sessions over cleaning them up on view changes: YouTube capture must
  keep translating while typed translations are used.
- Added persistent live/text/test views using hash navigation and retained same-origin frames. Old
  text/test URLs enter the shell. Hidden live capture keeps its existing media session, queues and
  playback; chat history/drafts and component inputs remain mounted. Smoke opens separately.
- Header layout: title alone on top; navigation/status on the second row, with Live translation and
  Text translation on the left. User explicitly requested skipping testing for layout-only changes;
  recorded that preference in MEMORY.md and did not test this layout edit.
- Typed translation and TTS generation share the dashboard engine queues. Cleanup now applies only
  to actual document exit, never internal view switches; live Space shortcut is scoped to live view.
- Mocked browser regression passed with synthetic capture and the actual recording worklet: capture
  identity/track stayed active, live pipeline completed in the text view, chat history/draft and test
  input survived, hash Back worked, and no page exceptions occurred. No real YouTube/model tests.
  Sandbox Edge crashed; approved external check used only mocked routes and synthetic audio. Its first
  worklet URL failed in the harness; loading the same worklet from a test Blob fixed the test setup.

- Text chat spacing: removed the centered 820px message-column limit and matched live conversation
  side padding (16px desktop, 10px narrow screens), bringing messages closer to the panel borders.
  Layout-only change; no tests run per user preference.

- Unified typed translation rows with the live conversation using the same `.utterance`, `.meta`,
  `.original`, `.translation` and `.lang2` classes. Removed chat bubbles and separate user/assistant
  rows, keeping source and target stacked on one background with identical spacing and colors.
  Layout-only change; no tests run per user preference.

- Removed the divider between the dashboard title and navigation row. Layout-only; no tests run.

## 2026-10-15: additional requested TODOs

- Recorded launcher updates/version checking, changing the default Chinese Kokoro voice, and
  investigating whether the 6 GB recipe needs a better llama model. No implementation requested yet.
- Update discussion: support release ZIP installs without Git; local/remote JSON `.version` files
  carry version, direct download URL and master version file URL. Compare numeric version components;
  the remote file describes the latest published release. Stage a ZIP, then run a temporary updater
  outside the installation folder after shutdown to install, preserve user data and restart.

## 2026-10-15: automatic Kokoro CPU threading

- Changed the server argument default, default config, Kokoro recipe fragment and existing generated
  config from four threads to `0`, letting ONNX Runtime automatically choose its CPU thread pool.
  Explicit thread counts remain supported. No running servers restarted or benchmarks performed.

## 2026-10-15: resume interrupted automatic speech

- Automatic playback now keeps the heard position and resumes 100 ms earlier after the normal turn
  pause, skipping complete chunks and offsetting into the first remaining chunk. Playback positions
  use the AudioContext's scheduled segments, excluding buffering and scheduling gaps. Repeated
  interruptions before resumed audio starts do not rewind again. Backlog uses remaining duration.
- Discussed waveform-based rewind to a quiet point: it cannot reliably identify word boundaries, so
  kept the requested fixed rewind rather than adding signal analysis or alignment.
- Isolated scheduling checks passed: cross-chunk and repeated resume, pre-start interruptions, gaps,
  beginning clamp and remaining backlog. JavaScript syntax and whitespace checked; no real servers
  or audio quality tests, and running services left untouched.

## 2026-10-15: reload config on Launch

- Launch/Mock refreshes config-dependent server controls before starting. Child processes already
  read files at startup; manual edits do not require restarting the launcher. Removed the added
  validation/startup-blocking behavior at the user's request; only config refresh was requested.
- Clarified the standing principle in `AI/MEMORY.md`: keep configuration simple and read it at
  startup; avoid spending effort wiring settings through the entire app or building unnecessary
  configuration infrastructure. Field flexibility follows from that broader simplicity philosophy,
  rather than a blanket ban on parsing or error handling.
- Further clarification: simple configuration code must support complex deployments, including ten
  servers. Follow configured entries; recipes and familiar deployment shapes are not restrictions.
  Simplicity belongs in the implementation, not in limits imposed on user configuration.

- Final clarification of the configuration principle: do not add configuration validation; read and
  use what is specified. Deployment examples illustrated why not to judge configurations, rather than
  requesting deployment-specific infrastructure. Updated MEMORY.md to state this directly.

## 2026-10-18: remote desktop recipe

- Added `remote-nvidia-24gb`, a laptop-side recipe targeting a desktop's normal 24 GB setup. Existing
  `start = false` skips local installs, model downloads and startup; runtime host/port support is unchanged.
- Recipe metadata requests a Remote host text field (default 127.0.0.1), expanded into the fragment's
  Whisper/llama/Qwen3-TTS endpoints. No hostname/IP validation. Local GPU selection is hidden for this
  recipe, no local GPU is pinned, and remote recipes are excluded from GPU-based recommendations.
- Isolated generation checks passed: default/custom/empty/quoted hosts, ports, Qwen TTS selection,
  all local servers skipped after config merging, local GPU recommendations unchanged, and local
  recipes byte-identical to previous rendering. No real servers contacted or generated/user configs
  modified; no live desktop/laptop network test or launcher UI test performed.

## 2026-10-18: separate versions and app-only ZIP updates

- User chose separate app and launcher versions, both initially 0.1.0. `app.version` also records direct
  download and master metadata URLs. The latest master describes the published release. A newer
  launcher is informational, never a prerequisite for updating the app. An optional launcher-only
  helper is future work, not part of this change.
- Added manual Check for updates, download/confirmation, staging, owned-process shutdown, app overlay
  and prior-mode restart. Launcher stays open/unmodified; installed launcher version stays unchanged
  in local metadata after app-only updates. Full-update availability and download URL are displayed.
- Explicit app-file scope excludes user configs, voices, models, installed engines, logs, AI notes and
  Git metadata. ZIP paths stay within staging/install roots; failures roll back modified/new files.
- Eleven standard-library offline tests passed, covering version checks, newer-launcher information
  without an app gate, preservation, rollback, download failure, archive traversal/version mismatch,
  and launcher shutdown/resume sequencing. Scratch installs and mocked network/processes only; no
  actual update applied, no running services changed, no release published or live UI/network test.

## 2026-10-21: repository rename

- Updated the Git origin, release ZIP URL and master version URL to
  `audio-translation-dashboard`, matching the user-planned GitHub rename.
- User completed the local folder rename after closing the editor that held it open.
- User bumped the app version and release ZIP target to 0.3.2; launcher remains 0.3.0.
  ZIP extraction discovers the wrapper folder from app.version, independent of repository name.
- All eleven offline updater tests passed; git diff --check passed.

## 2026-10-21: playback panel

- Added Playback opposite Capture audio, reusing panel, button and meter styles;
  responsive layout keeps both panels and recording controls usable on narrow screens.
- Transport controls hold streamed speech on pause, clear/cancel queued speech on stop,
  restart fetched audio or skip to the next clip. Manual recording playback shares transport
  and metering. User pause survives incoming speech, streaming, speed changes and view switches.
- Per-language browser-persisted speed sliders apply at the current position; scheduled chunk
  durations, interruption positions and backlog seconds account for speed. Rate changes pitch.
- Verified with an isolated tone-generating mock dashboard and desktop/narrow browser layouts:
  pause/resume, restart, rate change, meter, view switching, next while paused, stop/queue clear.
  Offline Node checks cover scheduling/positions, interruption rewind, per-language rates,
  cancellation, meter, recordings and streamed arrival while paused. No real model servers used.

- Follow-up: added a matching current-clip seek bar and elapsed/total time. A trailing +
  indicates total duration is still growing during streaming. Seeking preserves pause and
  handles recordings as well as speech. Offline checks cover drag preview, paused seek,
  stop/reset and growing/final durations; mock browser verified paused seeking to 0:03 / 0:08.

- Layout follow-up: placed the seek bar above transport controls, with playback status
  aligned right on the elapsed/total time row. Layout-only; testing skipped per preference.

- Capture channel meters now reuse the microphone/playback meter styles, colors,
  dimensions and dB typography. Compact L/R labels (M for mono) place label, meter and
  value on one row; full channel names remain in tooltips/accessibility labels. Styling-only
  follow-up; testing skipped per preference.

- Matched the microphone and playback meter containers to the capture channel boxes,
  with centered alignment and consistent border, rounding and padding. Layout-only; no tests.

- Capitalized capture UI labels as Capture Audio and Stop Capture, including the panel/help
  headings and accessible panel name. Text-only; no tests.

## 2026-10-22: targeted session retention defaults

- At user direction discarded the earlier uncommitted confidentiality work and restarted from
  the playback commit. Scope: one config-only opt-in for original behavior, temporary session
  audio by default, and omit conversation text fields in application logs only.
- TemporaryFile provides unlink/delete-on-close semantics across Unix/Windows. Preserve its
  server-side handle for rechecks using the existing filename JSON request; no second WAV upload
  and no retained in-memory audio cache. Close after transcription/recheck, on failure, abandoned
  request expiry (15 minutes), or process exit. Browser playback continues using its own audio.
- Keep application activity/timing/language/confidence/error diagnostics and API-server output.
  No broad log filters, launcher changes, server logging changes, or cleanup of existing user files.
- Offline tests cover actual temporary-file cleanup, recheck ownership, success/failure/expiry/exit,
  original opt-in behavior, content omission and preserved activity/error diagnostics. No live
  dashboard or model servers were restarted or contacted.

- Release metadata bumped to app 0.4.0 at user request; launcher stays 0.3.0.
  Release notes include the playback panel and targeted session retention defaults.

## 2026-10-22: Kokoro v1.0 and reference alignment

- User approved v1.0 support and strongly prefers reference model-package behavior everywhere
  except deliberately chosen differences. Saved that principle in MEMORY.md.
- Added kokoro_v1.0 fragment: existing ONNX/Misaki runtime, v1.0 bundle/repo identity and
  stock voice defaults. voices=false disables the inherited v1.1 Beth pack with one small
  voice-files helper change. Existing recipes, generated and local config files are unchanged.
- --repo-id follows KPipeline's model-package identity rule for Chinese G2P. Matched reference
  chunking, American/British and other language G2P setup, fallback handling and voice blending.
  Dashboard passes known target language to Kokoro; old clients retain script selection.
- Documented the intentional ONNX/local-voice/HTTP transport differences and compatibility
  fallback/English callback. Included the reference license and pinned source for offline tests.
- Thirty offline Python tests passed, including direct chunk-output comparisons with upstream,
  fragment merging, language forwarding and blends. Actual installed Misaki phonemes for a/b/z/j
  matched reference outputs for both package identities. Sandbox initially blocked espeak DLL
  loading; repeating that CPU-only comparison outside the sandbox passed cleanly. No model
  inference, GPU use, v1.0 model download, or running-service changes. Voice quality remains
  for the user's audition.

## 2026-10-22: Advanced config generation and preview coloring

- Added Advanced mode alongside recipes: seeds individual backend choices from the selected recipe,
  groups Kokoro/Qwen under Speech, discovers local fragments and offers Off.
- Remote endpoints are synthesized from backend host/port defaults, with per-server host and port
  entries. No new remote fragment files. Generated remote sections only set connection/start fields
  and the selected speech backend; voices and other fields inherit defaults. Local and remote can mix.
- Added lexical TOML preview coloring without modifying output or introducing configuration checks.
- Six generator checks passed: recipe equivalence, mixed/all-remote assembly, defaults inheritance,
  quoting, syntax tags, actual hidden Tk mode switches and scratch-file Save/callback. No model
  processes, GPU workloads, existing user config files or running services were touched.

## 2026-10-22: Fragment sizes and advanced resource estimates

- Moved both remote speech choices below all local fragments.
- Recorded the requirement for model file sizes in fragment comments in MEMORY.md and supplied
  consistent size comments for all fragments, including codec totals, CPU placement and remote-only
  local size zero. Existing sizes are approximate decimal GB, not compressed archive sizes.
- Advanced mode shows per-selection files/VRAM and local totals. Uses +10% for llama context up
  to 2048 and +20% otherwise; CPU Kokoro adds disk size but zero VRAM. Remote/Off add neither.
  Labels explain this is a rough guide, excludes auxiliary files and other apps, and does not check
  capacity or change generated settings. No services or user config files changed.
- Seven focused generator checks passed, including CPU exclusion, context-dependent overhead, codec
  totals, local/remote totals in Tk widgets and remote choice ordering.

- Layout follow-up: moved file sizes into dropdown labels, per-selection VRAM to their right,
  and Files/VRAM totals into a separate column. Replaced the inline explanation with a `?` hover/
  keyboard-focus tooltip. Updated existing test expectations for the labels; no tests run for this
  layout-only follow-up, per user preference.

- Styled the generator help trigger as an outlined circular `?`, matching the dashboard reference.
  Help now opens/closes on click (also Enter/Space), stays open for reading and has Escape/close-button
  dismissal. No tests run for this presentation change, per user preference.

- User changed the help interaction to a tooltip on hover over the `Total` heading. Removed the
  separate `?` and click popup controls; keyboard focus also shows the explanation. No tests run
  for this layout-only change.

- Replaced the Files/VRAM totals block with one centered, top-aligned `Total Estimated VRAM`
  label; hovering anywhere on it shows the explanation. CPU models' zero-VRAM labels show
  `Runs on CPU` on hover; remote/Off zeroes do not. Updated label expectations without running
  tests for this presentation-only follow-up.

- Investigated missing total/CPU hover help. Both handlers produced the expected popups in an
  isolated Tk window, so the reported pointer behavior was not reproduced. Changed popup creation
  to stay withdrawn until positioned, then raise it, avoiding an initial window mapping that could
  interrupt hover on Windows. Added Enter/Leave coverage for both actual generator labels; all
  seven focused generator checks passed. User-visible hover still needs confirmation in normal use.

- User confirmed hover works. Added model-size fallback when a fragment has no size comment:
  reuse common.model_files paths and configured models.dir, summing only when all required files
  are present/nonempty. No HTTP requests or downloads; comments still take precedence, and
  incomplete downloads remain unavailable. Eight generator checks passed, including scratch
  multi-file totals, missing/.part files, archive members, Whisper aliases, comment precedence,
  CPU exclusion and an assertion that no network call occurs. User files/services untouched.

- At user request, bumped app 0.4.0 → 0.4.1 and launcher 0.3.0 → 0.4.1, including the
  matching v0.4.1 ZIP URL. Preparing commit/push with the advanced generator, size estimates,
  and remaining Kokoro fragment/reference/license files from the preceding work. All 38 Python
  checks passed before publishing; existing services and user configuration were untouched.
- User clarified launcher bumps use the current release version (0.4.1 here), rather than an
  independent patch increment. Corrected the unpublished commit/tag after the initial push failed
  to resolve GitHub from the sandbox.
