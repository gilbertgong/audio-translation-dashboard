# Audio Translation Dashboard

A local, self-hosted translator for conversations between two languages—English and Chinese by default.
Translate speech from your microphone, a browser tab or your computer's audio, see the original and
translation side by side, and hear the translation spoken aloud.

Powered by [whisper.cpp](https://github.com/ggml-org/whisper.cpp) for transcription,
[llama.cpp](https://github.com/ggml-org/llama.cpp) with Qwen for translation, and
[Kokoro](https://huggingface.co/hexgrad/Kokoro-82M-v1.1-zh) or Qwen3-TTS for speech.
No cloud API is required; engines can run locally or on another computer you control.

## Features

- Live speech translation with push-to-talk or continuous listening.
- Browser-tab and system-audio capture for videos, meetings and other playback.
- Text translation with copy and spoken-playback controls.
- Automatic spoken translations and separate playback speeds for each language.
- Configurable language pairs, models and local or remote inference servers.
- English and Chinese interface, with mobile-friendly defaults.

## Requirements

- **Python 3.11+**. Engine dependencies are installed separately by the setup tools.
- **A modern browser**; use Chrome or Edge for tab-audio capture.
- **Hardware:** an NVIDIA GPU with at least 6 GB VRAM for Windows setups; Apple silicon for Mac;
  or Linux/NVIDIA with Docker and NVIDIA Container Toolkit. Model choices depend on available memory.
- **Disk space:** approximately 8–10 GB for laptop setups or 20 GB for the 24 GB GPU setup.

## Quick start

Download and extract a [release ZIP](https://github.com/gilbertgong/audio-translation-dashboard/releases),
or clone this repository.

### Windows

1. Double-click `launcher.pyw`.
2. Choose **Generate config**, select your GPU/setup and two languages, then save.
3. Click **Launch**. The first launch downloads the engines and models, which can take a while.
4. Click **Open dashboard**, or visit <http://localhost:8080/>.

### Other platforms

- **Mac:** follow the [Apple silicon installation guide](INSTALL_MAC.md).
- **Linux/NVIDIA:** follow the [Docker service setup guide](INSTALL_LINUX.md).
- **Terminal:** after configuring your setup, run `./scripts/launch.sh`; Ctrl+C stops it.

The Mac and Linux setup guides describe their current testing limitations.

## Basic usage

1. Open **Live translation** and allow microphone access.
2. Hold **Hold to Talk** to record, then release to translate. For continuous listening,
   enable **Continuous Audio Mode** and click **Start Recording**. Space or the backtick key
   also controls recording.
3. Use **Automatic Translation Playback** to hear translations automatically, or click **▶**
   beside a translation to play it yourself.
4. To translate computer audio, use **Capture audio** in a supported desktop browser. Select
   **Mute captured tab** before capture if you want to hear only the translations.

The header's page menu also opens **Text translation** and **Component tests**. Navigating to
another page stops the current page's work; use separate tabs to keep both open.
The language button switches the interface between English and Chinese.

## Configuration

Use the launcher's **Generate config** for language, model and server choices. For additional
settings, add overrides to `config_local.toml` and relaunch. See the commented
[default settings](config/defaults.toml) and [configuration guide](DESIGN.md#configuration).

You can also [run inference servers on another computer](DESIGN.md#servers-on-another-machine).
The launcher's **Check for updates** installs published updates while preserving your settings
and downloaded models.

## Documentation

- [Design and detailed usage](DESIGN.md): dashboard controls, architecture and troubleshooting context.
- [Mac installation](INSTALL_MAC.md) and [Linux/NVIDIA setup](INSTALL_LINUX.md).
- [Voices](voices/README.md): stock voices and reference audio.
- [Header customization](DESIGN.md#header-customization).
- [szaL16 protocol](docs/szaL16.md) and [PCM reference implementations](docs/reference/README.md).
- [Logs and recording retention](DESIGN.md#logs).
- [Updates and release procedures](DESIGN.md#app-updates-and-release-versions).

## License

BSD 2-Clause; see [LICENSE](LICENSE). Kokoro pipeline-derived portions are Apache-2.0;
see [Kokoro license](src/licenses/Kokoro-LICENSE.txt).
