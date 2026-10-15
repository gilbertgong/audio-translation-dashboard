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

1. **Generate config:** picks a setup to suit your GPU and lets you choose the two languages. Save.
2. **▶ Launch:** downloads the servers and models the first time (this takes a while), then starts everything.
3. **Open dashboard:** opens <http://localhost:8080/>.

**Mac:** follow [INSTALL_MAC.md](INSTALL_MAC.md).

**From a terminal:** `./launch.sh` does the same as Launch. Ctrl+C stops everything.

## Use it

- **Start** (or Space) and talk. With Continuous Audio Mode on, each pause starts a new row.
- **▶** after a translation speaks it. With Automatic Translation Playback on, translations are spoken when the
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
