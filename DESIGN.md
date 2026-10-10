# Audio Translation Dashboard — Design

## Purpose

A local, self-hosted tool that captures speech from the microphone, transcribes it, translates it, shows the original and translation side by side (with timing and debugging details), and speaks the translation aloud. It uses whisper.cpp, llama.cpp and qwentts.cpp as external services, and includes helpers that download and start them, including their models.

## Approach

- The core application logic of voice translation is isolated from ancillary configuration and model handling, which are handled instead by a configuration file (`config.toml`) and supporting scripts (`scripts/`).
- The configuration is read at startup and fixed while running, so there's no configuration UI and no wiring for settings to flow back into behavior. This keeps the core app far simpler and easier to change and extend.
- It also lets the UI stay clean and focused on voice translation itself: the page only has controls you'd change while using it.

## Design principles

- **Simple first:** one page, one linear pipeline.
- **Use existing engines** instead of reimplementing them: whisper.cpp for speech-to-text (STT), llama.cpp for translation, qwentts.cpp for text-to-speech (TTS).
- **Loose coupling:** each engine is its own local server, reached over HTTP. Swapping a model, engine or machine is a config change.
- **Pipeline of small stages:** Capture → Transcribe → Translate → Display → Speak, so new stages can be inserted later.
- **Stream where it helps:** translations appear token by token and speech starts playing while it's still being generated. Engine responses are relayed unchanged, so what the page shows (and the raw viewers) is exactly what the engine sent.
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
                           |  whisper-server     |  |  llama-server       |  |  tts-server         |
                           |  speech-to-text     |  |  translation        |  |  text-to-speech     |
                           |  (port 8081)        |  |  (port 8082)        |  |  (port 8083)        |
                           +---------------------+  +---------------------+  +---------------------+
```

### Dashboard (`static/`)

- **Capture:** microphone → software gain → AudioWorklet. The page builds 16 kHz mono WAV itself, so whisper gets lossless audio in its native format with no ffmpeg.
- **Translation window:** one row per recording. The left column (Conversation) shows the original and its translation, aligned left or right by language; the translation fills in as llama generates it, and gets a ▶ button to speak it once complete. The right column (Activity) shows timings — audio length, transcribe time, translate time (with time to first token, TTFT, under it), and time until speech starts — plus errors. The Activity column is collapsible.
- **Controls:** Start/Stop (or Space), the Continuous Audio Mode and Automatic Translation Playback switches, a gain slider, and a status line and level meter aligned under the Activity column.
- **Speech playback:** the speech arrives as raw 16-bit PCM; each piece is converted and scheduled with Web Audio right after the previous one (with a 0.1 s head start against network jitter), so playback starts as soon as the first audio arrives. Only one thing plays at a time: the ▶ buttons stop whatever is playing, and starting a recording stops playback (speakers would otherwise be recorded).
- **Debugging aids:** each row can play back the exact audio sent to whisper, show the raw whisper and llama requests and responses (`{ }`), and show recording details (`ⓘ`: how a phrase was split, the saved file's path).

### Backend (`app.py`)

Serves the page and runs the pipeline. The HTTP plumbing lives in `microweb.py`, a small Flask-style framework (one file, standard library only), so `app.py` is just the app: config, the engine calls, and its endpoints as decorated functions:

| Endpoint               | Purpose                                                               |
|------------------------|-----------------------------------------------------------------------|
| `GET /api/config`      | Settings the page needs.                                              |
| `POST /api/recordings` | Save a WAV and transcribe it with whisper. Returns the text, timing and the raw whisper exchange. |
| `POST /api/translate`  | Translate text with llama (system prompt from the config, thinking off), streamed as server-sent events. |
| `POST /api/speak`      | Speak text with tts-server in the configured voice, streamed as raw PCM. |

The two streaming endpoints relay the engine's response unchanged, wrapped as little as possible:

- **`/api/translate`** sends one event of its own first (`event: request`, the exact request sent to llama, for the raw viewer), then llama's `stream: true` response byte for byte (OpenAI-style `data: {...}` chunks ending with `data: [DONE]`). If relaying fails midway, an `event: error` follows. The page assembles the text from the chunks' deltas and keeps every event for the raw viewer.
- **`/api/speak`** returns tts-server's `response_format: "pcm"` audio as is, labelled `audio/pcm; rate=24000; channels=1` so the page knows the format.

Both connect to the engine before the response starts, so an unreachable engine is a plain 502 JSON error rather than a broken stream. Each logs its result when the stream ends.

### Engines

Stock whisper-server and llama-server binaries from the projects' GitHub releases, run as separate processes. whisper auto-detects the language, so mixed-language speech works.

For TTS, `tts-server` from qwentts.cpp (Qwen3-TTS in C++/ggml), as built by the public release workflow in the Zyeine fork (the upstream project publishes no binaries). It speaks OpenAI-style `POST /v1/audio/speech` and detects the language per text, so one voice speaks both English and Chinese.

- **Model:** the 1.7B "base" talker plus its codec (two GGUF files), Vulkan build. The Vulkan build is small, needs no CUDA runtime, and was only slightly slower than CUDA in tests (about 5.5× vs 7× real time for the 1.7B; the 0.6B was about 6× vs 8×).
- **Voice cloning:** the base model clones a voice from a reference recording, using its speaker embedding (timbre only, so no transcript is needed). Voices are WAV files in `voices/` (with attribution in `voices/README.md`); tts-server keeps registered voices in memory only, so `run_servers.py` registers them every time it starts the server (about 2 s for a 30 s sample).
- **Order:** tts-server starts before llama-server, because llama-server sizes itself to the GPU memory left over.

## Flow

- **Manual mode:** Start/Stop records one clip. The page sends it to `/api/recordings`, shows the text when whisper returns, then sends the text to `/api/translate` and shows the translation as it streams in. Recording can continue while earlier rows are processing.
- **Continuous Audio Mode** (called *auto-split* in code and config; on by default): Start keeps the microphone open, and a voice activity detector in the page splits the audio at pauses. Each phrase goes through the same pipeline as its own row.
- **Speaking:** the ▶ after a translation sends it to `/api/speak` and plays the speech as it streams in. With Automatic Translation Playback on, each finished translation is spoken by itself, in order: one that finishes while something is playing waits its turn, and stopping playback (or starting a recording) drops the waiting ones.

## Configuration

`config.toml`, read at startup; each setting is documented in the file's comments.

| Section          | Covers                                                                    |
|------------------|---------------------------------------------------------------------------|
| `[server]`       | Dashboard host and port.                                                  |
| `[audio]`        | Recording storage, sample rate, and the browser's microphone processing.  |
| `[auto_split]`   | Continuous Audio Mode: initial state and pause-detection tuning.          |
| `[llama_prompt]` | The two languages, the translation prompt, temperature, thinking on/off.  |
| `[models]`       | The whisper and llama models, with suggestions by GPU memory.             |
| `[api_servers]`  | Per server: whether to start it, which build to download and whether to keep it current (`ensure_version`), and how to run it. |

## Running

```bash
./launch.sh
```

A development convenience that runs the three steps below in order; each can also be run by hand from the project root. All the logic is in the Python scripts, so `launch.sh` stays bare scaffolding:

```bash
python scripts/install_api_servers.py           # the servers, if needed
python scripts/download_models.py               # their models, if needed
python scripts/run_servers.py python app.py     # start the servers, then the dashboard
```

The dashboard is at <http://localhost:8080/>; Ctrl+C stops everything.

```bash
python app.py --mock
```

The dashboard alone with sample data, for working on the page without the servers. Translations are streamed in pieces like llama's, and speech is silence of about the right length.

### Scripts

In `scripts/`, run from the project root; each supports `--help`.

- **`install_api_servers.py`** — downloads the whisper.cpp, llama.cpp and qwentts.cpp binaries into `api_servers/`. With `ensure_version = false` (default) an installed build is kept without contacting GitHub; with `true`, every run makes sure the configured release is installed. Servers with `start = false` are skipped.
- **`download_models.py`** — downloads the models into `models/`; interrupted downloads resume. A model is one file, or several passed under their own flags (the TTS talker and codec: `--model`, `--codec`). Skips models for servers with `start = false`.
- **`run_servers.py`** — starts the servers with `start = true` (in config order), waits until each answers on `/health`, registers its voices (tts-server), runs the given command (e.g. `python app.py`) and stops the servers when it exits; with no command, runs them until Ctrl+C.
- **`common.py`** — helpers shared by the scripts.

### Logs

- **`logs/app.log`** — one shared log from `app.py` and the scripts: server commands, downloads, voice registration, each recording, transcription, translation and speech, and errors.
- **`logs/<name>.log`** — each server's own output.

Generated, git-ignored directories: `api_servers/`, `models/`, `logs/`, `tmp/`.

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

- Continuous Audio Mode (see [Flow](#flow)). The detector measures background noise for the first 400 ms, then tracks it continuously (falls fast, rises slowly — more slowly mid-phrase); speech is anything `threshold_db` above it, never below `min_threshold_db`. A phrase starts at the first loud block (with `pre_roll_ms` of earlier audio) and ends after `pause_ms` of quiet (keeping `tail_ms`) or at `max_phrase_s`; phrases with less than `min_speech_ms` of speech are dropped.
- **Known limitation:** a perfectly steady sound longer than about 9 s is absorbed into the background estimate and ends the phrase (deliberate, so constant noise can't hold it open).

### Milestone 5 — Dashboard layout and debugging aids ✅

- Conversation/activity grid with left/right alignment by language; timings as the main activity information; collapsible Activity column.
- Controls panel aligned with the window; level meter; gain slider; Space shortcut.
- Play buttons and raw whisper/llama request/response viewers.
- Browser microphone processing in the config, with automatic gain control off by default: Chrome's AGC restarts every time the microphone opens and can swing the level (including the Windows microphone level) during the first seconds, which mangled short recordings on a laptop's built-in mic.

### Milestone 6 — microweb ✅

- `microweb.py`: Flask-style, not Flask-compatible. `@app.get`/`@app.post` routes with exact paths; handlers take a request (`method`, `path`, `query`, `headers`, `client`, `body()`, `json()`) and return a dict/list (JSON), str, bytes, `None` or `(body, status)`; `HTTPError(status, message, **extra)` becomes a JSON error. Static files from `static/` with explicit content types and `no-cache`; no directory listings.
- Request bodies need a `Content-Length` (411 otherwise: browsers always send one, and it lets oversized bodies be rejected before reading) and are limited to 1 MB unless a handler allows more (recordings use `max_upload_mb`).
- Wrong method → 405 with `Allow`; unexpected exceptions → 500 with the traceback logged. `app.handle()` runs a request without a network connection, for tests.
- On Windows, Python's HTTP server would let a second server bind a port already in use; microweb disables that, so a taken port gives a clear error.
- Handlers use module-level state in `app.py` (`config`, `mock`), set in `main()`.

### Milestone 7 — Streaming translation ✅

- `/api/translate` streams (see [Backend](#backend-apppy)): llama's `stream: true` response is relayed byte for byte after one `request` event of our own, so the raw viewer shows llama's actual chunks. The page shows the text as it arrives and the time to first token under the translate time.
- microweb: a handler can return `Stream(chunks, content_type)`, which sends each piece as it's produced (no `Content-Length`; the connection closes at the end).

### Milestone 8 — Text-to-speech ✅

- Surveyed current TTS models and servers; chose Qwen3-TTS via qwentts.cpp (good English and Chinese, voice cloning, streaming, a small C++ server). Compared the 0.6B and 1.7B base models on CUDA and Vulkan with a cloned voice; chose the 1.7B on Vulkan (see [Engines](#engines)). Measured first audio after about 0.03–0.05 s at the server; end to end through the dashboard, 3.4 s of Chinese speech took 0.84 s in total.
- tts-server installed, its two model files downloaded, and its voices registered at start by the scripts; the default voice is cloned from Beth Thomas's LibriVox reading (see `voices/README.md`).
- `/api/speak`, the ▶ button after each translation, streamed playback, and Automatic Translation Playback with a queue.
- Dashboard cleanup: Continuous Audio Mode on by default; time to first token on the translate line; split and file details moved behind the `ⓘ` button.
- **Known limitation:** with Continuous Audio Mode and automatic playback both on, the microphone is open while the translation plays; if speakers get past echo cancellation, the speech is picked up as a new phrase. Headphones avoid it.

## Extension points (future)

- **Streaming:**
  - Live provisional text while speaking: re-send the current phrase's audio about once a second, show whisper's result as provisional, and translate only finished phrases. The browser keeps the audio, so the server stays stateless.
- **Continuous Audio Mode:** better voice detection (a small ML model in the browser, or whisper.cpp's VAD); filtering results with high `no_speech_prob`; passing the previous phrase to whisper as a prompt for continuity.
- **External servers:** use whisper or llama instances that are already running (set `start = false` and point `host`/`port` at them); the setting exists, the rest isn't built yet.
- **Housekeeping:** a retention limit for `tmp/audio/` (it grows by about 115 MB per hour of recorded speech).
- **Speech:**
  - Pause speech detection while a translation plays, so speakers can't feed it back into Continuous Audio Mode.
  - A voice per language, or the speaker's own voice cloned from their recordings.
  - Start speaking before the translation finishes (sentence by sentence).
  - Speaking rate: tts-server accepts `speed` but ignores it in this build.
- Add stages to the pipeline (summarization, glossary/terminology pass).
- Add more input sources (file upload, system audio) feeding the same pipeline.
- Add other backends behind the same STT and translation interfaces (e.g. cloud APIs).
