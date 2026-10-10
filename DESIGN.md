# Audio Translation Dashboard — Design

## Purpose

A local, self-hosted tool that captures speech from the microphone, transcribes it, translates it, and shows the original and translation side by side, with timing and debugging details. It uses whisper.cpp and llama.cpp as external services, and includes helpers that download and start them, including their models.

## Design principles

- **Simple first:** one page, one linear pipeline.
- **Use existing engines** instead of reimplementing them: whisper.cpp for speech-to-text (STT), llama.cpp for translation.
- **Loose coupling:** each engine is its own local server, reached over HTTP. Swapping a model, engine or machine is a config change.
- **Pipeline of small stages:** Capture → Transcribe → Translate → Display, so new stages (e.g. TTS) can be inserted later.
- **One config file** (`config.toml`), read at startup. The page only has controls you'd change while using it.
- **No dependencies** beyond Python's standard library (3.11+) and a browser.
- **Easy to extend or modify** by virtue of simplicity.

## Architecture

```text
        microphone
             |
             v
  +---------------------+     WAV audio, text       +---------------------+
  |  Dashboard (page)   | ------------------------> |  app.py (backend)   |
  |  capture, meter,    | <------------------------ |  saves recordings,  |
  |  splitting, display |      text, timings,       |  calls the servers  |
  +---------------------+      raw responses        +----------+----------+
                                                               |
                                                  +------------+------------+
                                                  v                         v
                                       +---------------------+   +---------------------+
                                       |  whisper-server     |   |  llama-server       |
                                       |  speech-to-text     |   |  translation        |
                                       |  (port 8081)        |   |  (port 8082)        |
                                       +---------------------+   +---------------------+
```

### Dashboard (`static/`)

- **Capture:** microphone → software gain → AudioWorklet. The page builds 16 kHz mono WAV itself, so whisper gets lossless audio in its native format with no ffmpeg.
- **Translation window:** one row per recording. The left column (Conversation) shows the original and its translation, aligned left or right by language. The right column (Activity) shows timings — audio length, transcribe and translate time — and debugging details. The Activity column is collapsible.
- **Controls:** Start/Stop (or Space), the Continuous Audio Mode switch, a gain slider, and a status line and level meter aligned under the Activity column.
- **Debugging aids:** each row can play back the exact audio sent to whisper, and show the raw whisper and llama requests and responses.

### Backend (`app.py`)

Serves the page and runs the pipeline. The HTTP plumbing lives in `microweb.py`, a small Flask-style framework (one file, standard library only), so `app.py` is just the app: config, the whisper and llama calls, and its endpoints as decorated functions:

| Endpoint               | Purpose                                                               |
|------------------------|-----------------------------------------------------------------------|
| `GET /api/config`      | Settings the page needs.                                              |
| `POST /api/recordings` | Save a WAV and transcribe it with whisper.                            |
| `POST /api/translate`  | Translate text with llama (system prompt from the config, thinking off). |

Both pipeline endpoints also return their timing and the raw server exchange for the page's debugging views.

### Engines

Stock whisper-server and llama-server binaries from the projects' GitHub releases, run as separate processes. whisper auto-detects the language, so mixed-language speech works.

## Flow

- **Manual mode** (default): Start/Stop records one clip. The page sends it to `/api/recordings`, shows the text when whisper returns, then sends the text to `/api/translate` and shows the translation. Recording can continue while earlier rows are processing.
- **Continuous Audio Mode** (called *auto-split* in code and config): Start keeps the microphone open, and a voice activity detector in the page splits the audio at pauses. Each phrase goes through the same pipeline as its own row.

## Configuration

`config.toml`, read at startup; each setting is documented in the file's comments.

| Section          | Covers                                                                    |
|------------------|---------------------------------------------------------------------------|
| `[server]`       | Dashboard host and port.                                                  |
| `[audio]`        | Recording storage, sample rate, and the browser's microphone processing.  |
| `[auto_split]`   | Continuous Audio Mode: initial state and pause-detection tuning.          |
| `[llama_prompt]` | The two languages, the translation prompt, temperature, thinking on/off.  |
| `[models]`       | The whisper and llama models, with suggestions by GPU memory.             |
| `[api_servers]`  | Where and which server builds to download, and how to run them.          |

## Running

```bash
./launch.sh [--skip-setup]
```

Installs or updates the server binaries and models (unless `--skip-setup`), starts whisper-server and llama-server, waits until both are ready, then runs the dashboard at <http://localhost:8080/>. Ctrl+C stops everything.

```bash
python app.py --mock
```

The dashboard alone with sample data, for working on the page without the servers.

### Scripts

In `scripts/`, run from the project root; each supports `--help`.

- **`install_api_servers.py`** — downloads the whisper.cpp and llama.cpp binaries into `api_servers/`.
- **`download_models.py`** — downloads the models into `models/`; interrupted downloads resume.
- **`server_command.py`** — builds each server's command line for `launch.sh`.
- **`common.py`** — helpers shared by the scripts.

### Logs

- **`logs/app.log`** — one shared log from `app.py`, the scripts and `launch.sh`: server commands, downloads, each recording, transcription and translation, and errors.
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

## Extension points (future)

- **Streaming:**
  - Stream llama's output token by token so translations appear as they are generated.
  - Live provisional text while speaking: re-send the current phrase's audio about once a second, show whisper's result as provisional, and translate only finished phrases. The browser keeps the audio, so the server stays stateless.
- **Continuous Audio Mode:** better voice detection (a small ML model in the browser, or whisper.cpp's VAD); filtering results with high `no_speech_prob`; passing the previous phrase to whisper as a prompt for continuity.
- **Housekeeping:** a retention limit for `tmp/audio/` (it grows by about 115 MB per hour of recorded speech).
- Add stages to the pipeline (TTS, summarization, glossary/terminology pass).
- Add more input sources (file upload, system audio) feeding the same pipeline.
- Add other backends behind the same STT and translation interfaces (e.g. cloud APIs).
