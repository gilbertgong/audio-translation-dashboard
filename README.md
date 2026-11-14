# Audio Translation Dashboard

A local, self-hosted live translator between two languages (English and Chinese by default). It listens to your
microphone, or to the sound of a browser tab or your whole computer, then:

1. transcribes each audio segment with [whisper.cpp](https://github.com/ggml-org/whisper.cpp);
2. translates it with [llama.cpp](https://github.com/ggml-org/llama.cpp) and a Qwen model;
3. shows the original and the translation side by side, with timings;
4. speaks the translation with [Kokoro](https://huggingface.co/hexgrad/Kokoro-82M-v1.1-zh) or Qwen3-TTS.

Everything runs on your own machine; nothing is sent to the cloud.

## Requirements

- Python 3.11 or newer. The app itself needs nothing else; the speech servers' packages are installed into their own
  folder automatically.
- A browser (Chrome or Edge for capturing tab audio).
- An NVIDIA GPU with 6 GB or more (Windows), a Mac with Apple silicon, or Linux/NVIDIA with working
  Docker/NVIDIA Container Toolkit and models sized for the GPU. Kokoro runs on the CPU.
- Disk space for the servers and models: about 8–10 GB for the laptop setups, about 20 GB for the 24 GB setup.

## Run it

**Windows:** double-click `launcher.pyw`.

1. **Generate config:** choose a GPU (when more than one is detected), a suggested setup, and the two languages. Save refreshes the launcher settings and closes the config window.
2. **▶ Launch:** downloads the servers and models the first time (this takes a while), then starts everything.
3. **Open dashboard:** opens <http://localhost:8080/>.

**Mac:** follow [INSTALL_MAC.md](INSTALL_MAC.md).

**Linux/NVIDIA Docker:** see [INSTALL_LINUX.md](INSTALL_LINUX.md) for the service supervisor and
local execution example. This extracted path still needs target-machine verification.

**From a terminal:** `./scripts/launch.sh` does the same as Launch. Ctrl+C stops everything.

## Use it

- The **中文 / EN** button in the upper right switches to the other interface language, remembering
  your English/Chinese choice in this browser. Initially it follows your preferred browser language.
  Chinese uses Traditional for a Traditional Chinese browser preference, Simplified otherwise (including
  when switching from an English browser). The button still offers only English and Chinese.
  This changes interface text only,
  independently of the languages being translated. The conversation areas ask browser automatic
  translation to leave their bilingual content intact.
- Whisper identifies the spoken language; translations explicitly target the other configured language.
- The **Coordinator** light shows whether the application backend is reachable, independently of the
  three inference-engine lights, using the same health response. Even if all engines report down,
  a successful response means Coordinator is alive. Live transcription and translation wait when
  Coordinator is unavailable.
  Fetch network failures and HTTP 502/503/504 responses get up to three attempts. Failures while
  subsequently reading JSON or translation streams use existing error handling and are not replayed.
  There is no manual Re-fetch button in this version.
- The header's page dropdown opens **Live translation**, **Text translation**, or **Component tests**.
  These are separate pages: navigating away stops the current page's recording, requests and playback;
  its conversation and drafts are not saved. Open another page in a separate tab to keep Live running.
  All three pages share the branding, language button and API-server status bar.
- **Text translation** in the header opens a chat page for typing or pasting text and choosing a
  language direction. Enter sends; Shift+Enter adds a line. Each translation appears below your message,
  with Copy and Listen controls.
- **Start** (or Space) and talk. With Continuous Audio Mode on, each pause starts a new row.
- **▶** after a translation speaks it; the adjacent copy icon copies the translation. With Automatic Translation Playback on, translations are spoken when the
  speaker pauses.
- **Capture audio** (the left sidebar) translates a browser tab or your whole system instead of the microphone. Tick
  **Mute captured tab** first to hear only the translations.
- The header's **Component tests** page checks each stage on its own.

## Header customization

The three application pages use the same browser-side header. Its top row is two-thirds branding
and one-third header controls; the status bar below contains the page dropdown and API-server lights.
`src/static/header-custom.js` is a shipped no-op extension file. A deployment can replace it to render
or extend any of those three sections. See [Header customization](DESIGN.md#header-customization)
for the renderer contract and an example. No server-side rendering or build step is involved.

## Settings

The launcher's Generate config covers the usual choices. For anything else, put your own settings in
`config_local.toml` in the project folder; `config/defaults.toml` lists every setting with comments.
Launch rereads the config files, so edits are picked up on the next launch without restarting the launcher.

Tick **Advanced mode** to choose individual local fragments for transcription, translation and
speech, starting from the selected recipe. Speech groups Kokoro and Qwen together. Each selector
also offers **Off** and remote backends with separate host/IP and port fields. Remote choices use
the existing backend defaults for voices and other settings; `config_local.toml` can override them.
Reopening the generator restores a previously generated advanced setup immediately, including
fragment selections, remote endpoints, languages and the saved GPU selection when available.
The config preview colors TOML comments, section headings, strings, numbers and booleans.
Advanced mode also shows each selection's model file size and a rough local VRAM
estimate: model size plus 10% for llama with context up to 2048, or 20% otherwise. CPU Kokoro
uses no VRAM; remote backends also contribute nothing to the local VRAM total.
Sizes use decimal GB and exclude voices/support files. These are guides, not capacity checks.
If a fragment has no size comment, already-downloaded model files supply the size when all
required files are present; otherwise the estimate stays unavailable. No downloads are triggered.

By default, session recordings use temporary files that are automatically deleted after transcription
or any recheck. Application logs keep activity and diagnostics but omit transcript, translation and
spoken-text fields. API-server logs are unchanged. To restore saved recordings and application
content logging, add this to `config_local.toml` and restart the app:

```toml
[diagnostics]
save_session_data = true
```

Kokoro v1.0 is available as `config/fragments/kokoro_v1.0.toml`. Select `kokoro_v1.0` under
Speech in Advanced mode, or replace `kokoro_v1.1` in a recipe's fragment list and regenerate. It
uses the same ONNX engine with the original package's phonemizer and stock voices; existing
recipes remain on v1.1-zh with its stock English/Chinese voices.

Qwen uses the official project’s hosted sample as its default reference voice (`qwen_demo`).
HTTP(S) voice references are cached locally on first launch; local WAV paths still work. Kokoro
uses bundled stock voices. No custom voice recordings or voicepacks are shipped.

## Use a remote desktop

On the laptop, open **Generate config**, choose **remote-nvidia-24gb**, and enter the desktop's IP
address or hostname in **Remote host** (default `127.0.0.1`). Save, then Launch. The dashboard runs
locally and connects to the desktop's Whisper, llama and Qwen3-TTS servers on ports 8081, 8082 and
8085. No local engine installation, model downloads or server startup are needed.

Run the normal **nvidia-24gb** setup on the desktop, with those servers listening on its LAN address
or `0.0.0.0`. On the laptop, use the actual desktop address, not `0.0.0.0`. The ports remain editable
in the config, and `config_local.toml` still takes priority over generated settings.

## Playback

The **Playback** panel, opposite Capture audio, has restart, pause/resume, stop/clear queue and
next-clip controls, a seek bar with elapsed/total clip time, an output level meter and queue status. Separate language sliders adjust live
translation speed from 0.75× to 1.5× and remember your choices. A browser-side waveform-matching
prototype preserves pitch while streaming PCM; at 1× audio passes through unchanged. Voice quality
at other speeds is experimental and may have audible artifacts.

## App updates

The launcher shows separate app and launcher versions. **Check for updates** reads the published
`app.version` file and offers an update when either version is newer. After confirmation it
stages the ZIP, stops this launcher's app and local servers, and installs the app and launcher files.
The current launcher stays open during installation.

User files (`config_local.toml`, `config_generated.toml`), models, voices, installed engines and logs
are kept. App code, shipped defaults/recipes, documentation and launch scripts are updated. A newer
installed `launcherVersion` prompts for an optional restart: **OK** performs the normal Restart launcher
action, with no session restoration; **Cancel** continues running and resumes the previous app/server
run. If no launcher restart is needed, that run resumes automatically. The small version display at
the upper right shows when a restart is still needed.

Checks require the master version file and the matching release ZIP to be published. This feature
works with ZIP installations and does not require Git.

### Publishing a release

Commit your code changes first, then run from Git Bash or another Bash terminal:

```bash
bash scripts/release.sh 0.4.2
```

Requires Git, authenticated GitHub CLI (`gh auth login`), and Python (`PYTHON=python3` if needed).
The script updates `app.version`, commits release metadata, creates an annotated version tag,
pushes the release refs, verifies them and creates a GitHub release with generated notes.
`launcherVersion` changes only when the launcher or its three imported project modules differ
from its recorded tag. Code changes must already be committed; pending changes are not included.

Follow the branch and remote conventions in `AI/PRIVATE.md` when present. The published master
version URL must receive the release metadata. Inspect any reported partial completion before
retrying a failed release.

## More

Pipeline terminology: a **session** is one browser tab; a **segment** is one audio or typed input it
submits; a **request** is one model task; an **attempt** is one backend execution of that task.
These definitions don't add tracking or telemetry. See [DESIGN.md](DESIGN.md#pipeline-terminology)
for details and the reserved meaning of app-side chunks.

- [DESIGN.md](DESIGN.md): how it works, in detail: architecture, configuration, scripts, running servers on another
  machine.
- [voices/README.md](voices/README.md): stock voices, URL reference audio and local test fixtures.

## License

BSD 2-Clause; see [LICENSE](LICENSE). Kokoro pipeline-derived portions are Apache-2.0;
see [Kokoro license](src/licenses/Kokoro-LICENSE.txt).
