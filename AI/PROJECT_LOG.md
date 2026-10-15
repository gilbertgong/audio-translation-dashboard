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
- **AI docs:** `AI/` set up per `AI/STRATEGY.md` (MEMORY, PLAN, this log, a git-ignored LOCAL), with `AGENTS.md`
  and `CLAUDE.md` pointing to it, and a `README.md` for people. DESIGN.md's milestones moved here; it gained a
  Known limitations section.
