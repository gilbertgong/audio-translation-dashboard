# Audio Translation Dashboard — Design

## Purpose

A local, self-hosted tool that captures speech from the microphone, transcribes it, translates it, shows the original and translation side by side (with timing and debugging details), and speaks the translation aloud. It uses whisper.cpp and llama.cpp as external services, and for speech either Kokoro (on sherpa-onnx, through a small server of its own) or Qwen3-TTS (qwentts.cpp). It includes a launcher and helpers that download and start them all, models included, and that pick a configuration to suit the GPU.

## Approach

### Pipeline terminology

| Term | Meaning |
|---|---|
| Session | One browser tab's connection/activity with the app, shared across its live, typed and component-test views. Microphone and capture recorder state are parts of that session, not separate sessions. |
| Segment | One input submitted by the browser to the app: an audio WAV or a typed text submission. It remains the same segment across transcription, language rechecks, translation and speech generation. |
| Request | One model task, such as initial transcription, transcription forced to one language, translation or speech generation. Each language recheck is a separate request. |
| Attempt | One backend execution of a request. Retrying that same task on another backend is another attempt of the same request. |
| Chunk (reserved) | A subdivision of a browser-submitted segment, potentially produced by future app-side VAD. A request could later identify which chunk it processes. The browser submission remains the parent segment. |

The agreed correlation hierarchy is **session → segment → request → attempt**. These are definitions,
not new runtime objects: there are no correlation IDs, telemetry events, token accounting, session
registration or persistence added by this terminology pass. The current browser splitter creates audio
segments before submission; app-side VAD/chunks and MessageProxy are future work. In that design, the
app creates session/segment/request IDs and MessageProxy creates attempt IDs while retaining the parents.

Existing low-level uses of "chunk" (network reads, PCM buffers and model phoneme chunks) describe
transport or model internals, not the reserved segment subdivision. Whisper's native response
`segments` also describes its own output, not additional browser-submitted segments.

Compatibility names stay as they are: `/api/recordings`, recording file names, response fields,
`max_phrase_s` and `save_session_data`. In recheck raw responses, `stt_raw.attempts` is a legacy name
for the separate forced-language request results; it doesn't imply retries under this terminology.

### Current implementation

- The core application logic of voice translation is isolated from ancillary configuration and model handling, which are handled instead by configuration files (`config/` and the project's `config_*.toml`) and supporting scripts (in `src/`).
- The configuration is read at startup and fixed while running, so there's no configuration UI and no wiring for settings to flow back into behavior. This keeps the core app far simpler and easier to change and extend. Choosing a configuration happens before starting, in the launcher's Generate config window, which writes a file and nothing else.
- It also lets the UI stay clean and focused on voice translation itself: the page only has controls you'd change while using it.

## Design principles

- **Simple first:** one page, one linear pipeline.
- **Use existing engines** instead of reimplementing them: whisper.cpp for speech-to-text (STT), llama.cpp for translation, Kokoro on sherpa-onnx or Qwen3-TTS for text-to-speech (TTS). Where an engine ships a server, we run it; where it ships only a library (sherpa-onnx), a thin server of ours wraps it, with the same kind of HTTP API.
- **Loose coupling:** each engine is its own local server, reached over HTTP. Swapping a model, engine or machine is a config change.
- **Pipeline of small stages:** Capture → Transcribe → Translate → Display → Speak, so new stages can be inserted later.
- **Stream where it helps:** translations appear token by token and speech starts playing while it's still being generated. Engine responses are relayed unchanged, so what the page shows (and the raw viewers) is exactly what the engine sent.
- **Fix after the fact:** checks that can wait (is this really one of our two languages?) run after a result is shown, and correct it in place, rather than delaying every segment.
- **No dependencies** beyond Python's standard library (3.11+) and a browser.

## Architecture

```text
    microphone  speakers
        |          ^
        v          |
+---------------------+       WAV audio, text       +---------------------+
|  Dashboard (page)   | --------------------------> |  app.py (backend)   |
|  capture, meter,    | <-------------------------- |  handles audio,     |
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

The servers are started (and stopped) by `run_servers.py`, which the launcher or `scripts/launch.sh` runs; the dashboard only talks to them over HTTP, so any of them can run on another machine (see [Running](#running)).

### Dashboard (`src/static/`)

- **Capture:** microphone → software gain → AudioWorklet. The page builds 16 kHz mono WAV itself, so whisper gets lossless audio in its native format with no ffmpeg.
- **Translation window:** one row per recording. The left column (Conversation) shows the original and its translation, aligned left or right by language; the translation fills in as llama generates it (the page sends whisper, llama and the TTS server one request at a time each, in order, while the stages overlap: the next segment is transcribed while this one is translated, and the next translation's speech is generated while this one plays), and gets matching inline ▶ playback and copy icon buttons once complete. The right column (Activity) shows timings — audio length, transcribe time, recheck time (when one ran), translate time (with time to first token, TTFT, under it), and time until speech starts — plus notes and errors. The Activity column is collapsible.
- **Capture audio** (a sidebar in the left margin; the main area keeps its size when there's room, and the sidebar moves above it on narrow screens): translates the sound of a browser tab, a window or the whole system instead of the microphone. It uses the browser's screen sharing (`getDisplayMedia`) in reverse: you share with the page, and it listens; the video is requested tiny and unused, and the audio comes unprocessed (no echo cancellation, noise suppression or gain control). A meter per channel, matching the microphone/playback meter styles, with L/R labels and dB values on the same row (M for mono); until one is chosen, the first channel with sound is (left if both start together), and clicking a meter switches. The chosen channel goes, mono, through the same pipeline as Continuous Audio Mode; its rows are noted "captured audio". **Mute captured tab**, a checkbox ticked before capturing (browsers honour `suppressLocalAudioPlayback` only when capture starts; it's locked while capturing), stops a captured tab playing here while it's still captured, so only the translations are heard; automatic playback then talks over it, waiting only for the microphone. The microphone and a capture can run at the same time (each has its own recording session, segment splitting and meters: the one by Start Recording is the microphone's, the sidebar's are the capture's; the status line notes "capturing too"); an experiment, to see how the pipeline copes. The code that made them exclusive is kept, commented out. A `?` explains which share choice gives sound (a tab with tab audio, or the entire screen with system audio; a window gives none).
- **Playback** sits opposite Capture audio, to the right of the conversation. Matching icon buttons restart the current clip, pause/resume, stop and clear all queued audio, or skip to the next queued clip. Pausing holds streamed audio and the queue without interrupting capture or translation; automatic speech interruption does not override a user pause. Status shows buffering/speaking/paused/idle, target language, and queued clip count/duration. A seek bar shows elapsed/total clip time; a trailing + marks a duration still growing as audio streams in. Seeking uses the received audio and keeps a user pause in place. Timeline time follows the original clip duration, independent of playback speed. An analyser measures playback output in dBFS using the microphone meter colors (also for recorded-row playback). Separate sliders named for the configured languages set live translation playback speed from 0.75× to 1.5×, remembered in browser storage. Changes apply at the current audio position, including paused clips. A browser-side WSOLA prototype changes duration while preserving pitch; at 1× samples bypass processing. Queue duration reflects speed. On narrow screens both side panels move above the conversation.
- **Controls:** Start/Stop (or Space), the Continuous Audio Mode and Automatic Translation Playback switches, a gain slider, and a status row and the microphone's level meter aligned under the Activity column. The status row has three independent, unlabeled indicators separated by light dividers (position says which is which; tooltips spell them out): the capture (left) and the mic (middle) each show their source's state (off, calibrating, listening, speech, or recording; for the capture, "no sound yet" until a channel is chosen) and how many of its segments are still being translated; playback (right) shows what is playing, how many translations are queued for a pause, or whether automatic playback is on.
- **Server status:** in the header, a dot per server — Transcribe, Translate, Speak — green (ok), amber (loading: llama-server while it loads its model), red (down), from `/api/health` every 10 s; the tooltip names the server, its address and what the check found.
- **Speech playback:** the speech arrives as raw 16-bit PCM; each piece is converted and scheduled with Web Audio right after the previous one (with a 0.1 s head start against network jitter), so playback starts as soon as the first audio arrives. Only one thing plays at a time: the ▶ buttons stop whatever is playing, and starting a recording stops playback (speakers would otherwise be recorded).
- **Debugging aids:** each row can play back the exact audio sent to whisper, show the raw whisper and llama requests and responses (`{ }`, including both attempts of a recheck), and show recording details (`ⓘ`: how a segment was split, temporary recording status, or the saved path when retention is enabled).

#### Persistent views

`index.html` keeps the live dashboard mounted, with `views.js` switching between live, text and
component-test views. Its header puts the title alone on the first row; the second row has live/text
links on the left, with service status and test links on the right. Navigation uses
`#live`, `#text` and `#tests`. Text and tests are lazy-loaded into same-origin
frames once and then retained. Switching views (including hash Back/Forward) never unloads capture,
queues or playback. Live processing continues while either other view is visible; chat history/drafts
and component-test inputs also survive switching. The live Space shortcut is limited to the live view.
`view-entry.js` redirects old standalone text/test URLs into the shell; embedded pages hide their own
headers. The tests view exposes Smoke test in a separate tab so it does not replace the running app.
Typed translation and speech generation share the live dashboard's per-engine request queues.

Actual page exit cancels live requests/queued jobs, timers, capture and playback, discarding unfinished
audio rather than uploading it. A full-document cache restore reloads those closed resources. These
exit handlers do not run when switching views. Refreshing or closing the app still ends the session.

#### Text translation (`translate.html`)

An everyday text translation page, linked from the dashboard and component tests. `translate.css`
uses the dashboard's light/dark colors, compact controls, borders and panel styling. A scrollable
conversation fills the available height, with a composer anchored below it. Enter sends a message;
Shift+Enter adds a line (IME composition is left alone). Each submission appends one shared `.utterance` row: time/source-language metadata, original text,
and a streamed translation underneath. It uses the live dashboard's exact conversation styles: common
background, padding, separators, source/translation widths and sizes, accent color, and language2
right alignment. Earlier turns
remain visible; they are display history only and are not sent as context to llama.

`translate.js` loads the configured language pair, displaying source and target labels with a swap
button as the sole direction control. Direction changes apply to subsequent messages. Each request captures its own source/target,
including for retry and speech playback. While a translation runs, the direction controls are locked
and the send button becomes Stop; the composer remains available for drafting the next message.
New conversation clears the visible turns and stops playback, preserving the draft and direction.

The toolbar places New conversation on the left and language labels/Swap on the right. Translations
use the dashboard's accent color and larger type, with smaller original text. Completed responses
offer identical compact ▶ playback and copy icons inline after the text, shared with the live dashboard
through `translation-actions.js` and the same CSS sizing. Icons have tooltips and accessible labels; ▶ becomes ■ while
speech is active. Copy falls back to selecting the text when clipboard
access fails. Listen requests `/api/speak` with that response's target key, buffers PCM in memory,
and plays through Web Audio. Clicking Stop on the same response or listening to another stops it.
Failed/stopped translations retain partial output with a status and Retry action; retry updates the
same response without duplicating the user's message. Scrolling follows streamed text only when
already near the bottom. Leaving the page aborts translation and speech. History is held only in
page memory; backend logging still follows the existing app behavior.

#### Component tests (`test.html`)

A second page, linked from the dashboard's header, that tests one stage at a time through the dashboard's own endpoints, so a result there is what the dashboard would get:

- **Transcribe:** record, or upload any audio file the browser can decode (converted to the dashboard's WAV format in the page); shows the text, language, timings and raw response.
- **Translate:** any text, streamed; uses a source-language selector and shows the translation, time to first token and the raw stream.
- **Speak:** any text in any of the TTS server's voices (a picker from `/api/voices`); shows time to first audio, with Play again and a WAV download.
- "Translate this ↓" and "Speak this ↓" pass a result to the next panel, and sample texts (English, Chinese, mixed) help test a language you don't speak.

#### Smoke test (`smoke.html`)

Linked from the component tests: checks that nothing broke, end to end, with the running servers (about a minute). It loads the real dashboard in a frame and replaces the frame's `getUserMedia` and `getDisplayMedia` with streams it plays known speech into (`voices/test_audio/`, generated locally and served by `/api/test-audio`), so the dashboard runs exactly as it would with a person talking. The microphone gets generated English speech; the capture, on its right channel, Chinese, then Japanese (outside the language pair). Then silence, with Automatic Translation Playback on. It checks: the capture chose the channel with sound; each clip split into several segments, each transcribed and translated without errors, in the expected language (the Japanese ones rechecked), without language tags; nothing spoken automatically while anyone was talking, then playback starting after the turn pause; and all three statuses back to off. It's a smoke test, not an accuracy test: the text itself isn't compared.

### Backend (`src/app.py`)

Serves the pages and runs the pipeline. The HTTP plumbing lives in `microweb.py`, a small Flask-style framework (one file, standard library only), so `app.py` is just the app: config, the engine calls, and its endpoints as decorated functions:

| Endpoint                       | Purpose                                                       |
|--------------------------------|---------------------------------------------------------------|
| `GET /api/config`              | Settings the page needs.                                      |
| `GET /api/health`              | The three servers' status (`ok`, `loading`, `down`), checked in parallel (2 s timeout each). |
| `POST /api/recordings`         | Transcribe a WAV with whisper using an automatically deleted temporary file by default. Returns the text, language, confidence, timing and the raw exchange, and `recheck: true` when the language is neither of the two configured. |
| `POST /api/recordings/recheck` | Transcribe the original server-side recording again, forced to each of the two languages; return the more confident result. |
| `POST /api/translate`          | Translate `{text, source}` with llama (source must match the configured pair; target is the other language; prompt rendered per request), streamed as server-sent events. |
| `POST /api/speak`              | Speak text with the TTS server, streamed as raw PCM: in a given voice, else the configured voice for its language (`target`: `language1`/`language2`), else the default voice. |
| `GET /api/voices`              | The TTS server's voices and the configured default (for the test page). |

The two streaming endpoints relay the engine's response unchanged, wrapped as little as possible:

- **`/api/translate`** sends one event of its own first (`event: request`, the exact request sent to llama, for the raw viewer), then llama's `stream: true` response byte for byte (OpenAI-style `data: {...}` chunks ending with `data: [DONE]`). If relaying fails midway, an `event: error` follows. The page assembles the text from the chunks' deltas and keeps every event for the raw viewer.
- **`/api/speak`** returns the TTS server's `response_format: "pcm"` audio as is, labelled `audio/pcm; rate=24000; channels=1` so the page knows the format.

Both connect to the engine before the response starts, so an unreachable engine is a plain 502 JSON error rather than a broken stream. Each logs its result when the stream ends.

### Engines

Stock whisper-server and llama-server binaries from the projects' GitHub releases, run as separate processes. whisper auto-detects the language, so mixed-language speech works.

#### Text-to-speech: Kokoro (`kokoro_server.py`)

[Kokoro](https://huggingface.co/hexgrad/Kokoro-82M-v1.1-zh) v1.1 is an 82M-parameter model built on StyleTTS 2 (without its diffusion stage), with 103 voices: 3 English, 100 Chinese. It was trained for English and Chinese; the v1.0 fragment provides stock Japanese voices. `kokoro_server.py` (ours, in `src/`) serves it: the model as [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) packages it (fp32 `model.onnx`, `voices.bin`, `tokens.txt`), run with ONNX Runtime, behind [misaki](https://github.com/hexgrad/misaki), Kokoro's own text front end:

- **Front end:** Misaki setup and phoneme chunking follow the reference `KPipeline`, pinned for
  equivalence tests at hexgrad/kokoro commit `6d87f4ae7abc2d14dbc4b3ef2e5f19852e861ac2`.
  `--repo-id` identifies the original model package, as in KPipeline: `hexgrad/Kokoro-82M`
  selects v1.0 Chinese G2P (`version=None`); `hexgrad/Kokoro-82M-v1.1-zh` selects v1.1.
  This is model identity, not an independently chosen phonemizer. English uses Misaki with
  the appropriate American/British setting and espeak fallback; Japanese uses `JAG2P`;
  Spanish, French, Hindi, Italian and Brazilian Portuguese use reference `EspeakG2P` codes.
  English waterfall chunking, non-English `.!?` boundaries, limits, truncation, vocabulary
  mapping and voice style indexing follow the reference. Stock/local voices can be blended
  with comma-separated names using the reference's equal-weight average.
- **Deliberate adapter differences:** ONNX Runtime replaces PyTorch; voices and tokens come
  from local sherpa bundles rather than runtime downloads. Our HTTP API streams PCM/WAV
  audio and does not expose reference Python token/word-timestamp result objects. It accepts
  an explicit reference `lang_code` (including reference aliases such as `en-gb`, `ja`, `zh`).
  The dashboard supplies the configured target language for Kokoro. Requests without a code
  retain the existing per-line script selection (kana → Japanese, Han → Chinese, otherwise
  American English) for compatibility. Chinese receives the reference's optional English
  callback for mixed text. The Japanese pyopenjtalk import stand-in remains: the selected
  default Japanese G2P uses cutlet, not pyopenjtalk. These choices do not justify other
  preprocessing changes. Pipeline-derived code is Apache-2.0; see `src/licenses/Kokoro-LICENSE.txt`.
- **v1.0 package:** `config/fragments/kokoro_v1.0.toml` uses the same engine/packages with
  `kokoro-multi-lang-v1_0` model/voices/tokens and its original repo identity. Stock defaults
  are `af_heart`, `zf_xiaoxiao`, `jf_alpha` for English/Chinese/Japanese. `voices = false`
  disables inherited extra voicepacks, which must match the selected model package.
  Existing recipes stay on v1.1. To use v1.0 in a recipe, replace its `kokoro_v1.1` fragment
  name with `kokoro_v1.0` and regenerate configuration.
- **Packages without a virtual environment:** misaki and its English, Chinese and Japanese dependencies (spaCy with its small English model, jieba, pypinyin, fugashi with unidic-lite, …), ONNX Runtime and NumPy, pinned in `[api_servers.kokoro] packages`, are installed by `install_api_servers.py` with `pip install --target api_servers/kokoro/lib` (about 790 MB, 250 MB of it Japanese); the server runs with the project's Python and puts that folder on its import path. misaki's `en` extra is left out on purpose: it brings `spacy-curated-transformers` and with it all of PyTorch, for a transformer model Kokoro's pipeline doesn't use. So is `pyopenjtalk`, which `misaki.ja` imports but only its non-default G2P calls: it has no Windows wheel, and its drop-in `pyopenjtalk-plus` brings about 370 MB more, so the server puts an empty module in its place (phonemes checked identical to having it installed).
- **API:** OpenAI-style `POST /v1/audio/speech` (`"voice"` by name, e.g. `zf_001`, or index; `"speed"`; `pcm` streamed or `wav`), `GET /v1/audio/voices`, `GET /health` (once loaded and warmed up). The dashboard uses it as it would any OpenAI-style TTS server. Speech streams a chunk at a time, about 4–6× real time end to end on four CPU threads (more threads: about 9×). Because chunks follow `KPipeline`, a long English text is one chunk up to 510 phonemes: its first audio comes after the whole chunk (about 2.4 s for two sentences), not after its first sentence.
- **Extra voices:** `[api_servers.kokoro.voices]` (name = a `[510, 1, 256]` voicepack, `.pt` or raw float32) are added to the 103 stock ones, passed at start as `--voice NAME=FILE`.
- **CPU threading:** `--threads 0` (also the default when omitted) lets ONNX Runtime choose its intra-op thread pool automatically, normally one thread per physical CPU core. Explicit positive values remain available. Defaults, the Kokoro recipe fragment and the generated config use 0. Restart Kokoro after changing its startup arguments.
- **CPU, no GPU memory:** fp32 Kokoro runs about 3× real time on one thread and 8× on eight (Ryzen 7 9800X3D); on the GPU, 26×. The int8 model is smaller but much slower (about 1.5× on CPU, 1.3× on GPU): its quantized convolutions miss ONNX Runtime's fast paths, and `DynamicQuantizeLinear` converts data before nearly every layer. Small enough for a laptop's CPU, which is why the 8 GB and 6 GB recipes use it.
- **Voices and languages:** each voice speaks both languages, but well only in its own: a Chinese voice (`zf_…`, `zm_…`) reads English with a strong accent, and the English ones (`af_maple`, `af_sol`, `bf_vale`) the reverse. Kokoro's voices are fixed style vectors (its encoder wasn't released), so it can't clone; custom voicepacks can be supplied locally. The defaults use stock af_maple for English and zf_001 for Chinese; use the v1.0 fragment for a stock Japanese voice. No custom packs are shipped.
- **`sherpa_server_win.py`,** our earlier Kokoro server, binds sherpa-onnx's C library with `ctypes` (standard library only; the C structures mirrored field for field, so the release is pinned, v1.13.8; Windows only). It still works for Kokoro (an older generated config keeps using it), with extra voicepacks only in place of stock ones (sherpa-onnx accepts only the model's own voice count), and has, also built in and not used by the dashboard yet, speech-to-text with SenseVoice (`POST /inference`, whisper-server compatible; int8 about 90× real time on CPU and under 400 MB of RAM, accurate Chinese, English weaker than whisper large-v3) and voice activity detection with Silero (`POST /vad`: a clip in, speech segments out, Silero's parameter names). Each engine loads only if its model is given. The recommended model files and URLs are in the script's docstring.

#### Text-to-speech: Qwen3-TTS (qwentts.cpp)

`tts-server` from qwentts.cpp (Qwen3-TTS in C++/ggml), as built by the public release workflow in the Zyeine fork (the upstream project publishes no binaries), with the 1.7B (or 0.6B) "base" talker on Vulkan. Its strength is **voice cloning** from a reference recording's speaker embedding (no transcript needed), voices can be configured as local WAV paths or HTTP(S) URLs. The default qwen_demo reference is the hosted tokenizer_demo_1.wav linked by the official Qwen3-TTS README. URL references download once into ignored voices/cache/<URL hash>/<filename>; subsequent launches reuse them offline. run_servers.py uploads the resolved WAV bytes as wav_b64 to /v1/audio/voices on each server start, since the C++ server keeps voices in memory only. The Python Qwen package accepts URLs directly, but this configured C++ endpoint does not. No reference audio is bundled; see voices/README.md. About 5.5× real time on the GPU (4 GB of GPU memory for the 1.7B), first audio after 0.03–0.05 s. It starts before llama-server, which sizes itself to the GPU memory left over. The 24 GB recipe uses it.

## Flow

- **Manual mode:** Start/Stop records one clip. The page sends it to `/api/recordings`, shows the text when whisper returns, then sends the text to `/api/translate` and shows the translation as it streams in. Recording can continue while earlier rows are processing.
- **Continuous Audio Mode** (called *auto-split* in code and config; on by default): Start keeps the microphone open, and a voice activity detector in the page splits the audio at pauses. Each segment goes through the same pipeline as its own row.
- **Language direction:** Whisper identifies the source language. If it is outside the configured pair and the transcript is nonempty, the page requests a **recheck** before translating: the server-side recording is transcribed forced to each language, and the result with the higher duration-weighted segment `avg_logprob` wins. The row shows the provisional transcript while waiting, then replaces its text and language. If the recheck fails, the original result is retained; an empty final transcript is not translated. Language rechecks are disabled if a configured language is not in Whisper's language list.
- **Translation prompt:** the page sends the final transcript and Whisper language as `{text, source}`. The backend requires a source matching one of the configured languages (case-insensitive), chooses the other as target, and renders `{language1}`, `{language2}`, `{source}` and `{target}` per request. Unknown or missing source languages produce an explicit 400 error, including after a failed recheck. llama returns translation text only: no language tags, parsing or llama-triggered recheck. Bracketed content is preserved. The component tests select the source explicitly for typed input. Custom prompt overrides must use the new direction placeholders and omit tag instructions.
- **Speaking:** the ▶ after a translation sends it to `/api/speak` and plays the speech as it streams in, once `[tts] playback_buffer_ms` of audio has arrived, or all of it if shorter, so a slow start doesn't stutter (the test page has a Buffer field for trying other amounts). Translations queued for automatic playback have their speech fetched as soon as they're queued, in order, and held in the page, so when one's turn comes its audio is ready (or on its way) and it starts at once; one interrupted by the speaker keeps its audio and heard position, resuming 100 ms before that position after the next turn pause. The cursor follows scheduled audio segments, so initial buffering and gaps do not count as heard speech; already-heard pieces are skipped and the first remaining piece starts at an offset. The backlog counts only the remaining audio. The page also says which of the two languages the translation is in (the other one than the row's), and the server picks the voice: `[tts] voice_language1`/`voice_language2` for it if set (for anything unusual, e.g. a language whisper doesn't know), else the TTS server's `language_voices` by the language's name (Kokoro: stock `af_maple` for English and `zf_001` for Chinese), else `[tts] voice` if set, else the server's `default_voice` (`af_maple` for Kokoro, `qwen_demo` for Qwen). A server's voices live in its own `[api_servers]` section, so switching engines can't leave another engine's voice names behind. With Automatic Translation Playback on, each finished translation is queued and spoken, in order, **once the speaker's turn has ended**: every running source (microphone, capture) quiet for `[tts] turn_pause_ms` (2 s). That's deliberately longer than the pause that splits segments (`[auto_split] split_pause_ms`, 250 ms): text appears when the speaker takes a breath, but translations are only voiced when they've finished, so they don't talk over them (even with a captured tab muted, where a 2 s pause still feels natural). If someone starts speaking during playback (sustained speech, not a cough), it stops and that translation goes back to the front of the queue for the next turn's end. The backlog is limited by time, not count: once the speech waiting (not counting the one playing, and only audio fetched in full) is over `[tts] max_backlog_ms` (30 s), the oldest are dropped, always keeping the newest, each with a note on its row ("over 30 s of speech waiting"; its ▶ still works). A speaker who never pauses that long is never talked over, so nothing is spoken until they do. With a muted captured tab, playback talks over the tab, so a fast speaker keeps the backlog full and playback about 30 s behind (see Extension points: a playback speed control). Stopping playback (or starting a recording) drops the waiting ones; the ▶ buttons play at once.

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
| `[launcher]`            | `show_dev_controls` (Mock, Restart app, the server buttons).             |
| `[audio]`               | Recording storage, sample rate, and the browser's microphone processing. |
| `[auto_split]`          | Continuous Audio Mode: initial state and pause-detection tuning.         |
| `[languages]`           | The two languages to translate between (`language1`, `language2`): whisper's language names (see below). |
| `[llama_prompt]`        | The translation prompt (with `{language1}`/`{language2}` and the language-tag instruction), temperature, thinking on/off. |
| `[tts]`                 | Which TTS server the dashboard speaks through (`server`); optional voice overrides (`voice`, and per language `voice_language1`, `voice_language2`); Automatic Translation Playback: its initial state, the turn pause it waits for (`turn_pause_ms`) and its backlog limit (`max_backlog_ms`: seconds of speech waiting, fetched in full; beyond it the oldest are dropped); audio buffered before speech plays (`playback_buffer_ms`). |
| `[gpu]`                 | `devices`: the GPU(s) the servers may use, by nvidia-smi index, passed as `CUDA_VISIBLE_DEVICES` (with `CUDA_DEVICE_ORDER=PCI_BUS_ID`); Generate config sets it to the card it sized the recipe for. Unset: every GPU. Not for Vulkan builds (Qwen's). |
| `[models.<name>]`       | Each server's model files, keyed by the server's flag (`model` → `--model`, `kokoro` → `--kokoro`); a URL can point into an archive (`url#path`). Saved to `models/<name>/`. |
| `[api_servers.<name>]`  | Per server: `start`, where to download it (`repo`, `release`, `assets`, `ensure_version`), and how to run it (`executable`, or `script` for one of ours; `host`, `port`, `args`); for TTS servers, their voices: `default_voice`, `language_voices` (a voice per language name) and `voices` (Qwen: WAVs to clone, registered after start; Kokoro: extra voicepacks, passed at start with `voice_arg`, each in place of a stock voice since sherpa only takes the model's own voice count); `gpu` and an `env` table override `[gpu]`. |

### Languages

`config/whisper_languages.json` lists the 100 languages whisper recognizes, in whisper's own order (roughly by training data: English, Chinese, German, Spanish, Russian, …), each with its code and lowercase name, generated by `src/tools/generate_whisper_languages.py` from the installed whisper library (`whisper_lang_str` and `whisper_lang_str_full`; no model needed). whisper accepts a language only as exactly that code or name: `chinese` or `zh`, but not `Mandarin` or `Chinese-simplified` (there's one Chinese; which script it writes is a separate matter). The language checks need to tell whisper the configured languages, so:

- Generate config offers only these languages (shown capitalized, e.g. "Chinese"), and won't save another name.
- `app.py` checks `[languages]` against the list at startup, since `config_local.toml` can set anything: a name whisper doesn't know gets a warning in the banner and log, and turns the language checks off rather than letting them force a wrong transcription.

### Fragments and recipes

- **Fragments** (`config/fragments/<server>_<model>.toml`): one validated server + model pair each, as plain TOML — the server's `[api_servers.<name>]` section with `start = true` and its `[models.<name>]` table; TTS fragments also bring `[tts] server`. Default voice settings stay in `defaults.toml`, attached to their server (`default_voice`, `language_voices`, and the voices Qwen registers). A fragment only uses tables that belong to its server, so any combination pastes together into valid TOML. A model fragment can override voices when its package differs. Today: whisper large-v3, large-v3-q5_0 and large-v3-turbo-q5_0; llama with Qwen3.8-27B UD-IQ3_XXS, Qwen3.5-9B UD-Q3_K_XL and UD-IQ3_XXS (used by `scripts/setup_mac.sh`), and Qwen3.5-4B UD-Q4_K_XL and UD-IQ3_XXS (context 2048, for the laptop recipes); Kokoro v1.0 and v1.1; Qwen3-TTS 1.7B and 0.6B base. Each fragment has a model file size comment (decimal GB, including required codecs).
- **Recipes** (`config/recipes/<name>.json`, JSON so they aren't mistaken for config): `description`, `vram_gb` (the card size it's meant for), `fragments` (pasted in order) and `off` (servers given `start = false`). Anything in neither list keeps its default, so a recipe can deliberately leave an extra server running.

| Recipe        | VRAM  | STT                       | Translation             | TTS                        |
|---------------|-------|---------------------------|-------------------------|----------------------------|
| `nvidia-24gb` | 24 GB | whisper large-v3          | Qwen3.8-27B UD-IQ3_XXS  | Qwen3-TTS 1.7B (qwen_demo), GPU |
| `nvidia-8gb`  | 8 GB  | whisper large-v3          | Qwen3.5-4B UD-Q4_K_XL (context 2048) | Kokoro (stock voices), CPU       |
| `remote-nvidia-24gb` | None locally | Desktop Whisper, port 8081 | Desktop llama, port 8082 | Desktop Qwen3-TTS, port 8085 |
| `nvidia-6gb`  | 6 GB  | whisper large-v3-q5_0     | Qwen3.5-4B UD-IQ3_XXS (context 2048) | Kokoro (stock voices), CPU       |

- **Generate config** (launcher): lists NVIDIA GPUs with `nvidia-smi`, showing a GPU dropdown only when more than one is detected (index, name and VRAM). It remembers the previously generated GPU when available, otherwise defaults to the card with the most memory; a missing driver just means choosing a recipe by hand. Selecting a card updates the preview GPU index and pre-selects the largest recipe that fits (cards report a little under their nominal size, hence a 0.5 GiB margin), takes the two languages from dropdowns of whisper's languages (starting from the current `config_generated.toml`), and previews the result: `[languages]`, then the fragments, then the `off` sections. Save writes `config_generated.toml` and immediately refreshes the launcher settings and server buttons, then closes the config window; the next Launch uses it. Existing services keep their current configuration. A recipe whose fragments are missing or don't fit together can't be saved.

The generator's **Advanced mode** replaces the recipe dropdown with independent backend
selectors, initialized from that recipe. Local choices are discovered from single-server,
enabled fragments. Fragments with `[tts] server` identify speech backends, which share one
selector. Other endpoint sections in the shipped defaults get their own selector. Remote choices
are synthesized from backend `[api_servers.<name>]` sections with host/port; no remote fragment
file is needed. Each reveals a separate host/IP (initially `127.0.0.1`) and editable default port.
Remote output contains only `start = false`, host, port and, for speech, `[tts] server`; other
fields retain defaults. Unselected backends receive `start = false`. GPU pinning is included only
when a local fragment is selected. Language selection and Save behavior are shared with recipe mode.
No compatibility, VRAM or endpoint checks are added. The read-only preview uses Tk text tags for
muted gray comments, blue sections, green strings and purple numeric/boolean literals; this lexical
coloring neither parses nor alters the generated text.

On opening the generator, an advanced generated config is recognized by its existing header.
Fragment/remote comment markers restore the selectors; TOML values restore endpoints, languages
and GPU selection. This happens before GPU detection finishes; later GPU suggestions preserve
advanced backend choices. Recipe-generated files keep existing recipe-mode startup behavior.

Advanced choices list local fragments first and remote backends last. Each fragment documents
approximate model weights/codec size using `# Model files: N GB ...` (decimal GB); CPU placement
is marked in that comment. Advanced mode puts file sizes in parentheses in local dropdown choices,
with estimated VRAM to the right. Hovering a CPU model's zero-VRAM label shows `Runs on CPU`.
A separate column shows `Total Estimated VRAM: x.xx GB`, horizontally centered and top aligned.
Hovering any part of this label (also keyboard focus) shows the explanation without a separate
help icon. CPU models, remote and Off contribute no local VRAM. VRAM is model size plus 10%
for llama with an explicit context of 1–2048 tokens,
20% otherwise. It assumes GPU model loading for other local fragments and excludes voices,
support files and other applications. When the size comment is missing, the generator uses the
downloader's existing path mapping (including Whisper aliases and archive members) to sum the
already-downloaded required model files, honoring `models.dir`. Every required file must exist
and be nonempty; `.part` files do not count. This fallback makes no network requests. If any file
is missing/unreadable, size remains unavailable with known subtotals. Comments take precedence;
estimates never gate saving or attempt to check available memory or user model overrides.

### PCM playback time stretching

`src/static/time-stretch.js` implements a small mono WSOLA-style prototype for 0.75–1.5×.
It uses 40 ms frames with 20 ms overlap/hops and a ±10 ms waveform-correlation search. The
nominal input position follows output position times speed, preventing cumulative search drift.
One processor spans all incoming PCM chunks; a short lookahead is held until more input or EOF,
then the tail is flushed to the requested total duration. Matching uses normalized correlation
and linear crossfades. No TTS/server/codec changes or additional packages are required.

`app.js` schedules the processed samples through Web Audio at rate 1, mapping the output timeline
back to nominal source positions for seeking, interruption rewind and backlog. At exactly 1× it
uses the original PCM path. Changing speed restarts processing from the heard source position
using already-received audio; it does not repeat the TTS request. The analyser meters output as
before. The processor keeps its input/tail for the active playback instance and is released on stop.

This is experimental speech processing, not a guarantee of transparent sound: overlaps can
produce artifacts, and the source-position mapping approximates the local waveform-search offset.
Offline signal checks cover pitch, duration, amplitude, silence, short input, arbitrary chunk
boundaries and EOF. Transport checks cover streaming/underflow, pause, seeking, mid-playback speed
changes, interruption/resume, queue controls and the output meter. Real voice quality needs audition.

## Running

On Windows, double-click **`launcher.pyw`**, the only Python file in the project folder. Its window:

- **Open dashboard** (top): the dashboard in the browser.
- **▶ Launch / ■ Stop:** runs the three steps below, showing their output (the dashboard starts before the servers, so its header shows them coming up); Stop (or closing the window) stops the servers and the dashboard. Each button is faded when it doesn't apply.
- **Generate config:** see [Configuration](#fragments-and-recipes).
- **Kill servers:** lists this project's processes still running (anything run from `api_servers/`, and Python running `src/` scripts), e.g. strays from an earlier run, and kills them after you confirm.
- **Developer controls** (only with `[launcher] show_dev_controls = true`): **Mock**, the dashboard alone with sample data (Launch and Mock each stop the other first, since both use the dashboard's port); **Restart app**, the dashboard alone restarted to pick up changes to it, with the servers left running (for Mock, the mock restarted); and a row of **server buttons**, one per server Launch starts (`start = true` in the merged settings), each starting or stopping its server (■ running, ▶ stopped, red if it failed or exited by itself). Before a launch, a server starts on its own, with Launch's steps for just that server (install, download its models, `run_servers.py`, each `--only NAME`), and a later Launch uses it as it is and leaves it running; during a launch, the launch's servers are stopped and started through its `run_servers.py`.

Elsewhere, or from a terminal:

```bash
./scripts/launch.sh
```

A development convenience that runs the three steps below in order; each can also be run by hand from the project root. All the logic is in the Python scripts, so `scripts/launch.sh` stays bare scaffolding:

```bash
python src/install_api_servers.py               # the servers, if needed
python src/download_models.py                   # their models, if needed
python src/run_servers.py python src/app.py     # start the dashboard, then the servers
```

The dashboard is at <http://localhost:8080/>; Ctrl+C stops everything.

```bash
python src/app.py --mock
```

(or `./scripts/mock.sh`): the dashboard alone with sample data, for working on the page without the servers. Translations are streamed in pieces like llama's, and speech is silence of about the right length.

On a Mac, `scripts/setup_mac.sh` comes first: whisper.cpp publishes no macOS server build, so it links Homebrew's `whisper-server` and `llama-server` (Metal) into `api_servers/` with a `VERSION` of `homebrew` (which the installer leaves alone), and writes `config_generated.toml` by the Mac's memory, since Generate config needs nvidia-smi. Speech defaults to Qwen3-TTS: the 1.7B base fragment with the pinned Apple Silicon Metal ZIP instead of the Windows Vulkan asset, with Kokoro disabled. `TTS=kokoro` selects CPU speech instead. Setup installs enabled Qwen and sets its executable permission (ZIP extraction doesn't preserve it). Existing generated configs are kept. Steps and troubleshooting: `INSTALL_MAC.md` (not yet tried on a real Mac).

### App updates and release versions

`app.version` is a JSON file, initially:

```json
{
  "appVersion": "0.1.0",
  "launcherVersion": "0.1.0",
  "directDownloadURL": "https://github.com/gilbertgong/audio-translation-dashboard/archive/refs/tags/v0.1.0.zip",
  "masterVersionFile": "https://raw.githubusercontent.com/gilbertgong/audio-translation-dashboard/main/app.version"
}
```

The local file records the installed app and launcher. The file at `masterVersionFile` describes the
latest published release, including its download URL. Versions are dotted numeric strings, compared
numerically (0.10.0 is newer than 0.9.0). `launcherVersion` identifies the launcher code installed on disk,
never a compatibility gate on updates. The running launcher captures its startup version separately;
the small upper-right label shows App and running Launcher versions on two lines, with a restart-needed
indication when a newer launcher has been installed. An archive without launcher.pyw keeps the prior
installed launcher version.

The launcher's **Check for updates** does its network work in a thread. If a newer app or launcher is available,
it asks whether to update. `src/app_updates.py` downloads into a unique `tmp/app-update-*` folder and
stages the ZIP before stopping anything. GitHub's single wrapper directory and flat release ZIPs are
supported. The archive must include a matching `app.version` and `src/app.py`. Paths cannot escape the
staging/installation folder, and application symlink entries are not installed.

Only files under `src/`, `config/` and `scripts/`, plus launcher.pyw, README.md, DESIGN.md, INSTALL_MAC.md and LICENSE
are replaced. User configs, voices, models, installed engines,
logs, AI notes and Git metadata are excluded. Additional files are not pruned. Existing files being
replaced are backed up in staging; on installation failure they are restored and newly installed files
are removed. If restoration itself fails, staging is retained and its backup path is reported.

The launcher waits for its own app and locally managed server processes to stop before installation;
remote or independently started servers are not stopped. It remains open throughout. If a newer launcher
version was installed, an OK/Cancel dialog offers a restart. OK schedules the existing Restart launcher
action, with no session restoration. Cancel (or an unchanged launcher version) resumes the previous
Launch/Mock mode or standalone servers as before. The label retains the restart-needed indication after
Cancel; another update check can offer the pending restart again. Failed installs report an
error and leave restarting to the user. No configuration validation is added. Imported launcher helper
modules remain loaded until the launcher is restarted; future app releases must preserve their callable
interfaces while older launchers remain in use.

For a release, increment `appVersion` for app changes and `launcherVersion` for launcher changes. Set
`directDownloadURL` to the new release/tag ZIP and include this metadata in that archive. Make the ZIP
available before publishing its master metadata. Keep the master describing a published release,
rather than unreleased development work. No external helper is needed to replace the Python launcher
source; its current process continues with already loaded code until restarted.

`bash scripts/release.sh VERSION` automates release metadata, its commit, an annotated `vVERSION`
tag, pushing and verification of release refs, and `gh release create` with generated notes and
`--verify-tag`. It requires a clean working tree and a newer numeric app version; code changes must
already be committed. It compares launcher.pyw, src/configuration.py, src/common.py and
src/app_updates.py against the recorded launcherVersion tag. Any difference sets launcherVersion
to the new app version; otherwise it is retained. A missing comparison tag stops the release.
Keep that file list aligned with launcher imports, including indirect imports and file moves.
The script preserves masterVersionFile and rewrites the tagged download URL for the release repo.
Branch and remote conventions are documented in `AI/PRIVATE.md` when present. Ensure the published
master URL receives the metadata; inspect reported partial completion before retrying failures.

Offline regression checks: `python -m unittest discover -s tests -v`. They use scratch installations
and mocked network/processes; they never update the working checkout or contact model servers.

### Servers on another machine

Generate config offers `remote-nvidia-24gb` for a laptop connecting to an existing 24 GB desktop.
The recipe's `remote_host` metadata requests a text entry (default `127.0.0.1`); `assemble` fills the
fragment's quoted `{host}` placeholders using an escaped TOML string. Host text is passed through,
without IP/hostname validation. The fragment selects Qwen3-TTS and sets Whisper, llama and Qwen3-TTS
`start = false`, with ports 8081, 8082 and 8085; its `off` list also disables Kokoro. Existing skip
logic handles local installation/download/startup. No new runtime server mode or configuration loader
is needed. Remote recipes are excluded from local GPU suggestions and omit local GPU pinning; the GPU
picker is hidden while a remote recipe is selected. The generated preview keeps hosts/ports explicit.
The desktop runs its normal recipe with reachable listening addresses; no servers are managed remotely.


`app.py` reaches each server at its `[api_servers.<name>] host` and `port`, and with `start = false` the scripts neither install, download nor start it. So a laptop can run the dashboard (and Kokoro) while a desktop's GPU runs whisper and llama: on the laptop, `start = false` and the desktop's address for those servers; on the desktop, the servers' `host` set to its network address, so they listen there and its own dashboard still reaches them (on Windows, `0.0.0.0` can be listened on but not connected to). The dashboard then runs on the laptop's localhost, so its microphone works. The servers have no authentication: fine at home, not on shared networks.

`src/tools/local_proxy.py` is the other way round, for testing only (not secure): with the dashboard itself listening on the network (`[server] host = "0.0.0.0"`), it forwards a laptop's `localhost:8080` to the desktop's dashboard, since browsers only allow the microphone on localhost or HTTPS pages.

### Project layout

```text
launcher.pyw          the thing to run (Windows)
README.md, DESIGN.md  for people: overview, and this document
AI/, AGENTS.md, CLAUDE.md   for AI assistants: plan, history, working rules (start at AI/MEMORY.md)
scripts/launch.sh, scripts/mock.sh    the same from a terminal
scripts/setup_mac.sh, INSTALL_MAC.md   macOS: setup before scripts/launch.sh, and the steps
config/               defaults.toml, fragments/, recipes/
config_generated.toml, config_local.toml   (yours, git-ignored)
voices/               a folder per speaker: Qwen3-TTS reference recordings, Kokoro voicepacks, test audio
src/                  all the Python, static/ (the pages) and tools/ (extras)
api_servers/, models/, logs/, tmp/         (generated, git-ignored)
```

### Scripts

In `src/`, run from the project root; each script supports `--help`.

- **`install_api_servers.py`** — downloads the whisper.cpp, llama.cpp (or qwentts.cpp) builds into `api_servers/<name>/`, unpacking `.zip` or `.tar.*`; for a server that's one of our scripts and lists `packages` (Kokoro), installs them with `pip install --target api_servers/<name>/lib` instead, again when the list or the Python version changes. One installer runs at a time (a lock file in `api_servers/`: a second one, e.g. a server button during a Launch, waits, then finds the work done), and an install replaces the old folder only by renaming it aside first, so a failure (a file in use) leaves the working install untouched. With `ensure_version = false` (default) an installed build is kept without contacting GitHub; with `true`, every run makes sure the configured release is installed. A pinned release that differs from the installed one is always installed. Servers with `start = false` are skipped.
- **`download_models.py`** — downloads each started server's models into `models/<name>/`; interrupted downloads resume. Both it and `install_api_servers.py` take `--only NAME` for one server (the launcher's server buttons). Archives (`url#path`) are unpacked and deleted.
- **`run_servers.py`** — starts the servers with `start = true` (in config order): an installed executable, or a project script run with the same Python (`kokoro_server.py`; `{server_dir}` in its `args` is its install folder). Starts the given command first (e.g. `python src/app.py`, so the dashboard is up while the servers start), waits until each server answers on `/health`, registers its voices (Qwen's tts-server), and stops the servers when the command exits; a `restart` line on its input (typed, or the launcher's Restart app) restarts just the command, and `stop NAME` / `start NAME` stop or start one server (the launcher's server buttons), each server's state reported as `== server NAME: <state>`; `--only NAME` starts just that one; a server already running at its address is used as it is (and left running); with no command, runs them until Ctrl+C.
- **`configuration.py`** — reads the three settings layers.
- **`common.py`** — helpers shared by the scripts.

In `src/tools/`, extras that aren't part of running the app:

- **`generate_whisper_languages.py`** — regenerates `config/whisper_languages.json` from the installed whisper library, reporting languages added or removed (`--dry-run` to only check). Rarely needed: whisper's languages are fixed by the model's vocabulary.
- **`generate_test_audio.py`** — creates ignored `voices/test_audio/` English, Chinese and Japanese fixtures using the running servers. Source defaults to the configured Qwen reference, or a `--source` WAV path/URL; the source language is detected automatically. `--voice` optionally overrides the configured TTS voices. Run explicitly; see `voices/README.md`.
- **`local_proxy.py`** — forwards a laptop's `localhost:8080` to the dashboard on another machine (see above); standalone, copy just this file over.

### Logs

- **`logs/app.log`** — one shared log from `app.py` and the scripts: server commands, downloads, voice registration, each recording, transcription, recheck, translation and speech, and errors.
- **`logs/<name>.log`** — each server's own output (unchanged by session retention settings).

`[diagnostics] save_session_data = false` is the default. Application log entries keep their activity,
timings, language, confidence and diagnostics, with explicit transcript/translation/spoken-text fields
replaced by `[content omitted]`. API-server logs are not modified or filtered. Set the option to `true`
in `config_local.toml` to restore saved WAVs and application content logging; there is no UI toggle.

Session WAVs use `tempfile.TemporaryFile` under `[audio] temp_dir`: Unix uses an unnamed/unlinked file;
Windows deletes it on close. A normal transcription closes it immediately, including on error. If a
recheck is needed, the backend retains its open handle keyed by the existing recording name. The
browser sends the same small JSON filename request as before; it never uploads audio again for a
recheck. The handle stays open through both forced-language requests and closes on completion or
failure. Abandoned rechecks expire after 15 minutes; pending handles also close at process exit.
With retention enabled, the original saved-file recheck behavior is preserved. Existing recordings
and logs are not removed by this setting.

## Known limitations

- **Segment splitting:** a perfectly steady sound longer than about 9 s is absorbed into the background estimate and ends the segment (deliberate, so constant noise can't hold it open).
- **Feedback:** with Continuous Audio Mode and automatic playback both on, the microphone is open while the translation plays; if speakers get past echo cancellation, the speech is picked up as a new segment. Headphones avoid it. (In practice the browser's echo cancellation has handled even a playing video well.)
- **misaki:** English misreads some proper names; Japanese reads a decimal point as a sentence end and leaves Latin letters as they are, and a line of kanji only (東京駅) is read as Chinese.
- **Mac:** `scripts/setup_mac.sh` and `INSTALL_MAC.md` haven't been tried on a real Mac.

The project's history (milestones, measurements and the reasons behind decisions) is in `AI/PROJECT_LOG.md`.

## Extension points (future)

- **Streaming:**
  - Live provisional text while speaking: re-send the current segment's audio about once a second, show whisper's result as provisional, and translate only finished segments. The browser keeps the audio, so the server stays stateless.
- **Continuous Audio Mode:** better voice detection, e.g. keep splitting on silence in the page, then run each clip through Silero VAD (`sherpa_server_win.py`'s `/vad`) before STT to drop or trim non-speech; filtering results with high `no_speech_prob`; passing the previous segment to whisper as a prompt for continuity.
- **Chinese script:** whisper sometimes writes Traditional characters (always, when a recheck forces Chinese), and it can't easily be made to write Simplified. llama can convert them reliably, e.g. a small pass (or an instruction in the translation prompt) that returns the transcript in Simplified characters before it's shown; a descriptive language name for llama ("Chinese-simplified") alongside whisper's name for the checks would fit the same idea.
- **Configuration:**
  - Recipes for other hardware: no GPU, non-NVIDIA (Vulkan builds), and remote setups (a laptop using a desktop's servers).
  - A recipe with two TTS servers (e.g. one per language) would define `[tts]` twice; it needs a per-language `[tts]` first.
- **Housekeeping:** a retention limit for `tmp/audio/` when session retention is explicitly enabled (about 115 MB per hour of recorded speech).
- **Speech:**
  - Pause speech detection while a translation plays, so speakers can't feed it back into Continuous Audio Mode.
  - The speaker's own voice, cloned from their recordings (Qwen3-TTS).
  - Start speaking before the translation finishes (sentence by sentence).
  - Playback speed quality: audition the PCM WSOLA prototype on real speech. Future Opus/media-element playback could use native pitch preservation if streaming latency and transport controls remain satisfactory.
  - `sherpa_server_win.py` on Linux/macOS: load the `.so`/`.dylib` and install those builds.
- **Small devices:** SenseVoice STT (built into `sherpa_server_win.py`) plus Kokoro on the CPU and a small llama model, for a setup without a large GPU.
- Add stages to the pipeline (summarization, glossary/terminology pass).
- **Capture:** both sources now run at once, but their rows look alike: marking each row with its source (you vs. the other side of a meeting) and aligning by source would make two-sided transcripts; file upload as another source.
- Add other backends behind the same STT and translation interfaces (e.g. cloud APIs).
