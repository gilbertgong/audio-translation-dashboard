# Audio Translation Dashboard — Design

## Purpose

A local, self-hosted tool that captures speech from the microphone, transcribes it, translates it, shows the original and translation side by side (with timing and debugging details), and speaks the translation aloud. It uses whisper.cpp and llama.cpp as external services, and for speech either Kokoro (on sherpa-onnx, through a small server of its own) or Qwen3-TTS (qwentts.cpp). It includes a launcher and helpers that download and start them all, models included, and that pick a configuration to suit the GPU.

## Approach

- The core application logic of voice translation is isolated from ancillary configuration and model handling, which are handled instead by configuration files (`config/` and the project's `config_*.toml`) and supporting scripts (in `src/`).
- The configuration is read at startup and fixed while running, so there's no configuration UI and no wiring for settings to flow back into behavior. This keeps the core app far simpler and easier to change and extend. Choosing a configuration happens before starting, in the launcher's Generate config window, which writes a file and nothing else.
- It also lets the UI stay clean and focused on voice translation itself: the page only has controls you'd change while using it.

## Design principles

- **Simple first:** one page, one linear pipeline.
- **Use existing engines** instead of reimplementing them: whisper.cpp for speech-to-text (STT), llama.cpp for translation, Kokoro on sherpa-onnx or Qwen3-TTS for text-to-speech (TTS). Where an engine ships a server, we run it; where it ships only a library (sherpa-onnx), a thin server of ours wraps it, with the same kind of HTTP API.
- **Loose coupling:** each engine is its own local server, reached over HTTP. Swapping a model, engine or machine is a config change.
- **Pipeline of small stages:** Capture → Transcribe → Translate → Display → Speak, so new stages can be inserted later.
- **Stream where it helps:** translations appear token by token and speech starts playing while it's still being generated. Engine responses are relayed unchanged, so what the page shows (and the raw viewers) is exactly what the engine sent.
- **Fix after the fact:** checks that can wait (is this really one of our two languages?) run after a result is shown, and correct it in place, rather than delaying every phrase.
- **No dependencies** beyond Python's standard library (3.11+) and a browser.

## Architecture

```text
    microphone  speakers
        |          ^
        v          |
+---------------------+       WAV audio, text       +---------------------+
|  Dashboard (page)   | --------------------------> |  app.py (backend)   |
|  capture, meter,    | <-------------------------- |  saves recordings,  |
|  splitting,         |  text, timing, speech audio |  calls the servers, |
|  display, playback  |                             |  relays streams     |
+---------------------+                             +----------+----------+
                                                               |
                                      +------------------------+------------------------+
                                      v                        v                        v
                           +---------------------+  +---------------------+  +---------------------+
                           |  whisper-server     |  |  llama-server       |  |  TTS server         |
                           |  speech-to-text     |  |  translation        |  |  Kokoro / Qwen3-TTS |
                           |  (port 8081)        |  |  (port 8082)        |  |  (port 8083 / 8085) |
                           +---------------------+  +---------------------+  +---------------------+
```

The servers are started (and stopped) by `run_servers.py`, which the launcher or `launch.sh` runs; the dashboard only talks to them over HTTP, so any of them can run on another machine (see [Running](#running)).

### Dashboard (`src/static/`)

- **Capture:** microphone → software gain → AudioWorklet. The page builds 16 kHz mono WAV itself, so whisper gets lossless audio in its native format with no ffmpeg.
- **Translation window:** one row per recording. The left column (Conversation) shows the original and its translation, aligned left or right by language; the translation fills in as llama generates it, and gets a ▶ button to speak it once complete. The right column (Activity) shows timings — audio length, transcribe time, recheck time (when one ran), translate time (with time to first token, TTFT, under it), and time until speech starts — plus notes and errors. The Activity column is collapsible.
- **Capture audio** (a sidebar in the left margin; the main area keeps its size when there's room, and the sidebar moves above it on narrow screens): translates the sound of a browser tab, a window or the whole system instead of the microphone. It uses the browser's screen sharing (`getDisplayMedia`) in reverse: you share with the page, and it listens; the video is requested tiny and unused, and the audio comes unprocessed (no echo cancellation, noise suppression or gain control). A meter per channel; until one is chosen, the first channel with sound is (left if both start together), and clicking a meter switches. The chosen channel goes, mono, through the same pipeline as Continuous Audio Mode; its rows are noted "captured audio". **Mute tab** stops a captured tab playing here while it's still captured (`suppressLocalAudioPlayback`), so only the translations are heard. The microphone and a capture can run at the same time (each has its own recording session, phrase splitting and meters: the one by Start Recording is the microphone's, the sidebar's are the capture's; the status line notes "capturing too"); an experiment, to see how the pipeline copes. The code that made them exclusive is kept, commented out. A `?` explains which share choice gives sound (a tab with tab audio, or the entire screen with system audio; a window gives none).
- **Controls:** Start/Stop (or Space), the Continuous Audio Mode and Automatic Translation Playback switches, a gain slider, and a status row and the microphone's level meter aligned under the Activity column. The status row has three independent, unlabeled indicators separated by light dividers (position says which is which; tooltips spell them out): the capture (left) and the mic (middle) each show their source's state (off, calibrating, listening, speech, or recording; for the capture, "no sound yet" until a channel is chosen) and how many of its phrases are still being translated; playback (right) shows what is playing, how many translations are queued for a pause, or whether automatic playback is on.
- **Server status:** in the header, a dot per server — Transcribe, Translate, Speak — green (ok), amber (loading: llama-server while it loads its model), red (down), from `/api/health` every 10 s; the tooltip names the server, its address and what the check found.
- **Speech playback:** the speech arrives as raw 16-bit PCM; each piece is converted and scheduled with Web Audio right after the previous one (with a 0.1 s head start against network jitter), so playback starts as soon as the first audio arrives. Only one thing plays at a time: the ▶ buttons stop whatever is playing, and starting a recording stops playback (speakers would otherwise be recorded).
- **Debugging aids:** each row can play back the exact audio sent to whisper, show the raw whisper and llama requests and responses (`{ }`, including both attempts of a recheck), and show recording details (`ⓘ`: how a phrase was split, the saved file's path).

#### Component tests (`test.html`)

A second page, linked from the dashboard's header, that tests one stage at a time through the dashboard's own endpoints, so a result there is what the dashboard would get:

- **Transcribe:** record, or upload any audio file the browser can decode (converted to the dashboard's WAV format in the page); shows the text, language, timings and raw response.
- **Translate:** any text, streamed; shows the translation, llama's language tag, time to first token and the raw stream.
- **Speak:** any text in any of the TTS server's voices (a picker from `/api/voices`); shows time to first audio, with Play again and a WAV download.
- "Translate this ↓" and "Speak this ↓" pass a result to the next panel, and sample texts (English, Chinese, mixed) help test a language you don't speak.

### Backend (`src/app.py`)

Serves the pages and runs the pipeline. The HTTP plumbing lives in `microweb.py`, a small Flask-style framework (one file, standard library only), so `app.py` is just the app: config, the engine calls, and its endpoints as decorated functions:

| Endpoint                       | Purpose                                                       |
|--------------------------------|---------------------------------------------------------------|
| `GET /api/config`              | Settings the page needs.                                      |
| `GET /api/health`              | The three servers' status (`ok`, `loading`, `down`), checked in parallel (2 s timeout each). |
| `POST /api/recordings`         | Save a WAV and transcribe it with whisper. Returns the text, language, confidence, timing and the raw exchange, and `recheck: true` when the language is neither of the two configured. |
| `POST /api/recordings/recheck` | Transcribe a saved recording again, forced to each of the two languages; return the more confident result. |
| `POST /api/translate`          | Translate text with llama (system prompt from the config, thinking off), streamed as server-sent events. |
| `POST /api/speak`              | Speak text with the TTS server, streamed as raw PCM: in a given voice, else the configured voice for its language (`target`: `language1`/`language2`), else the default voice. |
| `GET /api/voices`              | The TTS server's voices and the configured default (for the test page). |

The two streaming endpoints relay the engine's response unchanged, wrapped as little as possible:

- **`/api/translate`** sends one event of its own first (`event: request`, the exact request sent to llama, for the raw viewer), then llama's `stream: true` response byte for byte (OpenAI-style `data: {...}` chunks ending with `data: [DONE]`). If relaying fails midway, an `event: error` follows. The page assembles the text from the chunks' deltas and keeps every event for the raw viewer.
- **`/api/speak`** returns the TTS server's `response_format: "pcm"` audio as is, labelled `audio/pcm; rate=24000; channels=1` so the page knows the format.

Both connect to the engine before the response starts, so an unreachable engine is a plain 502 JSON error rather than a broken stream. Each logs its result when the stream ends.

### Engines

Stock whisper-server and llama-server binaries from the projects' GitHub releases, run as separate processes. whisper auto-detects the language, so mixed-language speech works.

#### Text-to-speech: Kokoro on sherpa-onnx (`sherpa_server_win.py`)

[Kokoro](https://huggingface.co/hexgrad/Kokoro-82M-v1.1-zh) v1.1 is an 82M-parameter model built on StyleTTS 2 (without its diffusion stage), with 103 voices: 3 English, 100 Chinese. It runs on [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx), a speech toolkit on ONNX Runtime that ships prebuilt libraries but no TTS server, so `sherpa_server_win.py` (ours, in `src/`) is that server:

- **Binding:** it loads `sherpa-onnx-c-api.dll` with `ctypes` (standard library only) and mirrors the C structures field for field, so the sherpa-onnx release is pinned (v1.13.8); upgrading means checking `c-api.h` for changes. Windows only for now (other systems would load the `.so`/`.dylib`).
- **API:** OpenAI-style `POST /v1/audio/speech` (`"voice"` by name, e.g. `zf_001`, or speaker id; `"speed"`; `pcm` streamed or `wav`), `GET /v1/audio/voices`, `GET /health`. The dashboard uses it as it would any OpenAI-style TTS server. Speech streams a sentence at a time: first audio after about 0.2–0.3 s, about 5.5× real time end to end.
- **CPU, no GPU memory:** fp32 Kokoro runs about 3× real time on one thread and 8× on eight (Ryzen 7 9800X3D); on the GPU, 26×. The int8 model is smaller but much slower (about 1.5× on CPU, 1.3× on GPU): its quantized convolutions miss ONNX Runtime's fast paths, and `DynamicQuantizeLinear` converts data before nearly every layer. Small enough for a laptop's CPU, which is why the 8 GB and 6 GB recipes use it.
- **Voices and languages:** each voice speaks both languages, but well only in its own: a Chinese voice (`zf_…`, `zm_…`) reads English with a strong accent, and the English ones (`af_maple`, `af_sol`, `bf_vale`) the reverse. Kokoro's voices are fixed style vectors (its encoder wasn't released), so it can't clone.
- **Also built in, not used by the dashboard yet:** speech-to-text with SenseVoice (`POST /inference`, whisper-server compatible; int8 about 90× real time on CPU and under 400 MB of RAM, accurate Chinese, English weaker than whisper large-v3) and voice activity detection with Silero (`POST /vad`: a clip in, speech segments out, Silero's parameter names). Each engine loads only if its model is given. The recommended model files and URLs are in the script's docstring.

#### Text-to-speech: Qwen3-TTS (qwentts.cpp)

`tts-server` from qwentts.cpp (Qwen3-TTS in C++/ggml), as built by the public release workflow in the Zyeine fork (the upstream project publishes no binaries), with the 1.7B (or 0.6B) "base" talker on Vulkan. Its strength is **voice cloning** from a reference recording's speaker embedding (no transcript needed), and because the model learned speaker and language separately, a cloned voice sounds natural in both languages: Beth, cloned from an English LibriVox reading, speaks fluent Chinese. Voices are WAV files in `voices/` (attribution in `voices/README.md`), registered by `run_servers.py` every time the server starts, since it keeps them in memory only. About 5.5× real time on the GPU (4 GB of GPU memory for the 1.7B), first audio after 0.03–0.05 s. It starts before llama-server, which sizes itself to the GPU memory left over. The 24 GB recipe uses it.

## Flow

- **Manual mode:** Start/Stop records one clip. The page sends it to `/api/recordings`, shows the text when whisper returns, then sends the text to `/api/translate` and shows the translation as it streams in. Recording can continue while earlier rows are processing.
- **Continuous Audio Mode** (called *auto-split* in code and config; on by default): Start keeps the microphone open, and a voice activity detector in the page splits the audio at pauses. Each phrase goes through the same pipeline as its own row.
- **Language checks** (after the fact): speech is expected in one of the two `[languages]`, but quiet or unclear speech can come back as a third (in testing, quiet Chinese came back as Russian, about 1 phrase in 100). Two independent checks catch it, and both lead to the same fix:
  1. *whisper's own label:* the detected language isn't one of the two → `/api/recordings` says `recheck`, before translating;
  2. *llama's language tag:* the system prompt asks llama to end each translation with the language(s) of the text it was given, e.g. `[chinese]`; the page hides the tag (the raw viewer keeps it), and a language other than the two triggers the recheck after translating.

  The **recheck** transcribes the saved recording again forced to each of the two languages and keeps the result whisper is more confident in (segment `avg_logprob`, weighted by length; on the Russian clip, forced Chinese scored −0.40 against English −0.60, and whisper's own language scores agreed). Meanwhile the row shows the first text, marked "(whisper heard russian; rechecking…)"; then it swaps in the corrected text and language, notes "re-transcribed as chinese (…)", and translates again if needed. Speech, including automatic playback, waits for the final translation.
- **Speaking:** the ▶ after a translation sends it to `/api/speak` and plays the speech as it streams in. The page also says which of the two languages the translation is in (the other one than the row's), and the server picks the voice: `[tts] voice_language1`/`voice_language2` for it if set (for anything unusual, e.g. a language whisper doesn't know), else the TTS server's `language_voices` by the language's name (Kokoro's: `english = "af_maple"`, `chinese = "zf_001"`, so each language gets a native voice), else `[tts] voice` if set, else the server's `default_voice` (Kokoro `zf_001`, Qwen `beth_thomas`). A server's voices live in its own `[api_servers]` section, so switching engines can't leave another engine's voice names behind. With Automatic Translation Playback on, each finished translation is queued and spoken, in order, **once the speaker's turn has ended**: every running source (microphone, capture) quiet for `[tts] turn_pause_ms` (2 s). That's deliberately longer than the pause that splits phrases (`[auto_split] split_pause_ms`, 250 ms): text appears when the speaker takes a breath, but translations are only voiced when they've finished, so they don't talk over them (even with a captured tab muted, where a 2 s pause still feels natural). If someone starts speaking during playback (sustained speech, not a cough), it stops and that translation goes back to the front of the queue for the next turn's end. At most `max_backlog` (3) wait; beyond that the oldest is dropped, with a note on its row (its ▶ still works). A speaker who never pauses that long is never talked over, so nothing is spoken until they do. Stopping playback (or starting a recording) drops the waiting ones; the ▶ buttons play at once.

## Configuration

Settings come in three layers, each on top of the one before; tables merge key by key, any other value (lists included) is replaced. `src/configuration.py` reads them, for `app.py`, the scripts and the launcher alike. All are read at startup; relative paths are relative to the project folder.

| Layer                    | What it is                                                                    |
|--------------------------|-------------------------------------------------------------------------------|
| `config/defaults.toml`   | Every setting, documented in its comments. Not edited for local changes.     |
| `config_generated.toml`  | Written by the launcher's Generate config: the languages and a recipe's servers and models. Optional, git-ignored. |
| `config_local.toml`      | Your own settings (e.g. host, the Mock button), always on top. Optional, git-ignored. |

Sections:

| Section                 | Covers                                                                   |
|-------------------------|--------------------------------------------------------------------------|
| `[server]`              | Dashboard host and port.                                                 |
| `[launcher]`            | `show_dev_controls` (Mock and Restart app).                               |
| `[audio]`               | Recording storage, sample rate, and the browser's microphone processing. |
| `[auto_split]`          | Continuous Audio Mode: initial state and pause-detection tuning.         |
| `[languages]`           | The two languages to translate between (`language1`, `language2`): whisper's language names (see below). |
| `[llama_prompt]`        | The translation prompt (with `{language1}`/`{language2}` and the language-tag instruction), temperature, thinking on/off. |
| `[tts]`                 | Which TTS server the dashboard speaks through (`server`); optional voice overrides (`voice`, and per language `voice_language1`, `voice_language2`); Automatic Translation Playback: its initial state, the turn pause it waits for (`turn_pause_ms`) and its backlog limit (`max_backlog`). |
| `[models.<name>]`       | Each server's model files, keyed by the server's flag (`model` → `--model`, `kokoro` → `--kokoro`); a URL can point into an archive (`url#path`). Saved to `models/<name>/`. |
| `[api_servers.<name>]`  | Per server: `start`, where to download it (`repo`, `release`, `assets`, `ensure_version`), and how to run it (`executable`, or `script` for one of ours; `host`, `port`, `args`); for TTS servers, their voices: `default_voice`, `language_voices` (a voice per language name) and `voices` to register (Qwen). |

### Languages

`config/whisper_languages.json` lists the 100 languages whisper recognizes, in whisper's own order (roughly by training data: English, Chinese, German, Spanish, Russian, …), each with its code and lowercase name, generated by `src/generate_whisper_languages.py` from the installed whisper library (`whisper_lang_str` and `whisper_lang_str_full`; no model needed). whisper accepts a language only as exactly that code or name: `chinese` or `zh`, but not `Mandarin` or `Chinese-simplified` (there's one Chinese; which script it writes is a separate matter). The language checks need to tell whisper the configured languages, so:

- Generate config offers only these languages (shown capitalized, e.g. "Chinese"), and won't save another name.
- `app.py` checks `[languages]` against the list at startup, since `config_local.toml` can set anything: a name whisper doesn't know gets a warning in the banner and log, and turns the language checks off rather than letting them force a wrong transcription.

### Fragments and recipes

- **Fragments** (`config/fragments/<server>_<model>.toml`): one validated server + model pair each, as plain TOML — the server's `[api_servers.<name>]` section with `start = true` and its `[models.<name>]` table; TTS fragments also bring `[tts] server`. Voices don't depend on the model, so they stay in `defaults.toml`, attached to their server (`default_voice`, `language_voices`, and the voices Qwen registers). A fragment only uses tables that belong to its server, so any combination pastes together into valid TOML. Today: whisper large-v3, large-v3-q5_0 and large-v3-turbo-q5_0; llama with Qwen3.8-27B UD-IQ3_XXS, Qwen3.5-9B UD-Q3_K_XL and Qwen3.5-9B UD-IQ3_XXS; Kokoro v1.1; Qwen3-TTS 1.7B and 0.6B base. Each comment gives the download size.
- **Recipes** (`config/recipes/<name>.json`, JSON so they aren't mistaken for config): `description`, `vram_gb` (the card size it's meant for), `fragments` (pasted in order) and `off` (servers given `start = false`). Anything in neither list keeps its default, so a recipe can deliberately leave an extra server running.

| Recipe        | VRAM  | STT                       | Translation             | TTS                        |
|---------------|-------|---------------------------|-------------------------|----------------------------|
| `nvidia-24gb` | 24 GB | whisper large-v3          | Qwen3.8-27B UD-IQ3_XXS  | Qwen3-TTS 1.7B (beth_thomas), GPU |
| `nvidia-8gb`  | 8 GB  | whisper large-v3-q5_0     | Qwen3.5-9B UD-Q3_K_XL   | Kokoro (af_maple / zf_001), CPU |
| `nvidia-6gb`  | 6 GB  | whisper large-v3-turbo-q5_0 | Qwen3.5-9B UD-IQ3_XXS | Kokoro (af_maple / zf_001), CPU |

- **Generate config** (launcher): reads the GPU with `nvidia-smi` (the card with the most memory; a missing driver just means choosing by hand), pre-selects the largest recipe that fits (cards report a little under their nominal size, hence a 0.5 GiB margin), takes the two languages from dropdowns of whisper's languages (starting from the current `config_generated.toml`), and previews the result: `[languages]`, then the fragments, then the `off` sections. Save writes `config_generated.toml`; the next Launch uses it. A recipe whose fragments are missing or don't fit together can't be saved.

## Running

On Windows, double-click **`launcher.pyw`**, the only Python file in the project folder. Its window:

- **Open dashboard** (top): the dashboard in the browser.
- **▶ Launch / ■ Stop:** runs the three steps below, showing their output (the dashboard starts before the servers, so its header shows them coming up); Stop (or closing the window) stops the servers and the dashboard. Each button is faded when it doesn't apply.
- **Generate config:** see [Configuration](#fragments-and-recipes).
- **Kill servers:** lists this project's processes still running (anything run from `api_servers/`, and Python running `src/` scripts), e.g. strays from an earlier run, and kills them after you confirm.
- **Developer controls** (only with `[launcher] show_dev_controls = true`): **Mock**, the dashboard alone with sample data (Launch and Mock each stop the other first, since both use the dashboard's port); **Restart app**, the dashboard alone restarted to pick up changes to it, with the servers left running (for Mock, the mock restarted).

Elsewhere, or from a terminal:

```bash
./launch.sh
```

A development convenience that runs the three steps below in order; each can also be run by hand from the project root. All the logic is in the Python scripts, so `launch.sh` stays bare scaffolding:

```bash
python src/install_api_servers.py               # the servers, if needed
python src/download_models.py                   # their models, if needed
python src/run_servers.py python src/app.py     # start the dashboard, then the servers
```

The dashboard is at <http://localhost:8080/>; Ctrl+C stops everything.

```bash
python src/app.py --mock
```

(or `./mock.sh`): the dashboard alone with sample data, for working on the page without the servers. Translations are streamed in pieces like llama's, and speech is silence of about the right length.

### Servers on another machine

`app.py` reaches each server at its `[api_servers.<name>] host` and `port`, and with `start = false` the scripts neither install, download nor start it. So a laptop can run the dashboard (and Kokoro) while a desktop's GPU runs whisper and llama: on the laptop, `start = false` and the desktop's address for those servers; on the desktop, the servers' `host` set to its network address, so they listen there and its own dashboard still reaches them (on Windows, `0.0.0.0` can be listened on but not connected to). The dashboard then runs on the laptop's localhost, so its microphone works. The servers have no authentication: fine at home, not on shared networks.

`src/local_proxy.py` is the other way round, for testing only (not secure): with the dashboard itself listening on the network (`[server] host = "0.0.0.0"`), it forwards a laptop's `localhost:8080` to the desktop's dashboard, since browsers only allow the microphone on localhost or HTTPS pages.

### Project layout

```text
launcher.pyw          the thing to run (Windows)
launch.sh, mock.sh    the same from a terminal
config/               defaults.toml, fragments/, recipes/
config_generated.toml, config_local.toml   (yours, git-ignored)
voices/               reference recordings for Qwen3-TTS
src/                  all the Python, and static/ (the pages)
api_servers/, models/, logs/, tmp/         (generated, git-ignored)
```

### Scripts

In `src/`, run from the project root; each script supports `--help`.

- **`install_api_servers.py`** — downloads the whisper.cpp, llama.cpp and sherpa-onnx (or qwentts.cpp) builds into `api_servers/<name>/`, unpacking `.zip` or `.tar.*`. With `ensure_version = false` (default) an installed build is kept without contacting GitHub; with `true`, every run makes sure the configured release is installed. A pinned release that differs from the installed one is always installed. Servers with `start = false` are skipped.
- **`download_models.py`** — downloads each started server's models into `models/<name>/`; interrupted downloads resume. Archives (`url#path`) are unpacked and deleted.
- **`run_servers.py`** — starts the servers with `start = true` (in config order): an installed executable, or a project script run with the same Python (`sherpa_server_win.py`; `{server_dir}` in its `args` is its install folder). Starts the given command first (e.g. `python src/app.py`, so the dashboard is up while the servers start), waits until each server answers on `/health`, registers its voices (Qwen's tts-server), and stops the servers when the command exits; a `restart` line on its input (typed, or the launcher's Restart app) restarts just the command; with no command, runs them until Ctrl+C.
- **`generate_whisper_languages.py`** — regenerates `config/whisper_languages.json` from the installed whisper library, reporting languages added or removed (`--dry-run` to only check). Rarely needed: whisper's languages are fixed by the model's vocabulary.
- **`configuration.py`** — reads the three settings layers.
- **`common.py`** — helpers shared by the scripts.

### Logs

- **`logs/app.log`** — one shared log from `app.py` and the scripts: server commands, downloads, voice registration, each recording, transcription, recheck, translation and speech, and errors.
- **`logs/<name>.log`** — each server's own output.

## Milestones

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

- Continuous Audio Mode (see [Flow](#flow)). The detector measures background noise for the first 400 ms, then tracks it continuously (falls fast, rises slowly — more slowly mid-phrase); speech is anything `threshold_db` above it, never below `min_threshold_db`. A phrase starts at the first loud block (with `pre_roll_ms` of earlier audio) and ends after `split_pause_ms` of quiet (250 ms; first 600 ms, shortened so text appears on a breath) (keeping `tail_ms`) or at `max_phrase_s`; phrases with less than `min_speech_ms` of speech are dropped.
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

- `/api/translate` streams (see [Backend](#backend-srcapppy)): llama's `stream: true` response is relayed byte for byte after one `request` event of our own, so the raw viewer shows llama's actual chunks. The page shows the text as it arrives and the time to first token under the translate time.
- microweb: a handler can return `Stream(chunks, content_type)`, which sends each piece as it's produced (no `Content-Length`; the connection closes at the end).

### Milestone 8 — Text-to-speech ✅

- Surveyed current TTS models and servers; chose Qwen3-TTS via qwentts.cpp (good English and Chinese, voice cloning, streaming, a small C++ server). Compared the 0.6B and 1.7B base models on CUDA and Vulkan with a cloned voice; chose the 1.7B on Vulkan (see [Engines](#engines)). Measured first audio after about 0.03–0.05 s at the server; end to end through the dashboard, 3.4 s of Chinese speech took 0.84 s in total.
- tts-server installed, its two model files downloaded, and its voices registered at start by the scripts; the default voice is cloned from Beth Thomas's LibriVox reading (see `voices/README.md`).
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

- `test.html` (see [Dashboard](#component-tests-testhtml)): transcribe, translate and speak one at a time, with a voice picker, so each engine can be checked on its own — e.g. Kokoro's English voices without a Chinese speaker at hand. `/api/speak` takes an optional voice; `/api/voices` lists them.

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

- whisper's language label and llama's language tag, with a recheck that transcribes again forced to each of the two languages and keeps the more confident result; the row is corrected while you watch (see [Flow](#flow)). From the logs: 1 wrong-language transcription (Russian, for quiet Chinese) in 96, which the recheck fixes.
- whisper-server's `verbose_json` turned out to include `language_probabilities` and per-segment `avg_logprob` and `no_speech_prob`; the recheck uses `avg_logprob`.

## Extension points (future)

- **Streaming:**
  - Live provisional text while speaking: re-send the current phrase's audio about once a second, show whisper's result as provisional, and translate only finished phrases. The browser keeps the audio, so the server stays stateless.
- **Continuous Audio Mode:** better voice detection, e.g. keep splitting on silence in the page, then run each clip through Silero VAD (`sherpa_server_win.py`'s `/vad`) before STT to drop or trim non-speech; filtering results with high `no_speech_prob`; passing the previous phrase to whisper as a prompt for continuity.
- **Chinese script:** whisper sometimes writes Traditional characters (always, when a recheck forces Chinese), and it can't easily be made to write Simplified. llama can convert them reliably, e.g. a small pass (or an instruction in the translation prompt) that returns the transcript in Simplified characters before it's shown; a descriptive language name for llama ("Chinese-simplified") alongside whisper's name for the checks would fit the same idea.
- **Configuration:**
  - Recipes for other hardware: no GPU, non-NVIDIA (Vulkan builds), and remote setups (a laptop using a desktop's servers).
  - A recipe with two TTS servers (e.g. one per language) would define `[tts]` twice; it needs a per-language `[tts]` first.
- **Housekeeping:** a retention limit for `tmp/audio/` (it grows by about 115 MB per hour of recorded speech).
- **Speech:**
  - Pause speech detection while a translation plays, so speakers can't feed it back into Continuous Audio Mode.
  - The speaker's own voice, cloned from their recordings (Qwen3-TTS).
  - Start speaking before the translation finishes (sentence by sentence).
  - Speaking rate: Kokoro supports `speed` (passed through by `sherpa_server_win.py`); not a setting yet. (Qwen's tts-server accepts it but ignores it.)
  - `sherpa_server_win.py` on Linux/macOS: load the `.so`/`.dylib` and install those builds.
- **Small devices:** SenseVoice STT (built into `sherpa_server_win.py`) plus Kokoro on the CPU and a small llama model, for a setup without a large GPU.
- Add stages to the pipeline (summarization, glossary/terminology pass).
- **Capture:** both sources now run at once, but their rows look alike: marking each row with its source (you vs. the other side of a meeting) and aligning by source would make two-sided transcripts; file upload as another source.
- Add other backends behind the same STT and translation interfaces (e.g. cloud APIs).
