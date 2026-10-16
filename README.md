# Audio Translation Dashboard

A local, self-hosted live translator between two languages (English and Chinese by default). It listens to your
microphone, or to the sound of a browser tab or your whole computer, then:

1. transcribes each phrase with [whisper.cpp](https://github.com/ggml-org/whisper.cpp);
2. translates it with [llama.cpp](https://github.com/ggml-org/llama.cpp) and a Qwen model;
3. shows the original and the translation side by side, with timings;
4. speaks the translation with [Kokoro](https://huggingface.co/hexgrad/Kokoro-82M-v1.1-zh) or Qwen3-TTS.

Everything runs on your own machine; nothing is sent to the cloud.

## Requirements

- Python 3.11 or newer. The app itself needs nothing else; the speech servers' packages are installed into their own
  folder automatically.
- A browser (Chrome or Edge for capturing tab audio).
- An NVIDIA GPU with 6 GB or more (Windows), or a Mac with Apple silicon. Kokoro runs on the CPU.
- Disk space for the servers and models: about 8–10 GB for the laptop setups, about 20 GB for the 24 GB setup.

## Run it

**Windows:** double-click `launcher.pyw`.

1. **Generate config:** choose a GPU (when more than one is detected), a suggested setup, and the two languages. Save refreshes the launcher settings and closes the config window.
2. **▶ Launch:** downloads the servers and models the first time (this takes a while), then starts everything.
3. **Open dashboard:** opens <http://localhost:8080/>.

**Mac:** follow [INSTALL_MAC.md](INSTALL_MAC.md).

**From a terminal:** `./launch.sh` does the same as Launch. Ctrl+C stops everything.

## Use it

- Whisper identifies the spoken language; translations explicitly target the other configured language.
- Switch between **Live translation**, **Text translation**, and **Component tests** in the header
  without stopping capture or losing your place. For example, captured YouTube audio keeps translating
  while you type a separate translation. Refreshing or closing the app ends the session.
- **Text translation** in the header opens a chat page for typing or pasting text and choosing a
  language direction. Enter sends; Shift+Enter adds a line. Each translation appears below your message,
  with Copy and Listen controls.
- **Start** (or Space) and talk. With Continuous Audio Mode on, each pause starts a new row.
- **▶** after a translation speaks it; the adjacent copy icon copies the translation. With Automatic Translation Playback on, translations are spoken when the
  speaker pauses.
- **Capture audio** (the left sidebar) translates a browser tab or your whole system instead of the microphone. Tick
  **Mute captured tab** first to hear only the translations.
- The header's **Component tests** page checks each stage on its own.

## Settings

The launcher's Generate config covers the usual choices. For anything else, put your own settings in
`config_local.toml` in the project folder; `config/defaults.toml` lists every setting with comments.

## More

- [DESIGN.md](DESIGN.md): how it works, in detail: architecture, configuration, scripts, running servers on another
  machine.
- [voices/beth_thomas/README.md](voices/beth_thomas/README.md): the default voice.

## License

BSD 2-Clause; see [LICENSE](LICENSE).
